import json
import threading
import time
from pathlib import Path

import pytest

from omarvis.catalog import catalog_from_data
from omarvis.daemon import (
    ExecutionResult,
    RunToolHandler,
    compact_browser_tabs,
    wait_for_conversation_connection,
)

FIXTURES = Path(__file__).parent / "fixtures"


def omarchy_catalog():
    return catalog_from_data(
        json.loads((FIXTURES / "omarchy-commands.json").read_text())
    )


def test_conversation_connection_waits_for_the_sdk_websocket():
    class Thread:
        @staticmethod
        def is_alive():
            return True

    conversation = type("Conversation", (), {"_ws": object(), "_thread": Thread()})()

    wait_for_conversation_connection(conversation, threading.Event(), timeout=0.01)


def test_conversation_connection_fails_if_the_sdk_thread_exits():
    class Thread:
        @staticmethod
        def is_alive():
            return False

    conversation = type("Conversation", (), {"_ws": None, "_thread": Thread()})()

    with pytest.raises(RuntimeError, match="ended before it became ready"):
        wait_for_conversation_connection(conversation, threading.Event(), timeout=0.01)


def test_run_tool_enforces_a_user_turn_before_confirmation():
    executed = []

    def executor(argv, *, timeout, kill_on_timeout, stdout_limit):
        executed.append(tuple(argv))
        return ExecutionResult(0, "done\n", "")

    handler = RunToolHandler(
        catalog=omarchy_catalog(),
        dispatchers={"workspace", "exit"},
        config={},
        executor=executor,
        clock=lambda: 100.0,
        confirmation_wait=0,
    )

    assert (
        handler.handle({"command": "omarchy system shutdown", "confirmed": True})[
            "status"
        ]
        == "needs_confirmation"
    )
    assert (
        handler.handle({"command": "omarchy system shutdown"})["status"]
        == "needs_confirmation"
    )
    assert executed == []

    handler.note_user_transcript("yes")
    result = handler.handle({"command": "omarchy system shutdown", "confirmed": True})

    assert result == {"status": "ok", "exit_code": 0, "stdout": "done\n", "stderr": ""}
    assert executed == [("omarchy", "system", "shutdown")]


def test_browser_tab_ownership_rewrites_only_navigation_until_omarvis_owns_the_tab():
    executed = []

    def executor(argv, *, timeout, kill_on_timeout, stdout_limit):
        executed.append((tuple(argv), timeout, kill_on_timeout, stdout_limit))
        return ExecutionResult(0, "[]", "")

    config = {
        "agent_browser_path": "/opt/agent-browser",
        "browser_mode": "own-browser",
    }
    handler = RunToolHandler(
        catalog=omarchy_catalog(),
        dispatchers=set(),
        config=config,
        executor=executor,
        confirmation_wait=0,
    )

    assert (
        handler.handle({"command": "agent-browser open https://github.com"})["status"]
        == "ok"
    )
    assert (
        handler.handle({"command": "agent-browser open https://example.com"})["status"]
        == "ok"
    )

    browser_actions = [
        call
        for call in executed
        if "--json" not in call[0] and call[0][-2:] != ("tab", "list")
    ]
    assert browser_actions[0][0][-3:] == ("tab", "new", "https://github.com")
    assert browser_actions[1][0][-2:] == ("open", "https://example.com")
    assert browser_actions[0][1:] == (30.0, True, 3000)

    click_calls = []

    def click_executor(argv, *, timeout, kill_on_timeout, stdout_limit):
        click_calls.append(tuple(argv))
        return ExecutionResult(0, "[]", "")

    fresh_handler = RunToolHandler(
        catalog=omarchy_catalog(),
        dispatchers=set(),
        config=config,
        executor=click_executor,
        confirmation_wait=0,
    )
    assert (
        fresh_handler.handle({"command": "agent-browser click @e1"})["status"] == "ok"
    )
    assert any(call[-2:] == ("click", "@e1") for call in click_calls)


