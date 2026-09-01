import json

from omarvis.simulate import run_simulation


def simulated_lines(*, include_error=False):
    lines = []
    run_simulation(
        lambda event: lines.append(json.dumps(dict(event))),
        delay_scale=0,
        include_error=include_error,
    )
    return lines


def test_simulation_is_json_and_covers_the_event_vocabulary():
    events = [json.loads(line) for line in simulated_lines()]

    assert events[0] == {"event": "state", "state": "starting"}
    assert events[-1] == {"event": "state", "state": "idle"}
    assert {event["event"] for event in events} >= {
        "state",
        "level",
        "user",
        "agent_part",
        "agent",
        "running",
        "ran",
        "dictation",
    }
    assert any(event.get("state") == "thinking" for event in events)


def test_simulation_error_is_opt_in():
    normal = [json.loads(line) for line in simulated_lines()]
    failure = [json.loads(line) for line in simulated_lines(include_error=True)]

    assert not any(event["event"] == "error" for event in normal)
    assert any(event["event"] == "error" for event in failure)
    assert failure[-1] == {"event": "state", "state": "idle"}
