import json
from pathlib import Path

import pytest

from omarvis.catalog import catalog_from_data
from omarvis.policy import PendingConfirmation, decide

FIXTURES = Path(__file__).parent / "fixtures"


def omarchy_catalog():
    return catalog_from_data(
        json.loads((FIXTURES / "omarchy-commands.json").read_text())
    )


def test_known_omarchy_route_runs():
    decision = decide("omarchy theme set tokyo-night", catalog=omarchy_catalog())

    assert decision.kind == "run"
    assert decision.argv == ("omarchy", "theme", "set", "tokyo-night")


@pytest.mark.parametrize(
    "command, reason",
    [
        ('omarchy reminder 15 "unfinished', "invalid quoting"),
        ("omarchy theme set x\n", "control character"),
        ("omarchy theme set x\r", "control character"),
        ("omarchy theme set x\0", "control character"),
        ("x" * 2001, "command too long"),
        ("bash -c ls", "unsupported command"),
        ("omarchy nonexistent thing", "unknown route"),
    ],
)
def test_malformed_or_unknown_commands_are_rejected(command, reason):
    decision = decide(command, catalog=omarchy_catalog())

    assert decision.kind == "reject"
    assert decision.reason == reason


@pytest.mark.parametrize(
    "command",
    [
        "omarchy system shutdown",
        "omarchy system reboot",
        "omarchy system logout",
        "omarchy hyprland window close all",
        "omarchy plugin add https://x --enable --yes",
        "omarchy plugin clone omarchy.clock",
        "omarchy plugin remove acme.thing",
        "omarchy plugin update acme.thing --yes",
        "omarchy plugin enable acme.thing",
        "omarchy bar set '{\"right\":[]}'",
        "omarchy bar move acme.thing right",
        "omarchy shell shell setPluginEnabled acme.thing true",
        "omarchy shell -q shell setPluginEnabled acme.thing true",
        "omarchy shell shell enablePlugin acme.thing '{}'",
        "omarchy pkg add portaudio",
        "omarchy pkg aur add example",
        "omarchy pkg remove example",
        "omarchy theme install https://example.com/theme.git",
        "omarchy theme remove tokyo-night",
        "omarchy install browser chromium",
        "omarchy remove browser chromium",
        "omarchy reinstall configs",
        "omarchy refresh shell",
        "omarchy update",
    ],
)
def test_dangerous_omarchy_routes_require_a_fresh_user_confirmation(command):
    catalog = omarchy_catalog()
    first = decide(command, catalog=catalog, now=100.0)
    normalized = tuple(__import__("shlex").split(command))

    assert first.kind == "confirm"
    assert decide(command, catalog=catalog, confirmed=True, now=101.0).kind == "confirm"
    assert (
        decide(
            command,
            catalog=catalog,
            confirmed=True,
            pending=PendingConfirmation(normalized, 100.0, 0),
            now=101.0,
        ).kind
        == "confirm"
    )
    assert (
        decide(
            command,
            catalog=catalog,
            confirmed=True,
            pending=PendingConfirmation(normalized, 100.0, 1),
            now=101.0,
        ).kind
        == "run"
    )
    assert (
        decide(
            command,
            catalog=catalog,
            confirmed=True,
            pending=PendingConfirmation(normalized, 60.0, 1),
            now=101.0,
        ).kind
        == "confirm"
    )


def test_hyprland_dispatchers_are_allowlisted_and_exit_requires_confirmation():
    catalog = omarchy_catalog()
    dispatchers = {"workspace", "focuswindow", "exit"}

    assert (
        decide(
            "hyprctl dispatch workspace 3",
            catalog=catalog,
            dispatchers=dispatchers,
        ).kind
        == "run"
    )
    assert (
        decide(
            "hyprctl dispatch focuswindow class:firefox",
            catalog=catalog,
            dispatchers=dispatchers,
        ).kind
        == "run"
    )
    assert (
        decide(
            "hyprctl dispatch exec kitty",
            catalog=catalog,
            dispatchers=dispatchers,
        ).kind
        == "reject"
    )
    assert (
        decide(
            "hyprctl dispatch exit",
            catalog=catalog,
            dispatchers=dispatchers,
            now=100.0,
        ).kind
        == "confirm"
    )
    assert (
        decide(
            "hyprctl dispatch exit",
            catalog=catalog,
            dispatchers=dispatchers,
            confirmed=True,
            pending=PendingConfirmation(("hyprctl", "dispatch", "exit"), 99.0, 1),
            now=100.0,
        ).kind
        == "run"
    )


