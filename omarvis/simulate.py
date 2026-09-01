from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any


BASE_TIMELINE: list[tuple[float, dict[str, Any]]] = [
    (0.0, {"event": "state", "state": "starting"}),
    (1.0, {"event": "state", "state": "listening"}),
    (0.25, {"event": "level", "in": 0.0, "out": 0.0}),
    (0.25, {"event": "level", "in": 0.2, "out": 0.0}),
    (0.25, {"event": "level", "in": 0.5, "out": 0.0}),
    (0.25, {"event": "level", "in": 0.8, "out": 0.0}),
    (0.25, {"event": "level", "in": 0.5, "out": 0.0}),
    (0.25, {"event": "level", "in": 0.2, "out": 0.0}),
    (0.25, {"event": "level", "in": 0.0, "out": 0.0}),
    (0.5, {"event": "user", "text": "Show me the current workspace"}),
    (0.0, {"event": "state", "state": "thinking"}),
    (1.75, {"event": "state", "state": "speaking"}),
    (0.0, {"event": "agent_part", "text": "You are ", "type": "delta"}),
    (0.4, {"event": "agent_part", "text": "on workspace 3.", "type": "delta"}),
    (0.25, {"event": "level", "in": 0.0, "out": 0.15}),
    (0.25, {"event": "level", "in": 0.0, "out": 0.45}),
    (0.25, {"event": "level", "in": 0.0, "out": 0.8}),
    (0.25, {"event": "level", "in": 0.0, "out": 0.4}),
    (0.25, {"event": "level", "in": 0.0, "out": 0.0}),
    (0.4, {"event": "running", "command": "omarchy-workspace-switch 3"}),
    (2.0, {"event": "ran", "command": "omarchy-workspace-switch 3"}),
    (0.5, {"event": "agent", "text": "You are on workspace 3."}),
    (0.75, {"event": "user", "text": "Dictate a short note"}),
    (0.0, {"event": "state", "state": "thinking"}),
    (1.5, {"event": "state", "state": "speaking"}),
    (0.0, {"event": "agent_part", "text": "Ready.", "type": "delta"}),
    (0.5, {"event": "level", "in": 0.0, "out": 0.6}),
    (0.5, {"event": "level", "in": 0.0, "out": 0.0}),
    (0.5, {"event": "agent", "text": "Ready."}),
    (0.5, {"event": "state", "state": "idle"}),
    (0.5, {"event": "dictation", "state": "recording", "level": 0.15}),
    (0.25, {"event": "dictation", "state": "recording", "level": 0.7}),
    (0.25, {"event": "dictation", "state": "recording", "level": 0.0}),
    (0.75, {"event": "dictation", "state": "transcribing"}),
    (1.0, {"event": "dictation", "state": "idle", "text": "A short note"}),
]


def timeline(*, include_error: bool = False) -> list[tuple[float, dict[str, Any]]]:
    events = [(delay, dict(event)) for delay, event in BASE_TIMELINE]
    if include_error:
        events.extend(
            [
                (0.5, {"event": "state", "state": "starting"}),
                (0.75, {"event": "error", "message": "Simulated microphone failure"}),
                (2.0, {"event": "state", "state": "idle"}),
            ]
        )
    elif events[-1][1].get("event") != "state":
        events.append((0.5, {"event": "state", "state": "idle"}))
    return events


def run_simulation(
    sink: Callable[[Mapping[str, Any]], None],
    *,
    delay_scale: float = 1.0,
    include_error: bool = False,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Emit the deterministic HUD development timeline."""
    for delay, event in timeline(include_error=include_error):
        if delay and delay_scale:
            sleeper(delay * delay_scale)
        sink(event)
