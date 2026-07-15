"""Direct QA persistence adapter.

Single write-boundary between the Direct QA service and LearningMemory /
user_model.  Consumes an already-produced ``qa_result``, updates the user
profile via the existing ``update_profile_from_qa`` pure function, and writes
``source=direct_qa`` mastery records for every valid confusion KG node.

Usage::

    from backend.services.qa.direct_qa_persistence import update_profile_from_qa
    from backend.services.learning.memory import LearningMemory

    memory = LearningMemory()
    profile = memory.get_or_create_profile(user_id)
    result = update_profile_from_qa(
        memory=memory,
        user_id=user_id,
        profile=profile,
        qa_result={
            "should_reinforce": True,
            "confusion_kg_node_ids": ["kg-pulp-periapical"],
            "question": "牙髓炎与根尖周炎如何鉴别？",
            ...
        },
        source_ref="dqa-event-001",
    )
    # result["profile"] is the persisted profile
    # result["mastery_records"] lists every written direct_qa_mastery_records row
"""

from __future__ import annotations

from typing import Any

from backend.services.learning.memory import LearningMemory
from backend.services.learning.user_model import update_profile_from_qa as _update_profile


def _filter_valid_kg_ids(raw: Any) -> list[str]:
    """Return deduplicated, non-empty, stripped KG node id strings."""
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for item in raw:
        text = str(item).strip() if item else ""
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def update_profile_from_qa(
    memory: LearningMemory,
    user_id: str,
    profile: dict[str, Any],
    qa_result: dict[str, Any],
    source_ref: str = "",
    outline_node_id: str = "",
) -> dict[str, Any]:
    """Persist a Direct QA interaction into the user profile and mastery records.

    **Only** writes when *should_reinforce* is the Python boolean ``True``
    (strict — truthy values like ``1`` or ``"true"`` are ignored) **and**
    at least one valid non-empty ``confusion_kg_node_ids`` entry exists.

    For every valid KG node id the adapter:

    * calls the existing ``user_model.update_profile_from_qa`` to update
      ``known_weak_points`` / ``mistake_patterns`` on the profile dict,
    * writes one row to ``direct_qa_mastery_records`` with
      ``mastery_delta=-0.03``, ``source='direct_qa'``, and full evidence,
    * saves the updated profile back to the database.

    Normal expansion questions (``should_reinforce=False`` or no valid
    confusion KG ids) produce **no** profile or mastery changes.

    Parameters
    ----------
    memory:
        A :class:`LearningMemory` instance.  The adapter writes through it
        only — no direct SQLite access.
    user_id:
        The user id.
    profile:
        The current user profile dict (as returned by
        ``memory.get_or_create_profile``).
    qa_result:
        A dict produced by the Direct QA service containing at least:

        * ``should_reinforce``: ``bool`` — ``True`` when the QA interaction
          exposed a genuine knowledge gap.
        * ``confusion_kg_node_ids``: ``list[str]`` — KG node ids that the
          answer model identified as weak points.
        * ``question`` (optional): the original user question.
        * ``resolved_question`` (optional): the disambiguated question.
        * ``used_evidence`` (optional): list of KAG evidence chunks.

        Additional keys are forwarded to the profile-update function and
        included in the mastery evidence payload.
    source_ref:
        Opaque reference for upstream traceability (e.g. a Direct QA
        conversation / event id).  Optional.
    outline_node_id:
        Passed through to :func:`user_model.update_profile_from_qa`.
        Optional — Direct QA has no outline, so this defaults to ``""``.

    Returns
    -------
    dict
        ``profile``
            The updated (and persisted) profile dict.
        ``mastery_records``
            List of written ``direct_qa_mastery_records`` dicts (empty when
            no update occurred).
        ``profile_updated``
            ``bool`` — ``True`` when the profile was modified and saved.
        ``confusion_kg_node_ids_processed``
            The deduplicated, validated KG node ids that were actually
            processed.
        ``trace``
            A human-readable summary of what happened, suitable for
            logging or debugging.
    """
    should_reinforce = qa_result.get("should_reinforce")
    confusion_kg_raw = qa_result.get("confusion_kg_node_ids", [])

    valid_kg_ids = _filter_valid_kg_ids(confusion_kg_raw)

    # ------------------------------------------------------------------
    # Guard: only act on strict boolean True + valid KG ids
    # ------------------------------------------------------------------
    if not (should_reinforce is True and valid_kg_ids):
        return {
            "profile": profile,
            "mastery_records": [],
            "profile_updated": False,
            "confusion_kg_node_ids_processed": [],
            "trace": (
                "noop: should_reinforce is not strictly True"
                if should_reinforce is not True
                else "noop: no valid confusion_kg_node_ids"
            ),
        }

    # ------------------------------------------------------------------
    # 1. Update profile (pure — no DB access)
    # ------------------------------------------------------------------
    updated_profile = _update_profile(profile, qa_result, outline_node_id)

    # ------------------------------------------------------------------
    # 2. Write one Direct QA mastery record per valid KG node (-0.03)
    # ------------------------------------------------------------------
    question_text = str(qa_result.get("question", "")).strip()
    resolved_question = str(qa_result.get("resolved_question", "")).strip()
    used_evidence = qa_result.get("used_evidence", [])

    mastery_records: list[dict[str, Any]] = []
    for kg_id in valid_kg_ids:
        concept_name = f"Direct QA confusion: {kg_id}"
        evidence: dict[str, Any] = {
            "event": "direct_qa_confusion_signal",
            "question": question_text,
            "resolved_question": resolved_question,
            "confusion_kg_node_ids": valid_kg_ids,
            "used_evidence": used_evidence,
        }
        try:
            rec = memory.upsert_direct_qa_mastery(
                user_id=user_id,
                concept_name=concept_name,
                mastery_delta=-0.03,
                kg_node_id=kg_id,
                evidence=evidence,
                source_ref=source_ref,
            )
            mastery_records.append(rec)
        except Exception:
            # Non-fatal: mastery write failure must not block the answer
            # or profile update.
            pass

    # ------------------------------------------------------------------
    # 3. Persist the updated profile
    # ------------------------------------------------------------------
    try:
        persisted_profile = memory.save_profile(user_id, updated_profile)
    except Exception:
        # Profile save failure: return the in-memory updated profile so
        # the caller can decide what to do.  Mastery records were already
        # written (or at least attempted).
        return {
            "profile": updated_profile,
            "mastery_records": mastery_records,
            "profile_updated": True,
            "confusion_kg_node_ids_processed": valid_kg_ids,
            "trace": (
                f"partial: {len(mastery_records)} mastery records written, "
                "profile save failed"
            ),
        }

    return {
        "profile": persisted_profile,
        "mastery_records": mastery_records,
        "profile_updated": True,
        "confusion_kg_node_ids_processed": valid_kg_ids,
        "trace": (
            f"ok: profile updated, {len(mastery_records)} mastery records "
            f"written for {len(valid_kg_ids)} KG node(s)"
        ),
    }
