import json
from pathlib import Path

import pytest

from omarvis.catalog import (
    HYPR_DISPATCHERS,
    CommandOutput,
    browser_catalog,
    catalog_from_data,
    clean_hypr_error,
    compact_herdr_agents,
    compact_hypr_clients,
    current_state,
    desktop_state,
    herdr_catalog_from_help,
    hyprland_prompt,
    load_catalog,
    load_herdr_catalog,
    profile_memory,
    translate_dispatch,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_omarchy_catalog_warns_when_the_prompt_exceeds_the_tripwire(capsys):
    data = {
        "commands": [
            {
                "route": "omarchy example",
                "summary": "x" * 48_100,
                "hidden": False,
                "aliases": [],
            }
        ]
    }

    catalog = catalog_from_data(data)

    assert len(catalog.prompt_text) > 48_000
    assert "exceeds 48000 bytes" in capsys.readouterr().err


def test_omarchy_catalog_excludes_hidden_routes_and_resolves_aliases():
    catalog = catalog_from_data(
        json.loads((FIXTURES / "omarchy-commands.json").read_text())
    )

    assert ("omarchy", "agent", "usage", "claude") not in catalog.routes
    assert "omarchy agent usage claude" not in catalog.prompt_text
    assert catalog.aliases[("omarchy", "screenshot")] == (
        "omarchy",
        "capture",
        "screenshot",
    )


def test_herdr_catalog_uses_live_help_but_only_renders_policy_routes():
    fixture_dir = FIXTURES / "herdr-help"
    help_by_group = {
        path.stem: path.read_text()
        for path in fixture_dir.glob("*.txt")
        if path.name != "verified-flags.txt"
    }

    catalog = herdr_catalog_from_help(help_by_group)

    assert "herdr agent list — List agents" in catalog.prompt_text
    assert "herdr pane split — Split a pane" in catalog.prompt_text
    assert "herdr status — Show local client and server status" in catalog.prompt_text
    assert "herdr agent wait" not in catalog.prompt_text
    assert "herdr pane report-agent" not in catalog.prompt_text
    assert "herdr server update-agent-manifests" not in catalog.prompt_text


def test_browser_catalog_only_advertises_the_static_policy_allowlist():
    catalog = browser_catalog()

    assert "agent-browser snapshot" in catalog.prompt_text
    assert 'find text "<visible label>" click' in catalog.prompt_text
    assert "tab t2" in catalog.prompt_text
    assert "agent-browser eval" not in catalog.prompt_text
    assert "agent-browser connect" not in catalog.prompt_text
    assert "agent-browser get cdp-url" not in catalog.prompt_text


def test_herdr_agent_state_is_compacted_for_spoken_context():
    payload = json.loads((FIXTURES / "herdr-agent-list.json").read_text())

    lines = compact_herdr_agents(payload)

    assert lines == [
        "w58:p5 codex idle name=reviewer cwd=~/dev/gtm-skills",
        "w58:p6 claude blocked cwd=~/dev/omarvis",
    ]


@pytest.mark.parametrize(
    "cwd, shortened",
    [
        ("/Users/another-user/project", "~/project"),
        ("/home/another-user/project", "~/project"),
        ("/opt/shared/project", "/opt/shared/project"),
    ],
)
def test_herdr_agent_paths_are_compacted_across_hosts(cwd, shortened):
    payload = {
        "result": {
            "agents": [
                {"pane_id": "w1:p1", "agent": "codex", "agent_status": "idle", "cwd": cwd}
            ]
        }
    }

    assert compact_herdr_agents(payload) == [
        f"w1:p1 codex idle cwd={shortened}"
    ]


def test_hyprland_prompt_documents_closing_windows():
    prompt = hyprland_prompt()

    assert "closewindow" in HYPR_DISPATCHERS
    assert "hyprctl dispatch closewindow class:<class>" in prompt


def test_hyprland_prompt_advertises_read_only_state_queries():
    prompt = hyprland_prompt()

    assert "hyprctl clients -j" in prompt
    assert "hyprctl activewindow -j" in prompt
    assert "hyprctl activeworkspace -j" in prompt


def test_profile_memory_uses_configured_path_and_caps_content(tmp_path):
    path = tmp_path / "profile.md"
    path.write_text("profile:" + "x" * 3000)

    profile = profile_memory({"profile_path": str(path)})

    assert profile.startswith("profile:")
    assert len(profile) == 2000


def test_missing_profile_memory_is_an_empty_dynamic_variable(tmp_path):
    assert profile_memory({"profile_path": str(tmp_path / "missing.md")}) == ""


@pytest.mark.parametrize(
    "legacy, lua",
    [
        ("workspace 2", 'hl.dsp.focus({ workspace = "2" })'),
        ("workspace +1", 'hl.dsp.focus({ workspace = "+1" })'),
        ("movetoworkspace 3", 'hl.dsp.window.move({ workspace = "3" })'),
        (
            "focuswindow class:chromium",
            'hl.dsp.focus({ window = "class:(?i)chromium" })',
        ),
        ("killactive", "hl.dsp.window.close()"),
        (
            "closewindow class:chromium",
            'hl.dsp.window.close({ window = "class:(?i)chromium" })',
        ),
        ("fullscreen", 'hl.dsp.window.fullscreen({ action = "toggle" })'),
        ("fullscreen 0", 'hl.dsp.window.fullscreen({ action = "toggle" })'),
        (
            "fullscreen 1",
            'hl.dsp.window.fullscreen({ mode = "maximized", action = "toggle" })',
        ),
        ("togglefloating", 'hl.dsp.window.float({ action = "toggle" })'),
        ("movefocus l", 'hl.dsp.focus({ direction = "left" })'),
        ("movefocus d", 'hl.dsp.focus({ direction = "down" })'),
        ("swapwindow r", 'hl.dsp.window.swap({ direction = "right" })'),
        ("togglesplit", 'hl.dsp.layout("togglesplit")'),
        ("centerwindow", "hl.dsp.window.center()"),
        ("pin", 'hl.dsp.window.pin({ action = "toggle" })'),
        ("togglegroup", "hl.dsp.group.toggle()"),
        ("changegroupactive f", "hl.dsp.group.next()"),
        ("changegroupactive b", "hl.dsp.group.prev()"),
        ("cyclenext", "hl.dsp.window.cycle_next()"),
        ("focusmonitor +1", 'hl.dsp.focus({ monitor = "+" })'),
        ("exit", "hl.dsp.exit()"),
    ],
)
def test_legacy_dispatchers_translate_to_lua_expressions(legacy, lua):
    argv = ("hyprctl", "dispatch", *legacy.split())

    assert translate_dispatch(argv) == ("hyprctl", "dispatch", lua)


@pytest.mark.parametrize(
    "legacy, lua",
    [
        (
            "focuswindow class:Chromium",
            'hl.dsp.focus({ window = "class:(?i)Chromium" })',
        ),
        (
            "closewindow class:Chromium",
            'hl.dsp.window.close({ window = "class:(?i)Chromium" })',
        ),
        (
            "closewindow initialclass:Chromium",
            'hl.dsp.window.close({ window = "initialclass:(?i)Chromium" })',
        ),
        (
            "closewindow title:GitHub",
            'hl.dsp.window.close({ window = "title:(?i)GitHub" })',
        ),
    ],
)
def test_window_selectors_match_case_insensitively(legacy, lua):
    argv = ("hyprctl", "dispatch", *legacy.split())

    assert translate_dispatch(argv) == ("hyprctl", "dispatch", lua)


def test_bare_window_arguments_become_class_selectors():
    argv = ("hyprctl", "dispatch", "focuswindow", "chromium")

    assert translate_dispatch(argv) == (
        "hyprctl",
        "dispatch",
        'hl.dsp.focus({ window = "class:(?i)chromium" })',
    )


def test_window_selectors_do_not_double_case_flags():
    argv = ("hyprctl", "dispatch", "focuswindow", "class:(?i)chromium")

    assert translate_dispatch(argv) == (
        "hyprctl",
        "dispatch",
        'hl.dsp.focus({ window = "class:(?i)chromium" })',
    )


def test_address_selectors_pass_through_unmodified():
    argv = ("hyprctl", "dispatch", "focuswindow", "address:0x55d2a7b0")

    assert translate_dispatch(argv) == (
        "hyprctl",
        "dispatch",
        'hl.dsp.focus({ window = "address:0x55d2a7b0" })',
    )


def test_clean_hypr_error_strips_lua_chunk_noise():
    raw = (
        'WARN: [string "return hl.dispatch(hl.dsp.focus({ window = "cl..."]:1: '
        "hl.focus: window not found"
    )

    assert clean_hypr_error(raw) == "WARN: hl.focus: window not found"


def test_clean_hypr_error_leaves_plain_text_alone():
    assert clean_hypr_error("ok") == "ok"
    assert clean_hypr_error("Invalid dispatcher") == "Invalid dispatcher"


def test_dispatch_translation_escapes_lua_string_arguments():
    argv = ("hyprctl", "dispatch", "closewindow", 'class:a"b\\c')

    assert translate_dispatch(argv) == (
        "hyprctl",
        "dispatch",
        'hl.dsp.window.close({ window = "class:(?i)a\\"b\\\\c" })',
    )


def test_non_dispatch_commands_pass_through_translation_unchanged():
    argv = ("hyprctl", "clients", "-j")

    assert translate_dispatch(argv) == argv


def test_current_state_advertises_the_spaced_terminal_herdr_route():
    def runner(argv, timeout):
        return CommandOutput(1, "", "unavailable")

    state = current_state(runner=runner)

    assert "omarchy launch terminal herdr" in state
    assert "terminal-herdr" not in state


def test_hypr_clients_are_compacted_with_their_workspace():
    payload = [
        {"class": "firefox", "title": "GitHub", "workspace": {"id": 3}},
        {"class": "foot", "title": "tests", "workspace": {"id": 1}},
        "garbage",
    ]

    assert compact_hypr_clients(payload) == [
        "firefox — GitHub (ws 3)",
        "foot — tests (ws 1)",
    ]


def test_desktop_state_is_one_compact_refresh_line():
    responses = {
        ("hyprctl", "activeworkspace", "-j"): '{"id":3}',
        ("hyprctl", "activewindow", "-j"): '{"class":"firefox","title":"GitHub"}',
        ("hyprctl", "clients", "-j"): (
            '[{"class":"firefox","title":"GitHub","workspace":{"id":3}},'
            '{"class":"foot","title":"tests","workspace":{"id":1}}]'
        ),
    }

    def runner(argv, timeout):
        return CommandOutput(0, responses[tuple(argv)], "")

    assert desktop_state(runner=runner) == (
        "Desktop: workspace 3; focused firefox — GitHub; "
        "windows: firefox — GitHub (ws 3), foot — tests (ws 1)"
    )


def test_current_state_collects_desktop_and_herdr_context_without_browser_probe():
    agents = (FIXTURES / "herdr-agent-list.json").read_text()
    responses = {
        ("hyprctl", "activeworkspace", "-j"): '{"id":3}',
        ("hyprctl", "activewindow", "-j"): '{"class":"firefox","title":"GitHub"}',
        (
            "hyprctl",
            "clients",
            "-j",
        ): '[{"class":"firefox","title":"GitHub"},{"class":"foot","title":"tests"}]',
        ("omarchy", "theme", "current"): "Tokyo Night\n",
        (
            "herdr",
            "workspace",
            "list",
        ): '{"result":{"workspaces":[{"workspace_id":"w58","label":"gtm-skills","tab_count":4,"pane_count":6,"agent_status":"idle"}]}}',
        ("herdr", "agent", "list"): agents,
    }
    calls = []

    def runner(argv, timeout):
        calls.append(tuple(argv))
        return CommandOutput(0, responses[tuple(argv)], "")

    state = current_state(
        runner=runner,
        config={"browser_mode": "own-browser"},
    )

    assert "Workspace: 3" in state
    assert "Focused: firefox — GitHub" in state
    assert "firefox — GitHub" in state
    assert "Theme: Tokyo Night" in state
    assert "w58 gtm-skills tabs=4 panes=6 status=idle" in state
    assert "w58:p5 codex idle name=reviewer cwd=~/dev/gtm-skills" in state
    assert "Browser: unprobed:own-browser" in state
    assert not any(call[0] == "agent-browser" for call in calls)


def test_live_omarchy_catalog_is_cached_by_version(tmp_path):
    fixture = (FIXTURES / "omarchy-commands.json").read_text()
    calls = []

    def runner(argv, timeout):
        calls.append(tuple(argv))
        if tuple(argv) == ("omarchy", "version"):
            return CommandOutput(0, "Omarchy 3.2.1\n", "")
        if tuple(argv) == ("omarchy", "commands", "--json"):
            return CommandOutput(0, fixture, "")
        raise AssertionError(argv)

    first = load_catalog(runner=runner, cache_dir=tmp_path)
    second = load_catalog(
        runner=lambda argv, timeout: (_ for _ in ()).throw(
            AssertionError("cache miss")
        ),
        cache_dir=tmp_path,
        version="Omarchy 3.2.1",
    )

    assert first.prompt_text == second.prompt_text
    assert calls == [
        ("omarchy", "version"),
        ("omarchy", "commands", "--json"),
    ]


def test_herdr_catalog_is_empty_when_the_server_is_not_running(tmp_path):
    def runner(argv, timeout):
        assert tuple(argv) == ("herdr", "status")
        return CommandOutput(1, "", "server unavailable")

    catalog = load_herdr_catalog(runner=runner, cache_dir=tmp_path)

    assert catalog.routes == frozenset()
    assert catalog.prompt_text == ""