@pytest.mark.parametrize(
    "command",
    [
        "hyprctl clients -j",
        "hyprctl activewindow -j",
        "hyprctl activeworkspace -j",
    ],
)
def test_read_only_hyprctl_state_queries_run_without_a_dispatcher(command):
    assert decide(command, catalog=omarchy_catalog()).kind == "run"


@pytest.mark.parametrize(
    "command",
    [
        "hyprctl clients",
        "hyprctl clients -j --verbose",
        "hyprctl keyword general:gaps_in 0",
        "hyprctl monitors -j",
    ],
)
def test_hyprctl_outside_reads_and_allowed_dispatchers_is_rejected(command):
    assert decide(command, catalog=omarchy_catalog()).kind == "reject"


@pytest.mark.parametrize(
    "command",
    [
        "herdr agent list",
        'herdr agent prompt w58:p5 "run the tests and fix $FAILURES"',
        "herdr pane split --current --direction right --no-focus",
        "herdr agent send-keys reviewer esc",
        "herdr pane send-keys w58:p5 enter",
        "herdr workspace focus w58",
        "herdr notification show Omarvis done",
        "herdr api snapshot",
        "herdr status",
    ],
)
def test_approved_herdr_commands_run(command):
    assert decide(command, catalog=omarchy_catalog()).kind == "run"


@pytest.mark.parametrize(
    "command",
    [
        'herdr pane run w1:p1 "just test"',
        "herdr pane close w1:p1",
        "herdr pane send-text w1:p1 hello",
        "herdr tab close w1:t1",
        "herdr workspace close w1",
        "herdr session stop main",
        "herdr server reload-config",
        "herdr worktree list",
        "herdr agent send-keys reviewer ctrl+c",
    ],
)
def test_sensitive_herdr_commands_require_confirmation(command):
    assert decide(command, catalog=omarchy_catalog()).kind == "confirm"


@pytest.mark.parametrize(
    "command",
    [
        "herdr",
        "herdr update",
        "herdr agent prompt reviewer x --wait",
        "herdr agent prompt reviewer x --until idle",
        "herdr agent wait reviewer",
        "herdr session attach main",
        "herdr pane wait-output w1:p1 done",
        "herdr pane release-agent w1:p1",
        "herdr workspace report-metadata w1",
        "herdr server update-agent-manifests",
        "herdr --remote host agent list",
    ],
)
def test_unapproved_or_blocking_herdr_commands_are_rejected(command):
    assert decide(command, catalog=omarchy_catalog()).kind == "reject"


@pytest.mark.parametrize(
    "command",
    [
        "agent-browser open https://github.com",
        'agent-browser find text "Sign in" click',
        "agent-browser click @e7",
        'agent-browser fill @e3 "omarchy plugins"',
        'agent-browser fill @e1 "--weird query"',
        "agent-browser press Enter",
        "agent-browser tab list",
        "agent-browser tab new https://example.com",
        "agent-browser tab close",
        "agent-browser tab t2",
        "agent-browser tab 4A0B7C4E1F2D3A4B5C6D7E8F90A1B2C3",
        "agent-browser snapshot",
        "agent-browser snapshot -i -d 6",
        "agent-browser screenshot",
        "agent-browser wait 20000",
        "agent-browser get title",
        'agent-browser keyboard type "hello world"',
    ],
)
def test_approved_browser_commands_run(command):
    assert decide(command, catalog=omarchy_catalog()).kind == "run"


@pytest.mark.parametrize(
    "command",
    [
        "agent-browser close --all",
        "agent-browser close",
        "agent-browser download @e2 /tmp/x",
        "agent-browser upload @e1 /etc/passwd",
        "agent-browser drag @e1 @e2",
    ],
)
def test_sensitive_browser_commands_require_confirmation(command):
    assert decide(command, catalog=omarchy_catalog()).kind == "confirm"


@pytest.mark.parametrize(
    "command",
    [
        'agent-browser eval "document.cookie"',
        "agent-browser connect 9222",
        "agent-browser --cdp 9222 open x",
        "agent-browser open x --profile foo",
        "agent-browser open x --executable-path /tmp/evil",
        "agent-browser open x --args --remote-debugging-port=9222",
        "agent-browser snapshot --json",
        "agent-browser snapshot -s body",
        "agent-browser session list",
        "agent-browser install",
        "agent-browser",
        "agent-browser -p browserbase open x",
        "agent-browser -q tab list",
        "agent-browser screenshot /home/user/.bashrc",
        "agent-browser tab switch t2",
        "agent-browser get cdp-url",
        "agent-browser wait 20001",
        "agent-browser keyboard inserttext hello",
    ],
)
def test_unapproved_browser_commands_and_flags_are_rejected(command):
    assert decide(command, catalog=omarchy_catalog()).kind == "reject"
