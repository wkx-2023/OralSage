"""Learning Orchestrator — top-level agent that wires Planner, Tutor, QA,
Assessment, and Memory into a single terminal-drivable learning flow.

Public API
----------
* ``LearningOrchestrator`` — the main orchestrator class.

The orchestrator's job is to correctly manage session state, progress,
and event recording.  Content generation is delegated to leaf modules
(Tutor, QA, Assessment).
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from .assessment import assess_answer
from .audio import ensure_handout_audio, get_section_audio
from .handout_cache import (
    get_handout_cache,
    list_handout_cache_status,
    save_handout_cache_failed,
    save_handout_cache_pending,
    save_handout_cache_ready,
)
from .tutor_cache import (
    get_tutor_cache,
    save_tutor_cache_failed,
    save_tutor_cache_pending,
    save_tutor_cache_ready,
)
from .memory import LearningMemory
from .planner import complete_progressive_lesson_plan, plan_lesson
from .profile_memory import build_profile_slice, update_learner_memory_from_event
from .qa import answer_interruption_question, answer_question_with_evidence
from .resource_factory import generate_resources
from .resource_factory import generate_handout_section as _generate_handout_section
from .tutor import build_tutor_step as _build_tutor_step
from .tutor import build_tutor_signals
from .user_model import merge_profile, update_profile_from_assessment, update_profile_from_qa


class LearningOrchestrator:
    """Top-level agent that orchestrates a full learning session.

    Parameters
    ----------
    memory:
        A :class:`LearningMemory` instance.  If *None*, a default one is
        created pointing at ``data/learning/learning.sqlite3``.
    resource_root:
        Passed through to :func:`generate_resources`.  Defaults to
        ``data/learning/sessions``.
    policy_fn:
        Optional callable that replaces the default rule-based policy
        (``decide_next_action``).  Receives ``(state, user_input)`` and
        must return a decision dict.  When *None*, the built-in rule-based
        policy is used.
    """

    def __init__(
        self,
        memory: LearningMemory | None = None,
        resource_root: str | Path | None = None,
        policy_fn: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.memory = memory or LearningMemory()
        self.resource_root = resource_root or Path("data/learning/sessions")
        self.policy_fn: Callable[..., dict[str, Any]] = (
            policy_fn or decide_next_action
        )
        # Cached from last start_lesson for handout-section-driven tutor
        self._last_evidence_pool: dict[str, Any] | None = None
        self._last_outline_evidence_map: dict[str, Any] | None = None
        # Cached handout sections keyed by outline_node_id — populated during
        # Tutor steps, read by QA without triggering regeneration.
        self._last_handout_sections: dict[str, Any] = {}
        # Most recent wrong-answer context — populated by submit_answer when
        # user answers incorrectly; cleared on continue_to_next_section.
        # Used by handle_interruption to provide context for QA follow-up.
        self._last_wrong_answer_context: dict[str, Any] | None = None
        self._outline_completion_threads: dict[str, threading.Thread] = {}
        self._outline_completion_events: dict[str, threading.Event] = {}
        self._outline_completion_errors: dict[str, str] = {}

    def _update_learner_memory_for_event(
        self,
        *,
        user_id: str,
        session_id: str,
        event_id: str,
        surface: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Best-effort L1 event -> L3 learner-memory update."""
        try:
            return update_learner_memory_from_event(
                self.memory,
                user_id=user_id,
                session_id=session_id,
                event_id=event_id,
                surface=surface,
                context=context or {},
            )
        except Exception as exc:  # noqa: BLE001 - memory must not block learning
            return {
                "updated": False,
                "reason": "memory_update_exception",
                "error": str(exc)[:240],
            }

    # ------------------------------------------------------------------
    # Start lesson
    # ------------------------------------------------------------------

    def start_lesson(
        self,
        user_id: str,
        topic: str,
        display_name: str | None = None,
        profile_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Begin a new learning session.

        1. Get or create the user profile, merging *profile_input* if given.
        2. Create a session row.
        3. Plan the lesson outline.
        4. Persist the outline.
        5. Set current node to the first outline node.
        6. Generate resource files.
        7. Record ``lesson_started`` event.

        Returns a dict with session, profile, outline, mindmap,
        source_trace, resources, and current_outline_node_id.
        """
        # --- profile --------------------------------------------------------
        profile = self.memory.get_or_create_profile(user_id, display_name)
        if profile_input:
            profile = merge_profile(profile, profile_input)
            profile = self.memory.save_profile(user_id, profile)

        # --- session --------------------------------------------------------
        session = self.memory.create_session(user_id, topic)
        session_id: str = session["id"]

        # --- plan lesson ----------------------------------------------------
        planner_profile = dict(profile)
        planner_profile["learner_memory_slice"] = build_profile_slice(
            self.memory,
            user_id=user_id,
            role="planner",
            context={"topic": topic},
        )
        plan_result = plan_lesson(topic, user_profile=planner_profile)
        if not plan_result.get("ok"):
            raise RuntimeError(
                f"plan_lesson failed for topic {topic!r}"
            )
        outline: dict[str, Any] = plan_result["outline"]
        mindmap: dict[str, Any] = plan_result["mindmap"]
        source_trace: dict[str, Any] = plan_result["source_trace"]

        nodes: list[dict[str, Any]] = outline.get("nodes", [])
        if not nodes:
            raise ValueError("outline has no nodes — cannot start lesson")

        # --- persist outline ------------------------------------------------
        self.memory.save_outline(session_id, outline, mindmap, source_trace)

        # --- set initial node -----------------------------------------------
        first_node_id = nodes[0]["id"]
        self.memory.set_current_outline_node(session_id, first_node_id)

        # --- cache evidence data for later handout-section-driven tutor ---
        self._last_evidence_pool = plan_result.get("evidence_pool", {})
        self._last_outline_evidence_map = plan_result.get("outline_evidence_map", {})

        # --- generate resources ---------------------------------------------
        resources = generate_resources(
            session_id=session_id,
            topic=topic,
            outline=outline,
            mindmap=mindmap,
            output_root=self.resource_root,
        )

        # --- record event ---------------------------------------------------
        lesson_started_event_id = self.memory.record_event(
            session_id=session_id,
            user_id=user_id,
            event_type="lesson_started",
            outline_node_id=first_node_id,
            payload={
                "surface": "planning",
                "topic": topic,
                "strategy": source_trace.get("strategy"),
                "node_count": len(nodes),
                "resource_count": len(resources),
            },
        )
        lesson_memory_update = self._update_learner_memory_for_event(
            user_id=user_id,
            session_id=session_id,
            event_id=lesson_started_event_id,
            surface="planning",
            context={
                "topic": topic,
                "outline_node_id": first_node_id,
                "kg_node_ids": nodes[0].get("kg_node_ids", []),
                "node_count": len(nodes),
            },
        )

        # --- generate first handout section synchronously ------------------
        first_section_ok = self._prepare_first_handout_section_sync(
            session_id=session_id,
            outline=outline,
            first_node_id=first_node_id,
        )

        # --- launch background outline completion / prefetch ---------------
        progressive_planning = plan_result.get("progressive_planning", {}) or {}
        outline_completion_started = False
        if progressive_planning.get("completion_required"):
            outline_completion_started = self._start_outline_completion_background(
                session_id=session_id,
                user_id=user_id,
                initial_plan_result=plan_result,
            )
        elif len(nodes) > 1:
            self._start_handout_prefetch_background(
                session_id=session_id,
                outline=outline,
            )

        return {
            "session": session,
            "profile": profile,
            "outline": outline,
            "mindmap": mindmap,
            "source_trace": source_trace,
            "resources": resources,
            "current_outline_node_id": first_node_id,
            "first_handout_section_ready": first_section_ok,
            "first_handout_audio": self._read_section_audio(
                session_id,
                first_node_id,
            ),
            "outline_status": (
                "initial_ready" if outline_completion_started else "complete"
            ),
            "outline_completion_started": outline_completion_started,
            "learner_memory_update": lesson_memory_update,
        }

    def _start_outline_completion_background(
        self,
        *,
        session_id: str,
        user_id: str,
        initial_plan_result: dict[str, Any],
    ) -> bool:
        """Start background completion for a progressive initial outline."""
        existing = self._outline_completion_threads.get(session_id)
        if existing and existing.is_alive():
            return True

        event = threading.Event()
        self._outline_completion_events[session_id] = event
        self._outline_completion_errors.pop(session_id, None)

        thread = threading.Thread(
            target=self._complete_outline_background,
            args=(session_id, user_id, initial_plan_result, event),
            daemon=True,
            name=f"outline-complete-{session_id[:8]}",
        )
        self._outline_completion_threads[session_id] = thread
        thread.start()
        return True

    def _complete_outline_background(
        self,
        session_id: str,
        user_id: str,
        initial_plan_result: dict[str, Any],
        done_event: threading.Event,
    ) -> None:
        """Background worker: finish outline planning, then prefetch sections."""
        try:
            completed = complete_progressive_lesson_plan(initial_plan_result)
            if not completed.get("ok"):
                raise RuntimeError("complete_progressive_lesson_plan returned not ok")

            outline = completed["outline"]
            mindmap = completed["mindmap"]
            source_trace = completed["source_trace"]

            self.memory.save_outline(session_id, outline, mindmap, source_trace)
            self._last_evidence_pool = completed.get("evidence_pool", {})
            self._last_outline_evidence_map = completed.get(
                "outline_evidence_map",
                {},
            )

            resources = generate_resources(
                session_id=session_id,
                topic=completed.get("topic", ""),
                outline=outline,
                mindmap=mindmap,
                output_root=self.resource_root,
            )

            nodes: list[dict[str, Any]] = outline.get("nodes", [])
            self.memory.record_event(
                session_id=session_id,
                user_id=user_id,
                event_type="outline_completed",
                outline_node_id=nodes[0].get("id") if nodes else None,
                payload={
                    "surface": "planning",
                    "strategy": source_trace.get("strategy"),
                    "node_count": len(nodes),
                    "resource_count": len(resources),
                    "background": True,
                },
            )

            if len(nodes) > 1:
                self._start_handout_prefetch_background(
                    session_id=session_id,
                    outline=outline,
                )
        except Exception as exc:  # noqa: BLE001 - background must not crash API
            self._outline_completion_errors[session_id] = str(exc)[:500]
            try:
                session = self.memory.get_session(session_id)
                self.memory.record_event(
                    session_id=session_id,
                    user_id=session.get("user_id", user_id),
                    event_type="outline_completion_failed",
                    outline_node_id=session.get("current_outline_node_id"),
                    payload={
                        "surface": "planning",
                        "error": f"{type(exc).__name__}: {exc}",
                        "background": True,
                    },
                )
            except Exception:
                pass
        finally:
            done_event.set()

    def _wait_for_outline_completion(
        self,
        session_id: str,
        timeout_seconds: float = 0.0,
    ) -> bool:
        """Wait for a progressive outline completion thread if one exists."""
        event = self._outline_completion_events.get(session_id)
        if event is None:
            return True
        if event.is_set():
            return session_id not in self._outline_completion_errors
        if timeout_seconds <= 0:
            return False
        event.wait(timeout_seconds)
        return event.is_set() and session_id not in self._outline_completion_errors

    # ------------------------------------------------------------------
    # Internal: handout section cache helpers
    # ------------------------------------------------------------------

    def _ensure_handout_section_audio(
        self,
        *,
        session_id: str,
        outline_node_id: str,
        handout_section: dict[str, Any],
        source: str,
    ) -> dict[str, Any]:
        """Best-effort slide-level audio generation for a handout section."""
        try:
            return ensure_handout_audio(
                session_id=session_id,
                outline_node_id=outline_node_id,
                handout_section=handout_section,
                source=source,
                resource_root=self.resource_root,
            )
        except Exception as exc:  # noqa: BLE001 - audio must not block handout
            return {
                "outline_node_id": outline_node_id,
                "status": "failed",
                "error": f"ensure_handout_audio_raised: {exc}",
            }

    def _read_section_audio(
        self,
        session_id: str,
        outline_node_id: str | None,
    ) -> dict[str, Any] | None:
        """Read slide-level audio metadata for one section, if present."""
        if not outline_node_id:
            return None
        try:
            return get_section_audio(
                session_id,
                outline_node_id,
                resource_root=self.resource_root,
            )
        except Exception:
            return None

    def _prepare_first_handout_section_sync(
        self,
        session_id: str,
        outline: dict[str, Any],
        first_node_id: str,
    ) -> bool:
        """Synchronously generate and cache the first handout section.

        Called during ``start_lesson`` so that the first Tutor step does
        not wait for handout generation.  On failure writes a ``failed``
        cache record but does **not** raise — the first Tutor step will
        handle the cache-miss gracefully.

        Returns True if the section was generated successfully.
        """
        nodes: list[dict[str, Any]] = outline.get("nodes", [])
        first_node: dict[str, Any] | None = None
        prev_title: str | None = None
        next_title: str | None = None
        for i, n in enumerate(nodes):
            if n.get("id") == first_node_id:
                first_node = n
                if i > 0:
                    prev_title = nodes[i - 1].get("title")
                if i + 1 < len(nodes):
                    next_title = nodes[i + 1].get("title")
                break

        if first_node is None:
            return False

        node_evidence = (
            self._last_outline_evidence_map or {}
        ).get(first_node_id, {})
        if not node_evidence:
            save_handout_cache_failed(
                session_id, first_node_id,
                "no_node_evidence_in_map",
                source="first_section_sync",
                resource_root=self.resource_root,
            )
            return False

        try:
            hs = _generate_handout_section(
                outline_node=first_node,
                node_evidence=node_evidence,
                evidence_pool=self._last_evidence_pool or {},
                previous_node_title=prev_title,
                next_node_title=next_title,
            )
        except Exception as exc:
            save_handout_cache_failed(
                session_id, first_node_id,
                f"generate_handout_section_raised: {exc}",
                source="first_section_sync",
                resource_root=self.resource_root,
            )
            return False

        if hs.get("section_status") == "llm_generated":
            save_handout_cache_ready(
                session_id, first_node_id,
                handout_section=hs,
                source="first_section_sync",
                resource_root=self.resource_root,
            )
            self._ensure_handout_section_audio(
                session_id=session_id,
                outline_node_id=first_node_id,
                handout_section=hs,
                source="first_section_sync",
            )
            # Also populate in-memory cache for fast QA reads
            if first_node_id:
                self._last_handout_sections[first_node_id] = hs
            return True
        else:
            save_handout_cache_failed(
                session_id, first_node_id,
                hs.get("error", "section_status_not_llm_generated"),
                source="first_section_sync",
                resource_root=self.resource_root,
            )
            return False

    def _start_handout_prefetch_background(
        self,
        session_id: str,
        outline: dict[str, Any],
    ) -> None:
        """Launch a daemon thread to pre-generate remaining handout sections.

        The thread runs ``_prefetch_remaining_handout_sections`` and is
        fully independent — failures are recorded per-node in the cache
        and do not affect the main flow.
        """
        t = threading.Thread(
            target=self._prefetch_remaining_handout_sections,
            args=(session_id, outline),
            daemon=True,
            name=f"handout-prefetch-{session_id[:8]}",
        )
        t.start()

    def _prefetch_remaining_handout_sections(
        self,
        session_id: str,
        outline: dict[str, Any],
    ) -> None:
        """Background worker: generate handout sections for nodes[1:]."""
        nodes: list[dict[str, Any]] = outline.get("nodes", [])
        if len(nodes) <= 1:
            return

        remaining = nodes[1:]  # skip first node (already done)

        def _generate_one(idx: int, node: dict[str, Any]) -> tuple[str, bool]:
            node_id = node.get("id", "")
            # Skip if already cached as ready
            existing = get_handout_cache(session_id, node_id, resource_root=self.resource_root)
            if existing.get("status") == "ready":
                # Also populate in-memory cache
                hs = existing.get("handout_section")
                if hs and isinstance(hs, dict):
                    self._last_handout_sections[node_id] = hs
                    self._ensure_handout_section_audio(
                        session_id=session_id,
                        outline_node_id=node_id,
                        handout_section=hs,
                        source="background_prefetch_cache_hit",
                    )
                return (node_id, True)

            # Mark pending
            save_handout_cache_pending(session_id, node_id, resource_root=self.resource_root)

            # Build context (previous / next titles)
            actual_idx = idx + 1  # offset because we sliced nodes[1:]
            prev_title: str | None = None
            next_title: str | None = None
            if actual_idx > 0:
                prev_title = nodes[actual_idx - 1].get("title")
            if actual_idx + 1 < len(nodes):
                next_title = nodes[actual_idx + 1].get("title")

            node_evidence = (
                self._last_outline_evidence_map or {}
            ).get(node_id, {})
            if not node_evidence:
                save_handout_cache_failed(
                    session_id, node_id,
                    "no_node_evidence_in_map",
                    source="background_prefetch",
                    resource_root=self.resource_root,
                )
                return (node_id, False)

            try:
                hs = _generate_handout_section(
                    outline_node=node,
                    node_evidence=node_evidence,
                    evidence_pool=self._last_evidence_pool or {},
                    previous_node_title=prev_title,
                    next_node_title=next_title,
                )
            except Exception as exc:
                save_handout_cache_failed(
                    session_id, node_id,
                    f"generate_handout_section_raised: {exc}",
                    source="background_prefetch",
                    resource_root=self.resource_root,
                )
                return (node_id, False)

            if hs.get("section_status") == "llm_generated":
                save_handout_cache_ready(
                    session_id, node_id,
                    handout_section=hs,
                    source="background_prefetch",
                    resource_root=self.resource_root,
                )
                self._ensure_handout_section_audio(
                    session_id=session_id,
                    outline_node_id=node_id,
                    handout_section=hs,
                    source="background_prefetch",
                )
                self._last_handout_sections[node_id] = hs

                # --- Generate and cache tutor_step draft ---
                # Only cache tutor_step when handout is valid.
                # Must be based on handout_section, NOT outline.
                self._generate_and_cache_tutor_step_draft(
                    session_id=session_id,
                    outline_node=node,
                    handout_section=hs,
                    actual_index=actual_idx,
                    total_nodes=len(nodes),
                    prev_title=prev_title,
                    next_title=next_title,
                )

                return (node_id, True)
            else:
                save_handout_cache_failed(
                    session_id, node_id,
                    hs.get("error", "section_status_not_llm_generated"),
                    source="background_prefetch",
                    resource_root=self.resource_root,
                )
                return (node_id, False)

        # Use ThreadPoolExecutor for concurrent prefetch (max 2 workers)
        max_workers = min(2, len(remaining))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {
                ex.submit(_generate_one, i, node): node
                for i, node in enumerate(remaining)
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    pass  # each _generate_one already handles its own errors

    def _generate_and_cache_tutor_step_draft(
        self,
        session_id: str,
        outline_node: dict[str, Any],
        handout_section: dict[str, Any],
        actual_index: int,
        total_nodes: int,
        prev_title: str | None,
        next_title: str | None,
    ) -> None:
        """Generate a tutor_step draft from a cached handout and persist it.

        Called during background prefetch after a handout section is ready.
        Uses the cached handout (not outline) to generate a default tutor
        step with no deep personalization.
        """
        node_id = outline_node.get("id", "")
        if not node_id:
            return

        # Mark pending
        save_tutor_cache_pending(
            session_id, node_id, resource_root=self.resource_root,
        )

        hs_content = handout_section.get("content", {})
        hs_title = handout_section.get(
            "title", outline_node.get("title", ""),
        )

        node_evidence = (
            self._last_outline_evidence_map or {}
        ).get(node_id, {})
        kg_node_ids = _extract_kg_node_ids_from_section(
            handout_section, node_evidence,
        )

        section_position = {
            "is_first_section": actual_index == 0,
            "is_last_section": actual_index + 1 >= total_nodes,
            "current_index": actual_index + 1,
            "total": total_nodes,
        }

        try:
            step = _build_tutor_step(
                outline_node=outline_node,
                handout_section_content=hs_content,
                handout_section_title=hs_title,
                kg_node_ids=kg_node_ids,
                previous_node_title=prev_title,
                next_node_title=next_title,
                user_mastery_summary=None,
                section_position=section_position,
            )
        except Exception as exc:
            save_tutor_cache_failed(
                session_id, node_id,
                f"build_tutor_step_raised: {exc}",
                source="background_prefetch",
                resource_root=self.resource_root,
            )
            return

        if step.get("content_status") == "llm_generated":
            save_tutor_cache_ready(
                session_id, node_id,
                tutor_step=step,
                source="background_prefetch",
                handout_source="background_prefetch",
                resource_root=self.resource_root,
            )
        else:
            save_tutor_cache_failed(
                session_id, node_id,
                step.get("trace", {}).get(
                    "error", "content_status_not_llm_generated",
                ),
                source="background_prefetch",
                resource_root=self.resource_root,
            )

    @staticmethod
    def _is_cache_in_progress(record: dict[str, Any]) -> bool:
        return record.get("status") in {"pending", "generating"}

    @staticmethod
    def _is_ready_handout_cache(record: dict[str, Any]) -> bool:
        return (
            record.get("status") == "ready"
            and record.get("section_status") == "llm_generated"
        )

    @staticmethod
    def _is_ready_tutor_cache(record: dict[str, Any]) -> bool:
        return (
            record.get("status") == "ready"
            and record.get("content_status") == "llm_generated"
        )

    def _wait_for_handout_cache_ready(
        self,
        session_id: str,
        outline_node_id: str,
        timeout_seconds: float = 8.0,
        poll_interval_seconds: float = 0.5,
    ) -> dict[str, Any]:
        """Briefly poll an in-progress handout cache before doing sync work."""
        record = get_handout_cache(
            session_id,
            outline_node_id,
            resource_root=self.resource_root,
        )
        if self._is_ready_handout_cache(record) or not self._is_cache_in_progress(record):
            return record

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            time.sleep(min(poll_interval_seconds, max(0.0, deadline - time.monotonic())))
            record = get_handout_cache(
                session_id,
                outline_node_id,
                resource_root=self.resource_root,
            )
            if self._is_ready_handout_cache(record) or not self._is_cache_in_progress(record):
                return record

        return record

    def _wait_for_tutor_cache_ready(
        self,
        session_id: str,
        outline_node_id: str,
        timeout_seconds: float = 8.0,
        poll_interval_seconds: float = 0.5,
    ) -> dict[str, Any]:
        """Briefly poll an in-progress tutor cache before doing sync work."""
        record = get_tutor_cache(
            session_id,
            outline_node_id,
            resource_root=self.resource_root,
        )
        if self._is_ready_tutor_cache(record) or not self._is_cache_in_progress(record):
            return record

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            time.sleep(min(poll_interval_seconds, max(0.0, deadline - time.monotonic())))
            record = get_tutor_cache(
                session_id,
                outline_node_id,
                resource_root=self.resource_root,
            )
            if self._is_ready_tutor_cache(record) or not self._is_cache_in_progress(record):
                return record

        return record

    def _return_cached_tutor_step(
        self,
        session_id: str,
        user_id: str,
        current_node_id: str | None,
        tutor_cache_record: dict[str, Any],
    ) -> dict[str, Any]:
        """Hydrate, trace, and return a ready cached tutor step."""
        step = tutor_cache_record.get("tutor_step") or {}
        tutor_generation_source = (
            tutor_cache_record.get("trace", {}).get("source", "")
        )

        _ho_cache = get_handout_cache(
            session_id,
            current_node_id or "",
            resource_root=self.resource_root,
        )
        if _ho_cache.get("status") == "ready":
            _ho_section = _ho_cache.get("handout_section")
            if _ho_section and current_node_id:
                self._last_handout_sections[current_node_id] = _ho_section
                self._ensure_handout_section_audio(
                    session_id=session_id,
                    outline_node_id=current_node_id,
                    handout_section=_ho_section,
                    source="tutor_cache_hit",
                )

        exposure_recorded = False
        exposure_memory_update: dict[str, Any] | None = None
        mastery_records: list[dict[str, Any]] = []
        kg_node_ids: list[str] = []
        _cached_kg = step.get("signals", {}).get("used_kg_node_ids", [])
        _cached_hs_title = step.get("signals", {}).get(
            "used_handout_section_title",
            "",
        )
        if _cached_kg:
            try:
                mastery_records, exposure_event_id = self.memory.record_tutor_exposure(
                    user_id=user_id,
                    session_id=session_id,
                    outline_node_id=current_node_id or "",
                    kg_node_ids=_cached_kg,
                    handout_section_title=_cached_hs_title,
                )
                exposure_recorded = True
                exposure_memory_update = self._update_learner_memory_for_event(
                    user_id=user_id,
                    session_id=session_id,
                    event_id=exposure_event_id,
                    surface="lecture",
                    context={
                        "outline_node_id": current_node_id,
                        "kg_node_ids": _cached_kg,
                        "handout_section_title": _cached_hs_title,
                    },
                )
            except Exception:
                pass

        step["_handout_section_used"] = True
        step["_handout_section_title"] = _cached_hs_title
        step["_exposure_recorded"] = exposure_recorded
        step["_learner_memory_update"] = exposure_memory_update
        step["_mastery_records"] = mastery_records

        step["tutor_cache_hit"] = True
        step["tutor_cache_miss_sync_generation"] = False
        step["tutor_generation_source"] = tutor_generation_source

        step["handout_cache_hit"] = True
        step["handout_cache_status"] = "ready"
        step["handout_generation_source"] = (
            tutor_cache_record.get("trace", {}).get("handout_source", "")
        )
        step["cache_miss_sync_generation"] = False
        step["section_audio"] = self._read_section_audio(session_id, current_node_id)

        tutor_step_event_id = self.memory.record_event(
            session_id=session_id,
            user_id=user_id,
            event_type="tutor_step_generated",
            outline_node_id=step.get("outline_node_id", current_node_id or ""),
            payload={
                "outline_node_id": step.get("outline_node_id", current_node_id or ""),
                "content_status": step.get("content_status"),
                "trace_mode": step.get("trace", {}).get("mode", ""),
                "handout_section_used": True,
                "exposure_recorded": exposure_recorded,
                "tutor_cache_hit": True,
            },
        )
        tutor_step_memory_update = self._update_learner_memory_for_event(
            user_id=user_id,
            session_id=session_id,
            event_id=tutor_step_event_id,
            surface="lecture",
            context={
                "outline_node_id": current_node_id,
                "kg_node_ids": _cached_kg,
                "content_status": step.get("content_status"),
            },
        )

        return {
            **step,
            "session_id": session_id,
            "current_outline_node_id": current_node_id,
            "tutor_step_memory_update": tutor_step_memory_update,
        }

    # ------------------------------------------------------------------
    # Next step
    # ------------------------------------------------------------------

    def next_step(self, session_id: str) -> dict[str, Any]:
        """Return the Tutor step for the current outline node.

        Attempts the **LLM handout-section-driven path** first: generates a
        handout section for the current node, then uses the LLM Tutor to
        produce teaching text from it.  Falls back to the rule-based path
        when handout generation or LLM is unavailable.

        After a successful LLM Tutor step, records a ``tutor_exposure``
        event and updates mastery with a small exposure bump for each KG
        node covered.

        Does **not** advance progress — only reads the current session state
        and produces teaching content.
        """
        outline_record = _require_outline(self.memory, session_id)
        outline: dict[str, Any] = outline_record["outline"]
        outline_nodes: list[dict[str, Any]] = outline.get("nodes", [])
        session = self.memory.get_session(session_id)
        current_node_id: str | None = session.get("current_outline_node_id")
        user_id: str = session["user_id"]

        # --- Find current outline node ---
        current_node: dict[str, Any] | None = None
        current_index = -1
        prev_title: str | None = None
        next_title: str | None = None
        for i, n in enumerate(outline_nodes):
            if n.get("id") == current_node_id:
                current_node = n
                current_index = i
                if i > 0:
                    prev_title = outline_nodes[i - 1].get("title")
                if i + 1 < len(outline_nodes):
                    next_title = outline_nodes[i + 1].get("title")
                break

        if (
            current_node
            and current_node_id
            and not (self._last_outline_evidence_map or {}).get(current_node_id)
            and self._wait_for_outline_completion(session_id, timeout_seconds=8.0)
        ):
            outline_record = _require_outline(self.memory, session_id)
            outline = outline_record["outline"]
            outline_nodes = outline.get("nodes", [])
            current_node = None
            current_index = -1
            prev_title = None
            next_title = None
            for i, n in enumerate(outline_nodes):
                if n.get("id") == current_node_id:
                    current_node = n
                    current_index = i
                    if i > 0:
                        prev_title = outline_nodes[i - 1].get("title")
                    if i + 1 < len(outline_nodes):
                        next_title = outline_nodes[i + 1].get("title")
                    break

        step: dict[str, Any]
        exposure_recorded = False
        exposure_memory_update: dict[str, Any] | None = None
        mastery_records: list[dict[str, Any]] = []
        kg_node_ids: list[str] = []

        # ================================================================
        # Try LLM handout-section-driven Tutor (cache-aware)
        # ================================================================
        handout_cache_hit: bool = False
        handout_cache_status: str = "unknown"
        handout_generation_source: str = ""
        cache_miss_sync_generation: bool = False

        if current_node and self._last_outline_evidence_map and self._last_evidence_pool:
            node_evidence = self._last_outline_evidence_map.get(current_node_id or "", {})
            if node_evidence:
                # ============================================================
                # Check tutor_step cache first
                # ============================================================
                tutor_cache_record = get_tutor_cache(
                    session_id, current_node_id or "",
                    resource_root=self.resource_root,
                )
                if self._is_cache_in_progress(tutor_cache_record):
                    tutor_cache_record = self._wait_for_tutor_cache_ready(
                        session_id,
                        current_node_id or "",
                    )

                if self._is_ready_tutor_cache(tutor_cache_record):
                    # --- Tutor cache hit: return cached step, no LLM ---
                    step = tutor_cache_record.get("tutor_step") or {}
                    tutor_cache_hit = True
                    tutor_generation_source = (
                        tutor_cache_record.get("trace", {}).get("source", "")
                    )
                    tutor_cache_miss_sync_generation = False

                    # Populate in-memory handout cache for QA reads
                    _ho_cache = get_handout_cache(
                        session_id, current_node_id or "",
                        resource_root=self.resource_root,
                    )
                    if _ho_cache.get("status") == "ready":
                        _ho_section = _ho_cache.get("handout_section")
                        if _ho_section and current_node_id:
                            self._last_handout_sections[current_node_id] = _ho_section
                            self._ensure_handout_section_audio(
                                session_id=session_id,
                                outline_node_id=current_node_id,
                                handout_section=_ho_section,
                                source="tutor_cache_hit",
                            )

                    # Record exposure from cached step
                    _cached_kg = (
                        step.get("signals", {}).get("used_kg_node_ids", [])
                    )
                    _cached_hs_title = (
                        step.get("signals", {}).get(
                            "used_handout_section_title", "",
                        )
                    )
                    if _cached_kg:
                        try:
                            mastery_records, exposure_event_id = self.memory.record_tutor_exposure(
                                user_id=user_id,
                                session_id=session_id,
                                outline_node_id=current_node_id or "",
                                kg_node_ids=_cached_kg,
                                handout_section_title=_cached_hs_title,
                            )
                            exposure_recorded = True
                            exposure_memory_update = self._update_learner_memory_for_event(
                                user_id=user_id,
                                session_id=session_id,
                                event_id=exposure_event_id,
                                surface="lecture",
                                context={
                                    "outline_node_id": current_node_id,
                                    "kg_node_ids": _cached_kg,
                                    "handout_section_title": _cached_hs_title,
                                },
                            )
                        except Exception:
                            pass

                    step["_handout_section_used"] = True
                    step["_handout_section_title"] = _cached_hs_title
                    step["_exposure_recorded"] = exposure_recorded
                    step["_learner_memory_update"] = exposure_memory_update
                    step["_mastery_records"] = mastery_records

                    # Tutor cache trace
                    step["tutor_cache_hit"] = True
                    step["tutor_cache_miss_sync_generation"] = False
                    step["tutor_generation_source"] = tutor_generation_source

                    # Handout cache trace (implied by tutor cache hit)
                    step["handout_cache_hit"] = True
                    step["handout_cache_status"] = "ready"
                    step["handout_generation_source"] = (
                        tutor_cache_record.get("trace", {}).get(
                            "handout_source", "",
                        )
                    )
                    step["cache_miss_sync_generation"] = False
                    step["section_audio"] = self._read_section_audio(
                        session_id,
                        current_node_id,
                    )

                    # Record event
                    tutor_step_event_id = self.memory.record_event(
                        session_id=session_id,
                        user_id=user_id,
                        event_type="tutor_step_generated",
                        outline_node_id=step["outline_node_id"],
                        payload={
                            "outline_node_id": step["outline_node_id"],
                            "content_status": step.get("content_status"),
                            "trace_mode": step["trace"]["mode"],
                            "handout_section_used": True,
                            "exposure_recorded": exposure_recorded,
                            "tutor_cache_hit": True,
                        },
                    )
                    tutor_step_memory_update = self._update_learner_memory_for_event(
                        user_id=user_id,
                        session_id=session_id,
                        event_id=tutor_step_event_id,
                        surface="lecture",
                        context={
                            "outline_node_id": current_node_id,
                            "kg_node_ids": _cached_kg,
                            "content_status": step.get("content_status"),
                        },
                    )

                    return {
                        **step,
                        "session_id": session_id,
                        "current_outline_node_id": current_node_id,
                        "tutor_step_memory_update": tutor_step_memory_update,
                    }

                # --- Tutor cache miss — fall through to handout logic ---
                tutor_cache_hit = False
                tutor_cache_miss_sync_generation = True
                tutor_generation_source = ""

                # --- Check handout cache first ---
                cache_record = get_handout_cache(session_id, current_node_id or "", resource_root=self.resource_root)
                handout_cache_status = cache_record.get("status", "missing")
                if self._is_cache_in_progress(cache_record):
                    cache_record = self._wait_for_handout_cache_ready(
                        session_id,
                        current_node_id or "",
                    )
                    handout_cache_status = cache_record.get("status", "missing")

                hs: dict[str, Any]

                if (
                    cache_record.get("status") == "ready"
                    and cache_record.get("section_status") == "llm_generated"
                ):
                    # Cache hit — use cached handout section directly
                    hs = cache_record.get("handout_section") or {}
                    if hs and current_node_id:
                        self._ensure_handout_section_audio(
                            session_id=session_id,
                            outline_node_id=current_node_id,
                            handout_section=hs,
                            source="next_step_handout_cache_hit",
                        )
                    handout_cache_hit = True
                    handout_generation_source = (
                        cache_record.get("trace", {}).get("source", "")
                    )
                    cache_miss_sync_generation = False
                elif handout_cache_status in ("pending", "generating"):
                    # Backend still generating — sync fallback
                    try:
                        hs = _generate_handout_section(
                            outline_node=current_node,
                            node_evidence=node_evidence,
                            evidence_pool=self._last_evidence_pool,
                            previous_node_title=prev_title,
                            next_node_title=next_title,
                        )
                    except Exception:
                        hs = {"section_status": "failed"}
                    handout_cache_hit = False
                    cache_miss_sync_generation = True
                    if hs.get("section_status") == "llm_generated":
                        save_handout_cache_ready(
                            session_id, current_node_id or "",
                            handout_section=hs,
                            source="cache_miss_sync_generation",
                            resource_root=self.resource_root,
                        )
                        self._ensure_handout_section_audio(
                            session_id=session_id,
                            outline_node_id=current_node_id or "",
                            handout_section=hs,
                            source="cache_miss_sync_generation",
                        )
                else:
                    # missing / failed — sync generation as fallback
                    try:
                        hs = _generate_handout_section(
                            outline_node=current_node,
                            node_evidence=node_evidence,
                            evidence_pool=self._last_evidence_pool,
                            previous_node_title=prev_title,
                            next_node_title=next_title,
                        )
                    except Exception:
                        hs = {"section_status": "failed"}
                    handout_cache_hit = False
                    cache_miss_sync_generation = True
                    if hs.get("section_status") == "llm_generated":
                        save_handout_cache_ready(
                            session_id, current_node_id or "",
                            handout_section=hs,
                            source="cache_miss_sync_generation",
                            resource_root=self.resource_root,
                        )
                        self._ensure_handout_section_audio(
                            session_id=session_id,
                            outline_node_id=current_node_id or "",
                            handout_section=hs,
                            source="cache_miss_sync_generation",
                        )

                if hs.get("section_status") != "llm_generated" and cache_miss_sync_generation:
                    refreshed_tutor_cache = get_tutor_cache(
                        session_id,
                        current_node_id or "",
                        resource_root=self.resource_root,
                    )
                    if self._is_ready_tutor_cache(refreshed_tutor_cache):
                        return self._return_cached_tutor_step(
                            session_id=session_id,
                            user_id=user_id,
                            current_node_id=current_node_id,
                            tutor_cache_record=refreshed_tutor_cache,
                        )

                    refreshed_handout_cache = get_handout_cache(
                        session_id,
                        current_node_id or "",
                        resource_root=self.resource_root,
                    )
                    handout_cache_status = refreshed_handout_cache.get(
                        "status", "missing",
                    )
                    if self._is_ready_handout_cache(refreshed_handout_cache):
                        hs = refreshed_handout_cache.get("handout_section") or {}
                        if hs and current_node_id:
                            self._ensure_handout_section_audio(
                                session_id=session_id,
                                outline_node_id=current_node_id,
                                handout_section=hs,
                                source="next_step_refreshed_handout_cache",
                            )
                        handout_cache_hit = True
                        handout_generation_source = (
                            refreshed_handout_cache.get("trace", {}).get("source", "")
                        )
                        cache_miss_sync_generation = False

                if hs.get("section_status") == "llm_generated":
                    # Cache for later QA reads (avoids regeneration)
                    if current_node_id:
                        self._last_handout_sections[current_node_id] = hs
                    hs_content = hs.get("content", {})
                    hs_title = hs.get("title", current_node.get("title", ""))
                    kg_node_ids = _extract_kg_node_ids_from_section(hs, node_evidence)

                    # Get mastery summary for these KG nodes
                    mastery_by_kg = self.memory.get_mastery_by_kg_node(
                        user_id=user_id, session_id=session_id
                    )
                    mastery_summary_for_tutor = dict(mastery_by_kg)
                    mastery_summary_for_tutor["profile_slice"] = build_profile_slice(
                        self.memory,
                        user_id=user_id,
                        role="tutor",
                        context={
                            "session_id": session_id,
                            "topic": session.get("topic", ""),
                            "outline_node_id": current_node_id,
                            "kg_node_ids": kg_node_ids,
                        },
                    )

                    try:
                        step = _build_tutor_step(
                            outline_node=current_node,
                            handout_section_content=hs_content,
                            handout_section_title=hs_title,
                            kg_node_ids=kg_node_ids,
                            previous_node_title=prev_title,
                            next_node_title=next_title,
                            user_mastery_summary=mastery_summary_for_tutor,
                            section_position={
                                "is_first_section": current_index == 0,
                                "is_last_section": (
                                    current_index >= 0
                                    and current_index + 1 == len(outline_nodes)
                                ),
                                "current_index": current_index + 1,
                                "total": len(outline_nodes),
                            },
                        )
                    except Exception as exc:
                        step = {
                            "content_status": "failed",
                            "trace": {
                                "mode": "handout_section_llm_tutor",
                                "error": f"build_tutor_step_raised: {exc}",
                            },
                        }

                    if step.get("content_status") != "failed":
                        # --- Record tutor exposure ---
                        signals = build_tutor_signals(step)
                        used_kg = signals.get("used_kg_node_ids", kg_node_ids)
                        if used_kg:
                            try:
                                mastery_records, exposure_event_id = self.memory.record_tutor_exposure(
                                    user_id=user_id,
                                    session_id=session_id,
                                    outline_node_id=current_node_id or "",
                                    kg_node_ids=used_kg,
                                    handout_section_title=hs_title,
                                )
                                exposure_recorded = True
                                exposure_memory_update = self._update_learner_memory_for_event(
                                    user_id=user_id,
                                    session_id=session_id,
                                    event_id=exposure_event_id,
                                    surface="lecture",
                                    context={
                                        "outline_node_id": current_node_id,
                                        "kg_node_ids": used_kg,
                                        "handout_section_title": hs_title,
                                    },
                                )
                            except Exception:
                                pass  # Non-fatal: tutor step is still valid

                        step["_handout_section_used"] = True
                        step["_handout_section_title"] = hs_title
                        step["_exposure_recorded"] = exposure_recorded
                        step["_learner_memory_update"] = exposure_memory_update
                        step["_mastery_records"] = mastery_records
                        # Cache trace fields
                        step["handout_cache_hit"] = handout_cache_hit
                        step["handout_cache_status"] = handout_cache_status
                        step["handout_generation_source"] = handout_generation_source
                        step["cache_miss_sync_generation"] = cache_miss_sync_generation
                        # Tutor cache trace (always miss on this path)
                        step["tutor_cache_hit"] = tutor_cache_hit
                        step["tutor_cache_miss_sync_generation"] = tutor_cache_miss_sync_generation
                        step["tutor_generation_source"] = tutor_generation_source
                        step["section_audio"] = self._read_section_audio(
                            session_id,
                            current_node_id,
                        )
                    else:
                        # LLM tutor failed — try rule-based below
                        refreshed_tutor_cache = get_tutor_cache(
                            session_id,
                            current_node_id or "",
                            resource_root=self.resource_root,
                        )
                        if self._is_ready_tutor_cache(refreshed_tutor_cache):
                            return self._return_cached_tutor_step(
                                session_id=session_id,
                                user_id=user_id,
                                current_node_id=current_node_id,
                                tutor_cache_record=refreshed_tutor_cache,
                            )

                        step["handout_cache_hit"] = handout_cache_hit
                        step["handout_cache_status"] = handout_cache_status
                        step["cache_miss_sync_generation"] = cache_miss_sync_generation
                        step["tutor_cache_hit"] = tutor_cache_hit
                        step["tutor_cache_miss_sync_generation"] = tutor_cache_miss_sync_generation
                        step["tutor_generation_source"] = tutor_generation_source
                        step["section_audio"] = self._read_section_audio(
                            session_id,
                            current_node_id,
                        )
                        pass
                else:
                    step = {"content_status": "failed",
                            "handout_cache_hit": False,
                            "handout_cache_status": handout_cache_status,
                            "cache_miss_sync_generation": cache_miss_sync_generation,
                            "tutor_cache_hit": tutor_cache_hit,
                            "tutor_cache_miss_sync_generation": tutor_cache_miss_sync_generation,
                            "tutor_generation_source": tutor_generation_source}
            else:
                step = {"content_status": "failed"}
        else:
            step = {"content_status": "failed"}

        # ================================================================
        # Fallback: rule-based Tutor
        # ================================================================
        if step.get("content_status") == "failed":
            step = _build_tutor_step(
                outline=outline,
                current_node_id=current_node_id,
            )
            step["_handout_section_used"] = False
            # Ensure tutor trace fields are present on fallback
            step.setdefault("tutor_cache_hit", False)
            step.setdefault("tutor_cache_miss_sync_generation", False)
            step.setdefault("tutor_generation_source", "")
        step.setdefault("section_audio", self._read_section_audio(session_id, current_node_id))

        # ================================================================
        # Record event (common for both paths)
        # ================================================================
        tutor_step_event_id = self.memory.record_event(
            session_id=session_id,
            user_id=user_id,
            event_type="tutor_step_generated",
            outline_node_id=step["outline_node_id"],
            payload={
                "outline_node_id": step["outline_node_id"],
                "content_status": step.get("content_status"),
                "trace_mode": step["trace"]["mode"],
                "handout_section_used": step.get("_handout_section_used", False),
                "exposure_recorded": exposure_recorded,
            },
        )
        tutor_step_memory_update = self._update_learner_memory_for_event(
            user_id=user_id,
            session_id=session_id,
            event_id=tutor_step_event_id,
            surface="lecture",
            context={
                "outline_node_id": current_node_id,
                "kg_node_ids": kg_node_ids,
                "content_status": step.get("content_status"),
            },
        )

        return {
            **step,
            "session_id": session_id,
            "current_outline_node_id": current_node_id,
            "tutor_step_memory_update": tutor_step_memory_update,
        }

    # ------------------------------------------------------------------
    # Interruption QA
    # ------------------------------------------------------------------

    def handle_interruption(
        self, session_id: str, question: str
    ) -> dict[str, Any]:
        """Handle a mid-lecture interruption question.

        Attempts the **LLM QA with evidence + targeted search** path first:
        reads current handout_section, node_evidence, and uses LLM-driven
        evidence planning and answer generation.  Falls back to rule-based
        QA when handout/evidence is unavailable or LLM is not configured.

        The QA result is recorded but course progress is **not** advanced.

        This is a **leaf executor** — it is recommended that upper layers use
        :meth:`route` + :meth:`execute_policy_action` as the unified execution
        entry point rather than calling this method directly.
        """
        outline_record = _require_outline(self.memory, session_id)
        outline: dict[str, Any] = outline_record["outline"]
        outline_nodes: list[dict[str, Any]] = outline.get("nodes", [])
        session = self.memory.get_session(session_id)
        current_node_id: str | None = session.get("current_outline_node_id")
        user_id: str = session["user_id"]
        topic: str = session.get("topic", "")

        before = current_node_id

        # --- Find current outline node ---
        current_node: dict[str, Any] | None = None
        for n in outline_nodes:
            if n.get("id") == current_node_id:
                current_node = n
                break

        # ================================================================
        # Try LLM QA with evidence + targeted search
        # ================================================================
        qa_result: dict[str, Any]
        qa_trace_mode: str = "fallback_rule_based"

        if (current_node and self._last_outline_evidence_map
                and self._last_evidence_pool is not None):
            node_evidence = self._last_outline_evidence_map.get(
                current_node_id or "", {}
            )
            if node_evidence:
                # Read existing handout section — do NOT generate on-the-fly.
                # User Q&A must not trigger Resource Factory regeneration.
                current_handout_section = self._read_existing_handout_section(
                    session_id=session_id,
                    current_node_id=current_node_id,
                )
                recent_qa_context = self._recent_qa_context_for_node(
                    session_id=session_id,
                    current_node_id=current_node_id,
                    limit=3,
                )

                # Get wrong-answer context if from same section
                _wrong_ctx = self._last_wrong_answer_context
                if _wrong_ctx and _wrong_ctx.get("outline_node_id") != current_node_id:
                    _wrong_ctx = None  # only pass context from same section

                qa_profile_slice = build_profile_slice(
                    self.memory,
                    user_id=user_id,
                    role="qa",
                    context={
                        "session_id": session_id,
                        "topic": topic,
                        "outline_node_id": current_node_id,
                        "kg_node_ids": current_node.get("kg_node_ids", []),
                    },
                )

                try:
                    qa_result = answer_question_with_evidence(
                        question=question,
                        topic=topic,
                        current_outline_node=current_node,
                        current_handout_section=current_handout_section,
                        node_evidence=node_evidence,
                        evidence_pool=self._last_evidence_pool,
                        user_profile=qa_profile_slice,
                        outline=outline,
                        current_node_id=current_node_id,
                        recent_qa_context=recent_qa_context,
                        wrong_answer_context=_wrong_ctx,
                    )
                    qa_trace_mode = qa_result.get("trace", {}).get("mode", "fallback_rule_based")
                except Exception:
                    qa_result = answer_interruption_question(
                        question=question,
                        outline=outline,
                        current_node_id=current_node_id,
                    )
            else:
                qa_result = answer_interruption_question(
                    question=question,
                    outline=outline,
                    current_node_id=current_node_id,
                )
        else:
            qa_result = answer_interruption_question(
                question=question,
                outline=outline,
                current_node_id=current_node_id,
            )

        # ================================================================
        # Guard: re-read session to confirm current_node_id unchanged
        # ================================================================
        session_after = self.memory.get_session(session_id)
        after = session_after.get("current_outline_node_id")

        # ================================================================
        # Record QA event with full payload
        # ================================================================
        qa_event_id = self.memory.record_event(
            session_id=session_id,
            user_id=user_id,
            event_type="user_interruption_question",
            outline_node_id=current_node_id,
            payload={
                "surface": "qa",
                "question": question,
                "answer": qa_result.get("answer", ""),
                "qa_status": qa_result.get("qa_status", "fallback_rule_based"),
                "question_scope": qa_result.get("question_scope", ""),
                "evidence_status": qa_result.get("evidence_status", ""),
                "targeted_search_used": qa_result.get("targeted_search_used", False),
                "search_plan": qa_result.get("search_plan", []),
                "used_evidence": qa_result.get("used_evidence", []),
                "related_kg_node_ids": qa_result.get("related_kg_node_ids", []),
                "confusion_kg_node_ids": qa_result.get("confusion_kg_node_ids", []),
                "should_reinforce": qa_result.get("should_reinforce", True),
                "advance_progress": qa_result.get("advance_progress", False),
                "trace_mode": qa_trace_mode,
            },
        )

        # ================================================================
        # Update user profile from QA interaction
        # ================================================================
        try:
            profile_before_qa = self.memory.get_or_create_profile(user_id)
            updated_profile_qa = update_profile_from_qa(
                profile=profile_before_qa,
                qa_result=qa_result,
                outline_node_id=current_node_id or "",
            )
            self.memory.save_profile(user_id, updated_profile_qa)
        except Exception:
            pass  # Non-fatal

        # ================================================================
        # Update mastery if QA exposes confusion
        # ================================================================
        # Only when should_reinforce=True and confusion_kg_node_ids exist:
        # apply a conservative mastery decrease (-0.03) to flag the gap
        # without over-penalizing.
        if (
            qa_result.get("should_reinforce") is True
            and qa_result.get("confusion_kg_node_ids")
        ):
            confusion_ids = qa_result.get("confusion_kg_node_ids", [])
            if isinstance(confusion_ids, list):
                for kg_id in confusion_ids:
                    if kg_id and isinstance(kg_id, str) and kg_id.strip():
                        try:
                            self.memory.upsert_mastery(
                                user_id=user_id,
                                session_id=session_id,
                                concept_name=f"QA confusion: {kg_id}",
                                mastery_delta=-0.03,
                                kg_node_id=kg_id.strip(),
                                evidence={
                                    "event": "qa_confusion_signal",
                                    "outline_node_id": current_node_id,
                                    "question": question,
                                    "should_reinforce": True,
                                },
                            )
                        except Exception:
                            pass  # Non-fatal

        learner_memory_update = self._update_learner_memory_for_event(
            user_id=user_id,
            session_id=session_id,
            event_id=qa_event_id,
            surface="qa",
            context={
                "topic": topic,
                "outline_node_id": current_node_id,
                "kg_node_ids": (
                    current_node.get("kg_node_ids", []) if current_node else []
                ),
            },
        )

        return {
            **qa_result,
            "current_outline_node_id_before": before,
            "current_outline_node_id_after": after,
            "learner_memory_update": learner_memory_update,
        }

    # ------------------------------------------------------------------
    # Internal: read existing handout section (no generation)
    # ------------------------------------------------------------------

    def _read_existing_handout_section(
        self,
        session_id: str,
        current_node_id: str | None,
    ) -> dict[str, Any] | None:
        """Read an already-generated handout section, preferring file cache.

        Check order:
        1. File-based handout cache (``get_handout_cache``).
        2. In-memory ``_last_handout_sections`` (backward compatible).

        Returns the handout section dict if found and valid, or None.
        Does **NOT** trigger Resource Factory generation — user Q&A
        must not cause on-the-fly handout regeneration.
        """
        if not current_node_id:
            return None

        # 1. File cache (persisted across sessions)
        cache = get_handout_cache(session_id, current_node_id, resource_root=self.resource_root)
        if cache.get("status") == "ready" and cache.get("section_status") == "llm_generated":
            hs = cache.get("handout_section")
            if hs and isinstance(hs, dict):
                return hs

        # 2. In-memory cache (backward compatible)
        cached = getattr(self, "_last_handout_sections", None)
        if cached and isinstance(cached, dict):
            section = cached.get(current_node_id)
            if section and isinstance(section, dict):
                if section.get("section_status") == "llm_generated":
                    return section

        return None

    def _recent_qa_context_for_node(
        self,
        session_id: str,
        current_node_id: str | None,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Return recent QA turns from the current outline node only.

        This supports same-section follow-up questions while intentionally
        dropping context after the user moves to another section.
        """
        if not current_node_id:
            return []
        try:
            summary = self.memory.session_summary(session_id)
        except Exception:
            return []

        turns: list[dict[str, Any]] = []
        for event in summary.get("events", []):
            if event.get("event_type") != "user_interruption_question":
                continue
            if event.get("outline_node_id") != current_node_id:
                continue
            payload = event.get("payload", {}) or {}
            question = str(payload.get("question", "")).strip()
            answer = str(payload.get("answer", "")).strip()
            if not question and not answer:
                continue
            turns.append({
                "question": question,
                "answer": answer[:1200],
                "qa_status": payload.get("qa_status", ""),
                "question_scope": payload.get("question_scope", ""),
                "evidence_status": payload.get("evidence_status", ""),
            })
        return turns[-max(0, limit):]

    # ------------------------------------------------------------------
    # Submit answer
    # ------------------------------------------------------------------

    def submit_answer(
        self,
        session_id: str,
        question: dict[str, Any],
        user_answer: str,
    ) -> dict[str, Any]:
        """Grade an answer and advance / hold progress accordingly.

        - Correct → advance to next node (or complete lesson).
        - Wrong   → stay on current node for reinforcement.

        Also persists answer_records and mastery_records.

        This is a **leaf executor** — it is recommended that upper layers use
        :meth:`route` + :meth:`execute_policy_action` as the unified execution
        entry point rather than calling this method directly.
        """
        outline_record = _require_outline(self.memory, session_id)
        outline: dict[str, Any] = outline_record["outline"]
        nodes: list[dict[str, Any]] = outline.get("nodes", [])
        session = self.memory.get_session(session_id)
        current_node_id: str | None = session.get("current_outline_node_id")
        user_id: str = session["user_id"]

        before = current_node_id

        # --- find current node title for mastery concept_name ---------------
        current_node_title = ""
        current_node: dict[str, Any] | None = None
        if current_node_id:
            for n in nodes:
                if n.get("id") == current_node_id:
                    current_node = n
                    current_node_title = n.get("title", "")
                    break

        # --- assess ---------------------------------------------------------
        # Read handout section and evidence for LLM deep analysis context
        _handout_section = self._read_existing_handout_section(
            session_id=session_id,
            current_node_id=current_node_id,
        )
        _node_evidence = None
        if self._last_outline_evidence_map and current_node_id:
            _node_evidence = self._last_outline_evidence_map.get(current_node_id, {})

        assessment_profile_slice = build_profile_slice(
            self.memory,
            user_id=user_id,
            role="assessment",
            context={
                "session_id": session_id,
                "outline_node_id": current_node_id,
                "kg_node_ids": question.get("kg_node_ids", [])
                or (current_node.get("kg_node_ids", []) if current_node else []),
            },
        )

        assessment = assess_answer(
            question=question,
            user_answer=user_answer,
            outline_node_id=current_node_id,
            user_profile=assessment_profile_slice,
            handout_section=_handout_section,
            node_evidence=_node_evidence,
        )

        # --- persist answer record ------------------------------------------
        self.memory.record_answer(
            session_id=session_id,
            user_id=user_id,
            question_id=question.get("id", ""),
            outline_node_id=current_node_id,
            user_answer=user_answer,
            grading={
                "is_correct": assessment["is_correct"],
                "score": assessment["score"],
                "mastery_delta": assessment["mastery_delta"],
                "mistake_tags": assessment["mistake_tags"],
                "explanation": assessment["explanation"],
                "option_error_analysis": assessment.get("option_error_analysis"),
                "correct_answer_explanation": assessment.get("correct_answer_explanation"),
                "misconception_summary": assessment.get("misconception_summary"),
                "related_kg_node_ids": assessment.get("related_kg_node_ids", []),
                "fallback_rule_based": assessment.get("fallback_rule_based", True),
                "content_status": assessment.get("content_status", "rule_based"),
                "recommendation": assessment["recommendation"],
            },
        )

        # --- persist mastery updates ----------------------------------------
        mastery_updates: list[dict[str, Any]] = []
        kg_node_ids: list[str] = question.get("kg_node_ids", [])
        evidence = {
            "question_id": question.get("id"),
            "outline_node_id": current_node_id,
            "user_answer": user_answer,
            "is_correct": assessment["is_correct"],
            "score": assessment["score"],
            "mastery_delta": assessment["mastery_delta"],
        }

        if kg_node_ids:
            for kg_id in kg_node_ids:
                concept = current_node_title or question.get("outline_node_id", "")
                m_rec = self.memory.upsert_mastery(
                    user_id=user_id,
                    session_id=session_id,
                    concept_name=concept,
                    mastery_delta=assessment["mastery_delta"],
                    kg_node_id=kg_id,
                    evidence=evidence,
                )
                mastery_updates.append(m_rec)
        else:
            # Fallback: at least one mastery record per answer
            concept = current_node_title or question.get("outline_node_id", "")
            m_rec = self.memory.upsert_mastery(
                user_id=user_id,
                session_id=session_id,
                concept_name=concept,
                mastery_delta=assessment["mastery_delta"],
                kg_node_id=None,
                evidence=evidence,
            )
            mastery_updates.append(m_rec)

        # --- wrong-answer: store context + update profile --------------------
        if not assessment["is_correct"]:
            # Store context for subsequent QA follow-up
            self._last_wrong_answer_context = {
                "outline_node_id": current_node_id,
                "question_id": question.get("id", ""),
                "stem": question.get("stem", ""),
                "options": question.get("options", []),
                "user_answer": user_answer,
                "selected_option": assessment.get("selected_option", ""),
                "correct_option": assessment.get("correct_option", ""),
                "option_error_analysis": assessment.get("option_error_analysis", ""),
                "correct_answer_explanation": assessment.get("correct_answer_explanation", ""),
                "misconception_summary": assessment.get("misconception_summary", ""),
                "remedial_suggestion": assessment.get("recommendation", {}).get("remedial_suggestion", ""),
            }

            # Update user profile from assessment
            try:
                profile_before = self.memory.get_or_create_profile(user_id)
                updated_profile = update_profile_from_assessment(
                    profile=profile_before,
                    assessment=assessment,
                    outline_node_id=current_node_id or "",
                    question=question,
                )
                self.memory.save_profile(user_id, updated_profile)
            except Exception:
                pass  # Non-fatal: profile update shouldn't block assessment

        # --- record answer_submitted (lesson event) -------------------------
        answer_event_id = self.memory.record_event(
            session_id=session_id,
            user_id=user_id,
            event_type="answer_submitted",
            outline_node_id=current_node_id,
            payload={
                "surface": "quiz",
                "question_id": question.get("id"),
                "question_stem": question.get("stem", ""),
                "kg_node_ids": kg_node_ids,
                "user_answer": user_answer,
                "is_correct": assessment["is_correct"],
                "score": assessment["score"],
                "mastery_delta": assessment["mastery_delta"],
                "mistake_tags": assessment.get("mistake_tags", []),
                "related_kg_node_ids": assessment.get("related_kg_node_ids", []),
                "misconception_summary": assessment.get("misconception_summary"),
                "remedial_suggestion": assessment.get("recommendation", {}).get("remedial_suggestion"),
                "recommendation_action": assessment["recommendation"]["action"],
                "fallback_rule_based": assessment.get("fallback_rule_based", True),
                "content_status": assessment.get("content_status", "rule_based"),
                "mastery_updates_count": len(mastery_updates),
            },
        )

        # --- progress decision ----------------------------------------------
        action: str = assessment["recommendation"]["action"]
        after: str | None = current_node_id
        advanced = False
        lesson_completed = False

        if action == "continue":
            next_id = _next_node_id(outline, current_node_id)
            if next_id is not None:
                self.memory.set_current_outline_node(session_id, next_id)
                after = next_id
                advanced = True
            else:
                # No more nodes — lesson complete
                self.memory.set_current_outline_node(session_id, None)
                after = None
                advanced = True
                lesson_completed = True
        # else: reinforce — stay on current node

        # --- record progress_updated ----------------------------------------
        progress_event_id = self.memory.record_event(
            session_id=session_id,
            user_id=user_id,
            event_type="progress_updated",
            outline_node_id=before,
            payload={
                "current_outline_node_id_before": before,
                "current_outline_node_id_after": after,
                "action": action,
                "advanced": advanced,
                "lesson_completed": lesson_completed,
            },
        )

        learner_memory_update = self._update_learner_memory_for_event(
            user_id=user_id,
            session_id=session_id,
            event_id=answer_event_id,
            surface="quiz",
            context={
                "outline_node_id": current_node_id,
                "question_id": question.get("id", ""),
                "kg_node_ids": kg_node_ids,
                "is_correct": assessment["is_correct"],
            },
        )
        progress_memory_update = self._update_learner_memory_for_event(
            user_id=user_id,
            session_id=session_id,
            event_id=progress_event_id,
            surface="planning",
            context={
                "outline_node_id": before,
                "current_outline_node_id_after": after,
                "action": action,
                "advanced": advanced,
                "lesson_completed": lesson_completed,
            },
        )

        return {
            "assessment": assessment,
            "current_outline_node_id_before": before,
            "current_outline_node_id_after": after,
            "advanced": advanced,
            "lesson_completed": lesson_completed,
            "mastery_updates": mastery_updates,
            "learner_memory_update": learner_memory_update,
            "progress_memory_update": progress_memory_update,
        }

    # ------------------------------------------------------------------
    # Policy layer — read-only decision support
    # ------------------------------------------------------------------

    def get_policy_state(self, session_id: str) -> dict[str, Any]:
        """Read a lightweight policy state dict for the decision layer.

        Returns
        -------
        dict
            Keys: ``session_id``, ``user_id``, ``topic``, ``mode``,
            ``status``, ``current_outline_node_id``, ``outline_node_count``,
            ``event_count``, ``answer_count``, ``mastery_count``.
        """
        session = self.memory.get_session(session_id)
        outline_rec = self.memory.latest_outline(session_id)
        outline_nodes: list = (
            outline_rec["outline"].get("nodes", []) if outline_rec else []
        )
        summary = self.memory.session_summary(session_id)

        return {
            "session_id": session["id"],
            "user_id": session["user_id"],
            "topic": session["topic"],
            "mode": session.get("mode", "course_learning"),
            "status": session.get("status", "active"),
            "current_outline_node_id": session.get("current_outline_node_id"),
            "outline_node_count": len(outline_nodes),
            "event_count": len(summary.get("events", [])),
            "answer_count": len(summary.get("answers", [])),
            "mastery_count": len(summary.get("mastery", [])),
        }

    def route(
        self, session_id: str, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Read policy state and produce a decision — does **not** execute.

        This is the read-only decision entry point.  It does not call
        Tutor / QA / Assessment, does not write events, and does not
        mutate ``current_outline_node_id``.
        """
        state = self.get_policy_state(session_id)
        decision = self.policy_fn(state, user_input)
        return {"state": state, "decision": decision}

    # ------------------------------------------------------------------
    # Unified execution entry point
    # ------------------------------------------------------------------

    def execute_policy_action(
        self,
        session_id: str,
        decision: dict[str, Any],
        user_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a policy decision by dispatching to the correct leaf executor.

        This is the **unified execution entry point**.  Upper layers (e.g. API
        endpoints, terminal loops) should route through :meth:`route` to get a
        decision and then call this method to execute it, rather than calling
        :meth:`next_step`, :meth:`handle_interruption`, or
        :meth:`submit_answer` directly.

        Parameters
        ----------
        session_id:
            The learning session id.
        decision:
            A decision dict as returned by the policy function (typically from
            :meth:`route`).  Must contain at least ``action``.
        user_input:
            Optional user input dict.  Required for actions that need user
            data (e.g. ``assess_answer``, ``answer_interruption``).

        Returns
        -------
        dict
            Always contains ``executed`` (bool).  On success contains
            ``decision`` and ``result``.  On failure contains ``error`` and
            possibly ``terminal`` or ``requires_clarification`` flags.
        """
        action = decision.get("action", "")

        # 1. teach_current_node — Tutor
        if action == "teach_current_node":
            step = self.next_step(session_id)
            return {
                "executed": True,
                "decision": decision,
                "result": step,
            }

        # 2. answer_interruption — QA
        if action == "answer_interruption":
            question = ""
            if user_input:
                question = user_input.get("text", "") or user_input.get("question", "")
            if not question:
                return {
                    "executed": False,
                    "decision": decision,
                    "error": "answer_interruption requires question text in user_input['text'] or user_input['question']",
                    "result": None,
                }
            qa_result = self.handle_interruption(session_id, question)
            return {
                "executed": True,
                "decision": decision,
                "result": qa_result,
            }

        # 3. assess_answer — Assessment
        if action == "assess_answer":
            if not user_input:
                return {
                    "executed": False,
                    "decision": decision,
                    "error": "assess_answer requires user_input with 'question' (dict) and 'answer' (str)",
                    "result": None,
                }
            question = user_input.get("question")
            answer = user_input.get("answer", "")
            if question is None:
                return {
                    "executed": False,
                    "decision": decision,
                    "error": "assess_answer requires user_input['question'] (dict)",
                    "result": None,
                }
            if not answer:
                return {
                    "executed": False,
                    "decision": decision,
                    "error": "assess_answer requires user_input['answer'] (str)",
                    "result": None,
                }
            assess_result = self.submit_answer(session_id, question, answer)
            return {
                "executed": True,
                "decision": decision,
                "result": assess_result,
            }

        # 4. continue_to_next_section — user explicitly clicks continue
        if action == "continue_to_next_section":
            continue_result = self.continue_to_next_section(session_id)
            return {
                "executed": True,
                "decision": decision,
                "result": continue_result,
            }

        # 5. lesson_completed — terminal state
        if action == "lesson_completed":
            return {
                "executed": False,
                "terminal": True,
                "decision": decision,
                "result": None,
            }

        # 6. unknown_input — clarification needed
        if action == "unknown_input":
            return {
                "executed": False,
                "requires_clarification": True,
                "decision": decision,
                "result": None,
            }

        # 7. Unknown action
        return {
            "executed": False,
            "decision": decision,
            "error": f"Unsupported action: {action!r}",
            "result": None,
        }

    # ------------------------------------------------------------------
    # Continue to next section
    # ------------------------------------------------------------------

    def continue_to_next_section(self, session_id: str) -> dict[str, Any]:
        """Advance to the next outline node, then generate its Tutor step.

        This is the backend counterpart of the user's "continue / next page"
        button.  It does not assess answers, does not answer questions, and
        does not let Python generate teaching content.  It only moves the
        session pointer and delegates teaching to :meth:`next_step`.
        """
        self._wait_for_outline_completion(session_id, timeout_seconds=12.0)
        outline_record = _require_outline(self.memory, session_id)
        outline: dict[str, Any] = outline_record["outline"]
        session = self.memory.get_session(session_id)
        user_id: str = session["user_id"]
        before: str | None = session.get("current_outline_node_id")

        # Clear wrong-answer context when advancing to a new section
        self._last_wrong_answer_context = None

        next_id = _next_node_id(outline, before)
        if next_id is None:
            self.memory.set_current_outline_node(session_id, None)
            progress_event_id = self.memory.record_event(
                session_id=session_id,
                user_id=user_id,
                event_type="progress_updated",
                outline_node_id=before,
                payload={
                    "current_outline_node_id_before": before,
                    "current_outline_node_id_after": None,
                    "action": "continue",
                    "advanced": before is not None,
                    "lesson_completed": True,
                },
            )
            progress_memory_update = self._update_learner_memory_for_event(
                user_id=user_id,
                session_id=session_id,
                event_id=progress_event_id,
                surface="planning",
                context={
                    "outline_node_id": before,
                    "current_outline_node_id_after": None,
                    "action": "continue",
                    "advanced": before is not None,
                    "lesson_completed": True,
                },
            )
            return {
                "current_outline_node_id_before": before,
                "current_outline_node_id_after": None,
                "advanced": before is not None,
                "lesson_completed": True,
                "tutor_step": None,
                "progress_memory_update": progress_memory_update,
            }

        self.memory.set_current_outline_node(session_id, next_id)
        progress_event_id = self.memory.record_event(
            session_id=session_id,
            user_id=user_id,
            event_type="progress_updated",
            outline_node_id=before,
            payload={
                "current_outline_node_id_before": before,
                "current_outline_node_id_after": next_id,
                "action": "continue",
                "advanced": True,
                "lesson_completed": False,
            },
        )
        progress_memory_update = self._update_learner_memory_for_event(
            user_id=user_id,
            session_id=session_id,
            event_id=progress_event_id,
            surface="planning",
            context={
                "outline_node_id": before,
                "current_outline_node_id_after": next_id,
                "action": "continue",
                "advanced": True,
                "lesson_completed": False,
            },
        )
        tutor_step = self.next_step(session_id)
        return {
            "current_outline_node_id_before": before,
            "current_outline_node_id_after": next_id,
            "advanced": True,
            "lesson_completed": False,
            "tutor_step": tutor_step,
            "progress_memory_update": progress_memory_update,
            "handout_cache_hit": tutor_step.get("handout_cache_hit", False),
            "cache_miss_sync_generation": tutor_step.get(
                "cache_miss_sync_generation", False
            ),
            "tutor_cache_hit": tutor_step.get("tutor_cache_hit", False),
            "tutor_cache_miss_sync_generation": tutor_step.get(
                "tutor_cache_miss_sync_generation", False
            ),
            "tutor_generation_source": tutor_step.get(
                "tutor_generation_source", ""
            ),
        }


# ============================================================================
# Policy — pure decision function
# ============================================================================


def decide_next_action(
    state: dict[str, Any],
    user_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce a next-action decision from policy state and optional user input.

    This is a pure rule-based function — no LLM, no DB reads, no side
    effects.  The return dict is consumed by the Orchestrator (or a
    future autonomous scheduler) to dispatch to the right agent.

    Parameters
    ----------
    state:
        A policy state dict as returned by
        :meth:`LearningOrchestrator.get_policy_state`.
    user_input:
        Optional user input dict.  Recognised keys:

        * ``type`` — ``"interruption_question"`` / ``"answer_submission"`` /
          ``"lesson_start"`` etc.

    Returns
    -------
    dict
        Keys: ``schema_version``, ``action_type``, ``action``,
        ``target_agent``, ``payload``, ``reason``,
        ``requires_user_input``, ``advance_progress_allowed``, ``trace``.
    """
    trace = {
        "mode": "rule_based_orchestrator_policy",
        "uses_llm": False,
    }

    # --- user_input present ------------------------------------------------
    if user_input:
        utype = user_input.get("type", "")

        # A. Interruption question
        if utype == "interruption_question":
            return {
                "schema_version": "policy.v1",
                "action_type": "agent_call",
                "action": "answer_interruption",
                "target_agent": "qa",
                "payload": {"input_type": "interruption_question"},
                "reason": "用户中途提问，应进入 QA，且不推进课程进度。",
                "requires_user_input": False,
                "advance_progress_allowed": False,
                "trace": trace,
            }

        # B. Answer submission
        if utype == "answer_submission":
            return {
                "schema_version": "policy.v1",
                "action_type": "agent_call",
                "action": "assess_answer",
                "target_agent": "assessment",
                "payload": {"input_type": "answer_submission"},
                "reason": "用户提交答案，应进入 Assessment，是否推进由评估结果决定。",
                "requires_user_input": False,
                "advance_progress_allowed": True,
                "trace": trace,
            }

        # C. Explicit continue / next-section button
        if utype == "continue":
            return {
                "schema_version": "policy.v1",
                "action_type": "state_transition",
                "action": "continue_to_next_section",
                "target_agent": "orchestrator",
                "payload": {"input_type": "continue"},
                "reason": "用户明确点击继续，应推进到下一节并调用 Tutor 讲解下一节。",
                "requires_user_input": False,
                "advance_progress_allowed": True,
                "trace": trace,
            }

        # E. Unknown input type
        return {
            "schema_version": "policy.v1",
            "action_type": "clarification",
            "action": "unknown_input",
            "target_agent": "orchestrator",
            "payload": {"input_type": utype},
            "reason": "未知输入类型，需要上层澄清。",
            "requires_user_input": True,
            "advance_progress_allowed": False,
            "trace": trace,
        }

    # --- no user_input -----------------------------------------------------

    # C. Active node → teach
    if state.get("current_outline_node_id"):
        return {
            "schema_version": "policy.v1",
            "action_type": "agent_call",
            "action": "teach_current_node",
            "target_agent": "tutor",
            "payload": {},
            "reason": "当前有学习节点，应生成 Tutor step 并等待用户回答。",
            "requires_user_input": True,
            "advance_progress_allowed": False,
            "trace": trace,
        }

    # D. No current node → lesson completed or not started
    return {
        "schema_version": "policy.v1",
        "action_type": "terminal_state",
        "action": "lesson_completed",
        "target_agent": "none",
        "payload": {},
        "reason": "没有当前节点，课程已完成或未开始。",
        "requires_user_input": False,
        "advance_progress_allowed": False,
        "trace": trace,
    }


# ============================================================================
# Internal helpers
# ============================================================================


def _require_outline(memory: LearningMemory, session_id: str) -> dict[str, Any]:
    """Return the latest outline for *session_id*, or raise."""
    rec = memory.latest_outline(session_id)
    if rec is None:
        raise ValueError(f"No outline found for session {session_id}")
    return rec


def _next_node_id(
    outline: dict[str, Any], current_node_id: str | None
) -> str | None:
    """Return the id of the node after *current_node_id*, or *None*."""
    nodes: list[dict[str, Any]] = outline.get("nodes", [])
    if not nodes or current_node_id is None:
        return None
    for i, node in enumerate(nodes):
        if node.get("id") == current_node_id:
            if i + 1 < len(nodes):
                return nodes[i + 1]["id"]
            return None
    return None


def _extract_kg_node_ids_from_section(
    section: dict[str, Any],
    node_evidence: dict[str, Any],
) -> list[str]:
    """Extract KG node ids from a generated handout section.

    Prefers ``used_evidence.kg_node_ids`` from the section, falls back to
    ``node_evidence``.
    """
    ue = section.get("used_evidence", {})
    if isinstance(ue, dict):
        kg_ids = ue.get("kg_node_ids", [])
        if isinstance(kg_ids, list) and kg_ids:
            return [str(x).strip() for x in kg_ids if str(x).strip()]
    # Fallback: node_evidence kg_node_ids
    return list(node_evidence.get("kg_node_ids", []))
