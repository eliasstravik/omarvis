import json
from pathlib import Path

from omarvis.catalog import (
    CommandOutput,
    browser_catalog,
    catalog_from_data,
    compact_herdr_agents,
    current_state,
    herdr_catalog_from_help,
    load_catalog,
    load_herdr_catalog,
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