def test_browser_probe_timeout_waits_for_chromium_approval_without_fallback():
    calls = []

    def executor(argv, *, timeout, kill_on_timeout, stdout_limit):
        calls.append(tuple(argv))
        return ExecutionResult(None, "", "", timed_out=True)

    handler = RunToolHandler(
        catalog=omarchy_catalog(),
        dispatchers=set(),
        config={
            "agent_browser_path": "/opt/agent-browser",
            "browser_mode": "own-browser",
        },
        executor=executor,
        confirmation_wait=0,
    )

    assert handler.handle({"command": "agent-browser tab list"}) == {
        "status": "failed",
        "reason": "browser-pending-approval",
    }
    assert calls == [
        (
            "/opt/agent-browser",
            "--session",
            "omarvis",
            "--pin-tab",
            "--idle-timeout",
            "0",
            "--auto-connect",
            "tab",
            "list",
        )
    ]


def test_browser_commands_disable_daemon_idle_shutdown():
    calls = []

    def executor(argv, *, timeout, kill_on_timeout, stdout_limit):
        calls.append(tuple(argv))
        return ExecutionResult(0, "[]", "")

    handler = RunToolHandler(
        catalog=omarchy_catalog(),
        dispatchers=set(),
        config={
            "agent_browser_path": "/opt/agent-browser",
            "browser_mode": "own-browser",
        },
        executor=executor,
        confirmation_wait=0,
    )

    assert handler.handle({"command": "agent-browser tab list"})["status"] == "ok"
    assert all(
        call[1:7]
        == ("--session", "omarvis", "--pin-tab", "--idle-timeout", "0", "--auto-connect")
        for call in calls
    )


def test_browser_tab_context_is_compact_and_omits_target_ids():
    payload = json.dumps(
        {
            "success": True,
            "data": {
                "tabs": [
                    {
                        "id": "t1",
                        "title": "Pull request #1 · eliasstravik/omarvis",
                        "url": "https://github.com/eliasstravik/omarvis/pull/1",
                        "targetId": "4A0B7C4E1F2D3A4B5C6D7E8F90A1B2C3",
                    }
                ]
            },
        }
    )

    assert compact_browser_tabs(payload) == (
        "Browser tabs: t1 Pull request #1 · eliasstravik/omarvis github.com"
    )


def test_browser_snapshot_budget_and_screenshot_path_are_daemon_owned(tmp_path):
    calls = []

    def executor(argv, *, timeout, kill_on_timeout, stdout_limit):
        calls.append((tuple(argv), stdout_limit))
        return ExecutionResult(0, "snapshot", "")

    handler = RunToolHandler(
        catalog=omarchy_catalog(),
        dispatchers=set(),
        config={
            "agent_browser_path": "/opt/agent-browser",
            "browser_mode": "omarvis-browser",
            "cache_dir": str(tmp_path),
        },
        executor=executor,
        confirmation_wait=0,
    )

    assert handler.handle({"command": "agent-browser snapshot"})["status"] == "ok"
    assert handler.handle({"command": "agent-browser screenshot"})["status"] == "ok"

    assert calls[0][1] == 6000
    assert calls[1][0][-2] == "screenshot"
    assert Path(calls[1][0][-1]).parent == tmp_path


def test_herdr_json_results_are_compacted_before_returning_to_the_agent():
    payload = (FIXTURES / "herdr-agent-list.json").read_text()
    limits = []

    def executor(argv, *, timeout, kill_on_timeout, stdout_limit):
        limits.append(stdout_limit)
        return ExecutionResult(0, payload, "")

    handler = RunToolHandler(
        catalog=omarchy_catalog(),
        dispatchers=set(),
        config={},
        executor=executor,
        confirmation_wait=0,
    )

    result = handler.handle({"command": "herdr agent list"})

    assert result["status"] == "ok"
    assert result["stdout"] == (
        "w58:p5 codex idle name=reviewer cwd=~/dev/gtm-skills\n"
        "w58:p6 claude blocked cwd=~/dev/omarvis"
    )
    assert len(result["stdout"]) <= 600
    assert limits == [64_000]


