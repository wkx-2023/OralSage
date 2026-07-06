"""Measure first-section latency for the learning interaction.

This script exercises the same streaming endpoint used by the frontend:

    POST /api/learning/session/start/stream

It reports:
* time until the initial outline + first handout section are ready
* extra time until the first Tutor step is ready
* total time-to-first-teachable-content
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_TOPIC = "\u7259\u5468\u708e"
DEFAULT_BASE_URL = "http://127.0.0.1:8086"


@dataclass
class Measurement:
    run: int
    ok: bool
    error: str | None
    session_id: str
    outline_ready_seconds: float | None
    tutor_ready_seconds: float | None
    complete_seconds: float | None
    next_step_seconds: float | None
    strategy: str
    outline_status: str
    outline_completion_started: bool | None
    first_handout_section_ready: bool | None
    outline_node_count: int | None
    first_node_id: str
    first_node_title: str
    tutor_content_status: str
    tutor_trace_mode: str
    handout_cache_hit: bool | None
    handout_cache_status: str
    handout_generation_source: str
    cache_miss_sync_generation: bool | None
    tutor_cache_hit: bool | None
    tutor_cache_miss_sync_generation: bool | None
    tutor_generation_source: str
    question_count: int | None
    events: list[dict[str, Any]]


def _round_seconds(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 3)


def _post_sse(url: str, payload: dict[str, Any], read_timeout: float) -> list[dict[str, Any]]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "text/event-stream",
        },
        method="POST",
    )

    events: list[dict[str, Any]] = []
    with urllib.request.urlopen(request, timeout=read_timeout) as response:
        event_name = "message"
        data_lines: list[str] = []
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                if data_lines:
                    raw_data = "\n".join(data_lines)
                    try:
                        payload_obj = json.loads(raw_data)
                    except json.JSONDecodeError:
                        payload_obj = {"raw": raw_data}
                    if isinstance(payload_obj, dict):
                        payload_obj = {"event": event_name, **payload_obj}
                    else:
                        payload_obj = {"event": event_name, "data": payload_obj}
                    events.append(payload_obj)
                    print(
                        json.dumps(
                            {
                                "event": payload_obj.get("event"),
                                "phase": payload_obj.get("phase"),
                                "elapsed_seconds": payload_obj.get("elapsed_seconds"),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                event_name = "message"
                data_lines = []
                continue
            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].lstrip())
        if data_lines:
            raw_data = "\n".join(data_lines)
            payload_obj = json.loads(raw_data)
            if isinstance(payload_obj, dict):
                payload_obj = {"event": event_name, **payload_obj}
            else:
                payload_obj = {"event": event_name, "data": payload_obj}
            events.append(payload_obj)
    return events


def _event_by_name(events: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for event in events:
        if event.get("event") == name:
            return event
    return {}


def _summarize_run(
    run: int,
    events: list[dict[str, Any]],
    fallback_elapsed: float,
) -> Measurement:
    error_event = _event_by_name(events, "error")
    outline_ready = _event_by_name(events, "outline_ready")
    tutor_ready = _event_by_name(events, "tutor_ready")
    complete = _event_by_name(events, "complete")
    result = complete.get("result") if isinstance(complete.get("result"), dict) else {}
    start = result.get("start") if isinstance(result.get("start"), dict) else {}
    tutor_step = (
        result.get("tutor_step")
        if isinstance(result.get("tutor_step"), dict)
        else {}
    )

    outline_nodes = start.get("outline", {}).get("nodes", []) if start else []
    first_node = outline_nodes[0] if outline_nodes else {}
    source_trace = start.get("source_trace", {}) if start else {}
    tutor_trace = tutor_step.get("trace", {}) if tutor_step else {}

    outline_seconds = outline_ready.get("elapsed_seconds")
    tutor_seconds = tutor_ready.get("elapsed_seconds")
    complete_seconds = complete.get("elapsed_seconds")
    if complete_seconds is None and complete:
        complete_seconds = fallback_elapsed
    next_step_seconds = None
    if isinstance(outline_seconds, (int, float)) and isinstance(tutor_seconds, (int, float)):
        next_step_seconds = float(tutor_seconds) - float(outline_seconds)

    return Measurement(
        run=run,
        ok=not bool(error_event) and bool(complete),
        error=error_event.get("error") if error_event else None,
        session_id=str(
            complete.get("session_id")
            or tutor_ready.get("session_id")
            or outline_ready.get("session_id")
            or ""
        ),
        outline_ready_seconds=_round_seconds(
            float(outline_seconds) if isinstance(outline_seconds, (int, float)) else None
        ),
        tutor_ready_seconds=_round_seconds(
            float(tutor_seconds) if isinstance(tutor_seconds, (int, float)) else None
        ),
        complete_seconds=_round_seconds(
            float(complete_seconds) if isinstance(complete_seconds, (int, float)) else None
        ),
        next_step_seconds=_round_seconds(next_step_seconds),
        strategy=str(source_trace.get("strategy", "")),
        outline_status=str(start.get("outline_status", outline_ready.get("outline_status", ""))),
        outline_completion_started=(
            start.get("outline_completion_started")
            if "outline_completion_started" in start
            else outline_ready.get("outline_completion_started")
        ),
        first_handout_section_ready=(
            start.get("first_handout_section_ready")
            if "first_handout_section_ready" in start
            else outline_ready.get("first_handout_section_ready")
        ),
        outline_node_count=(
            len(outline_nodes)
            if outline_nodes
            else outline_ready.get("outline_node_count")
        ),
        first_node_id=str(first_node.get("id", "")),
        first_node_title=str(first_node.get("title", "")),
        tutor_content_status=str(
            tutor_step.get("content_status", tutor_ready.get("content_status", ""))
        ),
        tutor_trace_mode=str(tutor_trace.get("mode", "")),
        handout_cache_hit=tutor_step.get("handout_cache_hit"),
        handout_cache_status=str(tutor_step.get("handout_cache_status", "")),
        handout_generation_source=str(tutor_step.get("handout_generation_source", "")),
        cache_miss_sync_generation=tutor_step.get("cache_miss_sync_generation"),
        tutor_cache_hit=tutor_step.get("tutor_cache_hit"),
        tutor_cache_miss_sync_generation=tutor_step.get(
            "tutor_cache_miss_sync_generation"
        ),
        tutor_generation_source=str(tutor_step.get("tutor_generation_source", "")),
        question_count=(
            len(tutor_step.get("questions") or [])
            if tutor_step
            else tutor_ready.get("question_count")
        ),
        events=events,
    )


def measure_once(
    *,
    run: int,
    base_url: str,
    topic: str,
    user_id: str,
    display_name: str,
    read_timeout: float,
) -> Measurement:
    url = base_url.rstrip("/") + "/api/learning/session/start/stream"
    payload = {
        "user_id": user_id,
        "topic": topic,
        "display_name": display_name,
    }
    started = time.perf_counter()
    try:
        events = _post_sse(url, payload, read_timeout=read_timeout)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return Measurement(
            run=run,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            session_id="",
            outline_ready_seconds=None,
            tutor_ready_seconds=None,
            complete_seconds=_round_seconds(time.perf_counter() - started),
            next_step_seconds=None,
            strategy="",
            outline_status="",
            outline_completion_started=None,
            first_handout_section_ready=None,
            outline_node_count=None,
            first_node_id="",
            first_node_title="",
            tutor_content_status="",
            tutor_trace_mode="",
            handout_cache_hit=None,
            handout_cache_status="",
            handout_generation_source="",
            cache_miss_sync_generation=None,
            tutor_cache_hit=None,
            tutor_cache_miss_sync_generation=None,
            tutor_generation_source="",
            question_count=None,
            events=[],
        )
    return _summarize_run(run, events, fallback_elapsed=time.perf_counter() - started)


def _mean(values: list[float | None]) -> float | None:
    nums = [float(v) for v in values if isinstance(v, (int, float))]
    if not nums:
        return None
    return round(statistics.mean(nums), 3)


def _minimum(values: list[float | None]) -> float | None:
    nums = [float(v) for v in values if isinstance(v, (int, float))]
    return round(min(nums), 3) if nums else None


def _maximum(values: list[float | None]) -> float | None:
    nums = [float(v) for v in values if isinstance(v, (int, float))]
    return round(max(nums), 3) if nums else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure first-section latency through the learning API stream."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    parser.add_argument("--user-id", default="latency_demo")
    parser.add_argument("--display-name", default="Latency Demo")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--read-timeout", type=float, default=120.0)
    args = parser.parse_args()

    if args.runs < 1:
        parser.error("--runs must be >= 1")

    measurements: list[Measurement] = []
    for index in range(args.runs):
        user_id = args.user_id
        if args.runs > 1:
            user_id = f"{args.user_id}_{index + 1}_{int(time.time())}"
        print(
            json.dumps(
                {
                    "run": index + 1,
                    "status": "started",
                    "base_url": args.base_url,
                    "topic": args.topic,
                    "user_id": user_id,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        measurement = measure_once(
            run=index + 1,
            base_url=args.base_url,
            topic=args.topic,
            user_id=user_id,
            display_name=args.display_name,
            read_timeout=args.read_timeout,
        )
        measurements.append(measurement)
        print(
            json.dumps(
                {
                    "run": measurement.run,
                    "status": "finished",
                    "ok": measurement.ok,
                    "error": measurement.error,
                    "outline_ready_seconds": measurement.outline_ready_seconds,
                    "next_step_seconds": measurement.next_step_seconds,
                    "tutor_ready_seconds": measurement.tutor_ready_seconds,
                    "complete_seconds": measurement.complete_seconds,
                    "strategy": measurement.strategy,
                    "outline_status": measurement.outline_status,
                    "first_handout_section_ready": measurement.first_handout_section_ready,
                    "content_status": measurement.tutor_content_status,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    summary = {
        "ok": all(m.ok for m in measurements),
        "runs": [m.__dict__ for m in measurements],
        "aggregate": {
            "run_count": len(measurements),
            "outline_ready_seconds": {
                "min": _minimum([m.outline_ready_seconds for m in measurements]),
                "avg": _mean([m.outline_ready_seconds for m in measurements]),
                "max": _maximum([m.outline_ready_seconds for m in measurements]),
            },
            "next_step_seconds": {
                "min": _minimum([m.next_step_seconds for m in measurements]),
                "avg": _mean([m.next_step_seconds for m in measurements]),
                "max": _maximum([m.next_step_seconds for m in measurements]),
            },
            "tutor_ready_seconds": {
                "min": _minimum([m.tutor_ready_seconds for m in measurements]),
                "avg": _mean([m.tutor_ready_seconds for m in measurements]),
                "max": _maximum([m.tutor_ready_seconds for m in measurements]),
            },
        },
    }
    print("===SUMMARY_JSON===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
