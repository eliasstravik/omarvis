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
        "herdr agent send-keys reviewer ctrl+c",
    ],
)
def test_sensitive_herdr_commands_require_confirmation(command):
    assert decide(command, catalog=omarchy_catalog()).kind == "confirm"


def test_herdr_worktree_list_is_immediate_read_only():
    assert decide("herdr worktree list", catalog=omarchy_catalog()).kind == "run"


def test_session_category_approval_skips_repeatable_but_not_permanent_risks():
    catalog = omarchy_catalog()

    assert (
        decide(
            'herdr pane run w1:p1 "pytest"',
            catalog=catalog,
            approved_categories={"herdr:pane"},
        ).kind
        == "run"
    )
    assert (
        decide(
            "herdr pane close w1:p1",
            catalog=catalog,
            approved_categories={"herdr:pane"},
        ).kind
        == "confirm"
    )
    assert (
        decide(
            "herdr session stop main",
            catalog=catalog,
            approved_categories={"herdr:session"},
        ).kind
        == "confirm"
    )
    assert (
        decide(
            "omarchy system shutdown",
            catalog=catalog,
            approved_categories={"omarchy:system"},
        ).kind
        == "confirm"
    )


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


READ_ONLY_COMMANDS = [
    "hyprctl clients -j",
    "hyprctl activewindow -j",
    "hyprctl activeworkspace -j",
    "herdr agent list",
    "herdr agent get reviewer",
    "herdr agent read reviewer",
    "herdr agent explain reviewer",
    "herdr pane list",
    "herdr pane get w1:p1",
    "herdr pane read w1:p1",
    "herdr pane process-info w1:p1",
    "herdr pane neighbor w1:p1 left",
    "herdr pane edges w1:p1",
    "herdr tab list",
    "herdr tab get w1:t1",
    "herdr workspace list",
    "herdr workspace get w1",
    "herdr session list",
    "herdr worktree list",
    "herdr api snapshot",
    "herdr status",
    "agent-browser snapshot",
    "agent-browser snapshot -i -d 4",
    "agent-browser tab list",
    "agent-browser screenshot",
    "agent-browser scroll down 400",
    "agent-browser scrollintoview @e3",
    *(f"agent-browser get {verb}" for verb in (
        "text", "html", "value", "attr", "title", "url", "count", "box", "styles"
    )),
    *(f"agent-browser is {verb}" for verb in ("visible", "enabled", "checked")),
    "omarchy capture screenshot",
    "omarchy screenshot",
    "omarchy theme current",
    "omarchy theme list",
    "omarchy system stats",
]


@pytest.mark.parametrize("command", READ_ONLY_COMMANDS)
def test_read_only_routes_run_without_confirmation(command):
    decision = decide(command, catalog=omarchy_catalog())

    assert decision.kind == "run"


def test_screen_reading_is_forced_through_elevenlabs():
    decision = decide("omarchy capture text", catalog=omarchy_catalog())

    assert decision.kind == "reject"
    assert "use omarvis see" in decision.reason


MUTATING_COMMANDS = [
    "hyprctl dispatch workspace 3",
    "hyprctl dispatch focuswindow class:firefox",
    "hyprctl dispatch killactive",
    "hyprctl dispatch exit",
    "herdr agent focus reviewer",
    'herdr agent prompt reviewer "fix it"',
    "herdr agent start reviewer",
    "herdr agent rename reviewer new-name",
    "herdr agent send-keys reviewer esc",
    "herdr pane focus w1:p1",
    "herdr pane split --current --direction right",
    "herdr pane swap w1:p1 w1:p2",
    "herdr pane move w1:p1 --direction right",
    "herdr pane resize w1:p1 --direction right --amount 5",
    "herdr pane zoom w1:p1",
    "herdr pane rename w1:p1 shell",
    "herdr pane input w1:p1 hello",
    "herdr pane send-keys w1:p1 enter",
    'herdr pane run w1:p1 "pytest"',
    "herdr pane send-text w1:p1 hello",
    "herdr pane close w1:p1",
    "herdr tab focus w1:t1",
    "herdr tab create --workspace w1",
    "herdr tab rename w1:t1 new-name",
    "herdr tab close w1:t1",
    "herdr workspace focus w1",
    "herdr workspace create project",
    "herdr workspace rename w1 project",
    "herdr workspace close w1",
    "herdr session stop main",
    "herdr session delete main",
    "herdr server stop",
    "herdr server reload-config",
    "herdr worktree create repo branch",
    "herdr worktree open repo",
    "herdr worktree remove repo branch",
    "herdr notification show Omarvis done",
    "agent-browser open https://example.com",
    "agent-browser back",
    "agent-browser forward",
    "agent-browser reload",
    "agent-browser click @e1",
    "agent-browser dblclick @e1",
    "agent-browser hover @e1",
    "agent-browser focus @e1",
    "agent-browser fill @e1 hello",
    "agent-browser type @e1 hello",
    "agent-browser press Enter",
    "agent-browser select @e1 option",
    "agent-browser check @e1",
    "agent-browser uncheck @e1",
    'agent-browser find text "Go" click',
    "agent-browser wait 100",
    "agent-browser tab new https://example.com",
    "agent-browser tab close",
    "agent-browser tab t2",
    'agent-browser keyboard type "hello"',
    "agent-browser close",
    "agent-browser download @e1 /tmp/file",
    "agent-browser upload @e1 /tmp/file",
    "agent-browser drag @e1 @e2",
    "omarchy theme set tokyo-night",
    "omarchy launch browser",
    "omarchy shell shell setPluginEnabled acme.thing true",
    'omarchy agent prompt "change the system"',
    "omarchy system shutdown",
    "omarchy plugin enable acme.thing",
    "omarchy pkg add example",
]


@pytest.mark.parametrize("command", MUTATING_COMMANDS)
def test_every_mutation_in_the_scope_table_runs_or_asks_for_confirmation(command):
    decision = decide(
        command,
        catalog=omarchy_catalog(),
        dispatchers={"workspace", "focuswindow", "killactive", "exit"},
    )

    assert decision.kind in {"run", "confirm"}


def test_policy_has_exactly_one_scope():
    # Ask mode is gone: the agent call is the only session type, so `decide`
    # must not carry a scope switch that could quietly resurrect a second one.
    source = (Path(__file__).parents[1] / "omarvis" / "policy.py").read_text()

    assert "scope" not in source
    with pytest.raises(TypeError):
        decide("herdr status", catalog=omarchy_catalog(), scope="ask")


def test_omarvis_see_is_the_only_internal_policy_route():
    catalog = omarchy_catalog()

    assert decide("omarvis see", catalog=catalog).kind == "run"
    rejected = decide("omarvis see /tmp/file", catalog=catalog)
    assert rejected.kind == "reject"


def test_herdr_help_and_bare_group_listings_run_immediately():
    # The skill's learn-the-CLI flow: --help and bare group help are
    # read-only and never need confirmation.
    assert decide("herdr --help", catalog=omarchy_catalog()).kind == "run"
    assert decide("herdr pane", catalog=omarchy_catalog()).kind == "run"
    assert decide("herdr agent", catalog=omarchy_catalog()).kind == "run"
    # Unknown groups and deeper flags still reject.
    assert decide("herdr channel", catalog=omarchy_catalog()).kind == "reject"
    assert decide("herdr --skill", catalog=omarchy_catalog()).kind == "reject"