def test_hyprland_client_json_is_not_truncated_before_compaction():
    clients = [
        {
            "class": f"app-{index}",
            "title": "x" * 200,
            "workspace": {"id": index},
        }
        for index in range(20)
    ]
    limits = []

    def executor(argv, *, timeout, kill_on_timeout, stdout_limit):
        limits.append(stdout_limit)
        return ExecutionResult(0, json.dumps(clients), "")

    handler = RunToolHandler(
        catalog=omarchy_catalog(),
        dispatchers=set(),
        config={},
        executor=executor,
        confirmation_wait=0,
    )

    result = handler.handle({"command": "hyprctl clients -j"})

    assert result["status"] == "ok"
    assert result["stdout"].startswith("app-0 — ")
    assert not result["stdout"].startswith("[")
    assert limits == [64_000]


def test_herdr_create_response_keeps_resource_ids_parseable():
    payload = json.dumps(
        {
            "id": "cli:tab:create",
            "result": {
                "root_pane": {
                    "pane_id": "w1:p2",
                    "tab_id": "w1:t2",
                    "workspace_id": "w1",
                    "cwd": "/home/example/project",
                    "terminal_id": "term_example",
                    "agent_status": "unknown",
                    "focused": False,
                    "foreground_cwd": "/home/example/project",
                    "revision": 0,
                    "scroll": {
                        "max_offset_from_bottom": 0,
                        "offset_from_bottom": 0,
                        "viewport_rows": 50,
                    },
                },
                "tab": {
                    "tab_id": "w1:t2",
                    "workspace_id": "w1",
                    "label": "omarvis-e2e",
                    "number": 2,
                    "pane_count": 1,
                    "focused": False,
                    "agent_status": "unknown",
                },
                "type": "tab_created",
            },
        }
    )
    limits = []

    def executor(argv, *, timeout, kill_on_timeout, stdout_limit):
        limits.append(stdout_limit)
        return ExecutionResult(0, payload, "")

    handler = RunToolHandler(
        catalog=omarchy_catalog(),
        dispatchers=set(),
        config={},
        executor=executor,
        confirmation_wait=0,
    )

    result = handler.handle(
        {"command": "herdr tab create --workspace w1 --label omarvis-e2e --no-focus"}
    )

    decoded = json.loads(result["stdout"])
    assert decoded["root_pane"]["pane_id"] == "w1:p2"
    assert decoded["tab"]["tab_id"] == "w1:t2"
    assert limits == [64_000]


def _wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        time.sleep(0.01)
    return predicate()


@pytest.mark.parametrize(
    "command, result",
    [
        ("hyprctl dispatch workspace 2", ExecutionResult(0, "", "")),
        ("omarchy launch browser", ExecutionResult(None, started=True)),
    ],
)
def test_desktop_mutations_push_a_state_refresh_to_conversation_context(
    command, result
):
    updates = []

    def executor(argv, *, timeout, kill_on_timeout, stdout_limit):
        return result

    handler = RunToolHandler(
        catalog=omarchy_catalog(),
        dispatchers={"workspace"},
        config={},
        executor=executor,
        confirmation_wait=0,
        context_sink=updates.append,
        state_provider=lambda: "Desktop: fresh",
        state_refresh_delay=0.0,
    )

    assert handler.handle({"command": command})["status"] in {"ok", "started"}
    assert _wait_for(lambda: updates)
    assert updates == ["Desktop: fresh"]


def test_read_only_commands_do_not_trigger_a_state_refresh():
    updates = []

    def executor(argv, *, timeout, kill_on_timeout, stdout_limit):
        stdout = '{"result":{"agents":[]}}' if argv[0] == "herdr" else "[]"
        return ExecutionResult(0, stdout, "")

    handler = RunToolHandler(
        catalog=omarchy_catalog(),
        dispatchers=set(),
        config={},
        executor=executor,
        confirmation_wait=0,
        context_sink=updates.append,
        state_provider=lambda: "Desktop: fresh",
        state_refresh_delay=0.0,
    )

    assert handler.handle({"command": "hyprctl clients -j"})["status"] == "ok"
    assert handler.handle({"command": "herdr agent list"})["status"] == "ok"
    assert not _wait_for(lambda: updates, timeout=0.1)


LUA_VERSION = "Hyprland, built from branch main at commit abc123  (tag: v0.55.1)\n"
LEGACY_VERSION = "Hyprland, built from branch main at commit abc123  (tag: v0.48.1)\n"


def _dispatch_handler(executor):
    return RunToolHandler(
        catalog=omarchy_catalog(),
        dispatchers={"workspace", "closewindow"},
        config={},
        executor=executor,
        confirmation_wait=0,
    )


def test_dispatches_are_translated_to_lua_on_hyprland_055():
    calls = []

    def executor(argv, *, timeout, kill_on_timeout, stdout_limit):
        calls.append(tuple(argv))
        if tuple(argv) == ("hyprctl", "version"):
            return ExecutionResult(0, LUA_VERSION, "")
        return ExecutionResult(0, "ok", "")

    handler = _dispatch_handler(executor)

    result = handler.handle({"command": "hyprctl dispatch closewindow class:chromium"})

    assert result["status"] == "ok"
    assert calls == [
        ("hyprctl", "version"),
        (
            "hyprctl",
            "dispatch",
            'hl.dsp.window.close({ window = "class:(?i)chromium" })',
        ),
    ]


def test_hyprland_version_is_probed_once_per_session():
    calls = []

    def executor(argv, *, timeout, kill_on_timeout, stdout_limit):
        calls.append(tuple(argv))
        if tuple(argv) == ("hyprctl", "version"):
            return ExecutionResult(0, LUA_VERSION, "")
        return ExecutionResult(0, "ok", "")

    handler = _dispatch_handler(executor)

    handler.handle({"command": "hyprctl dispatch workspace 2"})
    handler.handle({"command": "hyprctl dispatch workspace 3"})

    assert calls.count(("hyprctl", "version")) == 1


def test_dispatches_stay_legacy_on_older_hyprland():
    calls = []

    def executor(argv, *, timeout, kill_on_timeout, stdout_limit):
        calls.append(tuple(argv))
        if tuple(argv) == ("hyprctl", "version"):
            return ExecutionResult(0, LEGACY_VERSION, "")
        return ExecutionResult(0, "ok", "")

    handler = _dispatch_handler(executor)

    result = handler.handle({"command": "hyprctl dispatch workspace 2"})

    assert result["status"] == "ok"
    assert ("hyprctl", "dispatch", "workspace", "2") in calls


def test_dispatch_error_reply_with_exit_zero_is_reported_as_failure():
    def executor(argv, *, timeout, kill_on_timeout, stdout_limit):
        if tuple(argv) == ("hyprctl", "version"):
            return ExecutionResult(0, LUA_VERSION, "")
        return ExecutionResult(
            0,
            "error: return hl.dispatch(exit):1: hl.dispatch: expected a dispatcher",
            "",
        )

    handler = _dispatch_handler(executor)

    result = handler.handle({"command": "hyprctl dispatch closewindow class:chromium"})

    assert result["status"] == "failed"
    assert "expected a dispatcher" in result["stdout"]


def test_failed_dispatches_return_cleaned_error_and_desktop_state():
    def executor(argv, *, timeout, kill_on_timeout, stdout_limit):
        if tuple(argv) == ("hyprctl", "version"):
            return ExecutionResult(0, LUA_VERSION, "")
        return ExecutionResult(
            0,
            'WARN: [string "return hl.dispatch(hl.dsp.window.close({ window = "cl..."]:1: '
            "hl.window.close: window not found",
            "",
        )

    handler = RunToolHandler(
        catalog=omarchy_catalog(),
        dispatchers={"closewindow"},
        config={},
        executor=executor,
        confirmation_wait=0,
        state_provider=lambda: "Desktop: workspace 1; focused foot; windows: chromium — GitHub (ws 1)",
    )

    result = handler.handle({"command": "hyprctl dispatch closewindow class:Cromium"})

    assert result["status"] == "failed"
    assert result["stdout"] == "WARN: hl.window.close: window not found"
    assert '[string "' not in result["stdout"]
    assert result["desktop"] == (
        "Desktop: workspace 1; focused foot; windows: chromium — GitHub (ws 1)"
    )


def test_successful_dispatches_do_not_include_desktop_state_inline():
    def executor(argv, *, timeout, kill_on_timeout, stdout_limit):
        if tuple(argv) == ("hyprctl", "version"):
            return ExecutionResult(0, LUA_VERSION, "")
        return ExecutionResult(0, "ok", "")

    handler = RunToolHandler(
        catalog=omarchy_catalog(),
        dispatchers={"closewindow"},
        config={},
        executor=executor,
        confirmation_wait=0,
        state_provider=lambda: "Desktop: fresh",
        state_refresh_delay=0.0,
    )

    result = handler.handle({"command": "hyprctl dispatch closewindow class:chromium"})

    assert result["status"] == "ok"
    assert "desktop" not in result


def test_failed_dispatches_do_not_push_a_state_refresh():
    updates = []

    def executor(argv, *, timeout, kill_on_timeout, stdout_limit):
        if tuple(argv) == ("hyprctl", "version"):
            return ExecutionResult(0, LUA_VERSION, "")
        return ExecutionResult(0, "error: no such window", "")

    handler = RunToolHandler(
        catalog=omarchy_catalog(),
        dispatchers={"closewindow"},
        config={},
        executor=executor,
        confirmation_wait=0,
        context_sink=updates.append,
        state_provider=lambda: "Desktop: fresh",
        state_refresh_delay=0.0,
    )

    assert (
        handler.handle({"command": "hyprctl dispatch closewindow class:foo"})["status"]
        == "failed"
    )
    assert not _wait_for(lambda: updates, timeout=0.1)


def test_hyprctl_clients_results_are_compacted_before_returning_to_the_agent():
    payload = (
        '[{"class":"firefox","title":"GitHub","workspace":{"id":3}},'
        '{"class":"foot","title":"tests","workspace":{"id":1}}]'
    )

    def executor(argv, *, timeout, kill_on_timeout, stdout_limit):
        return ExecutionResult(0, payload, "")

    handler = RunToolHandler(
        catalog=omarchy_catalog(),
        dispatchers=set(),
        config={},
        executor=executor,
        confirmation_wait=0,
    )

    result = handler.handle({"command": "hyprctl clients -j"})

    assert result["status"] == "ok"
    assert result["stdout"] == "firefox — GitHub (ws 3)\nfoot — tests (ws 1)"


def test_client_tool_result_is_serialized_for_the_elevenlabs_protocol():
    def executor(argv, *, timeout, kill_on_timeout, stdout_limit):
        return ExecutionResult(None, started=True)

    handler = RunToolHandler(
        catalog=omarchy_catalog(),
        dispatchers=set(),
        config={},
        executor=executor,
        confirmation_wait=0,
    )

    result = handler.handle_client_tool(
        {"command": "omarchy launch terminal herdr"}
    )

    assert isinstance(result, str)
    assert json.loads(result) == {
        "status": "started",
        "command": "omarchy launch terminal herdr",
    }
