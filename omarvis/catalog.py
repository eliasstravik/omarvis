from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class Catalog:
    routes: frozenset[tuple[str, ...]]
    aliases: Mapping[tuple[str, ...], tuple[str, ...]]
    prompt_text: str


@dataclass(frozen=True)
class CommandOutput:
    returncode: int
    stdout: str
    stderr: str


def catalog_from_data(data: Mapping[str, Any]) -> Catalog:
    routes: set[tuple[str, ...]] = set()
    aliases: dict[tuple[str, ...], tuple[str, ...]] = {}
    lines: list[str] = []
    for item in data.get("commands", []):
        if item.get("hidden"):
            continue
        route = tuple(str(item["route"]).split())
        routes.add(route)
        lines.append(f"{' '.join(route)} — {item.get('summary', '')}".rstrip())
        for alias in item.get("aliases", []):
            aliases[tuple(str(alias).split())] = route
    prompt_text = "\n".join(lines)
    if len(prompt_text) > 48_000:
        print(
            f"omarvis: command catalog is {len(prompt_text)} bytes and exceeds 48000 bytes",
            file=sys.stderr,
        )
    return Catalog(
        routes=frozenset(routes),
        aliases=MappingProxyType(aliases),
        prompt_text=prompt_text,
    )


def herdr_catalog_from_help(help_by_group: Mapping[str, str]) -> Catalog:
    from .policy import HERDR_ALLOW

    routes: set[tuple[str, ...]] = {("herdr", "status")}
    lines = ["herdr status — Show local client and server status"]
    for group, help_text in sorted(help_by_group.items()):
        for raw_line in help_text.splitlines():
            match = re.match(r"^\s{2}([a-z][a-z0-9-]*)\s{2,}(.+?)\s*$", raw_line)
            if match is None:
                continue
            command, description = match.groups()
            if (group, command) not in HERDR_ALLOW:
                continue
            route = ("herdr", group, command)
            routes.add(route)
            lines.append(f"{' '.join(route)} — {description}")
    lines.extend(
        (
            'Usage: herdr agent prompt <target> "<text>"',
            "Usage: herdr pane split --current --direction right|down --no-focus",
            "Usage: herdr agent|pane send-keys <target> esc|enter|up|down",
        )
    )
    return Catalog(
        routes=frozenset(routes),
        aliases=MappingProxyType({}),
        prompt_text="\n".join(lines),
    )


def browser_catalog() -> Catalog:
    commands = {
        "open <url>": "Navigate in the Omarvis tab",
        "back": "Go back",
        "forward": "Go forward",
        "reload": "Reload the page",
        "scroll <up|down|left|right> [pixels]": "Scroll the page",
        "click <selector|@ref>": "Click an element",
        "dblclick <selector|@ref>": "Double-click an element",
        "hover <selector|@ref>": "Hover an element",
        "focus <selector|@ref>": "Focus an element",
        'fill <selector|@ref> "<text>"': "Clear and fill an input",
        'type <selector|@ref> "<text>"': "Type into an input",
        "press <key>": "Press a key",
        "select <selector|@ref> <value>": "Select an option",
        "check <selector|@ref>": "Check a checkbox",
        "uncheck <selector|@ref>": "Uncheck a checkbox",
        'find text "<visible label>" click': "Find visible text and click it",
        "get <text|html|value|attr|title|url|count|box|styles> [selector]": "Read page information",
        "is <visible|enabled|checked> <selector>": "Check element state",
        "snapshot [-i] [-c] [-d <depth>]": "Read the accessibility tree with element refs",
        "wait <selector|milliseconds>": "Wait up to 20 seconds",
        "tab list": "List tabs",
        "tab new [url]": "Open an Omarvis-owned tab",
        "tab close [ref]": "Close a tab",
        "tab t2": "Switch to a tab by stable reference",
        "screenshot": "Save a screenshot under the Omarvis cache",
        "scrollintoview <selector|@ref>": "Scroll an element into view",
        'keyboard type "<text>"': "Type real keystrokes",
        "close [--all]": "Close the browser attachment and require confirmation",
        "download <selector|@ref> <path>": "Download after confirmation",
        "upload <selector|@ref> <path>": "Upload after confirmation",
        "drag <source> <target>": "Drag after confirmation",
    }
    lines = [
        f"agent-browser {usage} — {description}"
        for usage, description in commands.items()
    ]
    routes = {tuple(("agent-browser " + usage).split()) for usage in commands}
    return Catalog(
        routes=frozenset(routes),
        aliases=MappingProxyType({}),
        prompt_text="\n".join(lines),
    )


def _short_path(value: Any) -> str:
    text = str(value or "unknown")
    home = str(Path.home())
    return (
        "~" + text[len(home) :] if text == home or text.startswith(home + "/") else text
    )


def compact_herdr_agents(payload: Mapping[str, Any], *, limit: int = 30) -> list[str]:
    result = payload.get("result", payload)
    agents = result.get("agents", []) if isinstance(result, Mapping) else []
    lines: list[str] = []
    for agent in agents[:limit]:
        if not isinstance(agent, Mapping):
            continue
        parts = [
            str(agent.get("pane_id", "unknown")),
            str(agent.get("agent", "agent")),
            str(agent.get("agent_status", "unknown")),
        ]
        if agent.get("name"):
            parts.append(f"name={agent['name']}")
        parts.append(f"cwd={_short_path(agent.get('cwd'))}")
        lines.append(" ".join(parts))
    return lines


def _default_runner(argv: Sequence[str], timeout: float) -> CommandOutput:
    completed = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return CommandOutput(completed.returncode, completed.stdout, completed.stderr)


def _run_json(
    runner: Callable[[Sequence[str], float], CommandOutput],
    argv: Sequence[str],
) -> Any:
    try:
        output = runner(argv, 2.0)
        if output.returncode != 0:
            return None
        return json.loads(output.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, TypeError):
        return None


def compact_herdr_workspaces(payload: Any, *, limit: int = 30) -> list[str]:
    if not isinstance(payload, Mapping):
        return []
    result = payload.get("result", payload)
    workspaces = result.get("workspaces", []) if isinstance(result, Mapping) else []
    lines = []
    for workspace in workspaces[:limit]:
        if not isinstance(workspace, Mapping):
            continue
        lines.append(
            " ".join(
                (
                    str(workspace.get("workspace_id", "unknown")),
                    str(workspace.get("label", "workspace")),
                    f"tabs={workspace.get('tab_count', '?')}",
                    f"panes={workspace.get('pane_count', '?')}",
                    f"status={workspace.get('agent_status', 'unknown')}",
                )
            )
        )
    return lines


def current_state(
    *,
    config: Mapping[str, Any] | None = None,
    runner: Callable[[Sequence[str], float], CommandOutput] = _default_runner,
) -> str:
    active_workspace = _run_json(runner, ("hyprctl", "activeworkspace", "-j"))
    active_window = _run_json(runner, ("hyprctl", "activewindow", "-j"))
    clients = _run_json(runner, ("hyprctl", "clients", "-j"))
    try:
        theme_output = runner(("omarchy", "theme", "current"), 2.0)
        theme = (
            theme_output.stdout.strip() if theme_output.returncode == 0 else "unknown"
        )
    except (OSError, subprocess.SubprocessError):
        theme = "unknown"
    workspace_id = (
        active_workspace.get("id", "unknown")
        if isinstance(active_workspace, Mapping)
        else "unknown"
    )
    if isinstance(active_window, Mapping):
        focused = f"{active_window.get('class', 'unknown')} — {active_window.get('title', '')}".rstrip()
    else:
        focused = "unknown"
    lines = [
        f"Workspace: {workspace_id}",
        f"Focused: {focused}",
        f"Theme: {theme}",
        "Windows:",
    ]
    if isinstance(clients, list):
        for client in clients[:20]:
            if isinstance(client, Mapping):
                lines.append(
                    f"- {client.get('class', 'unknown')} — {client.get('title', '')}".rstrip()
                )
    if len(lines) == 4:
        lines.append("- unknown")
    workspace_payload = _run_json(runner, ("herdr", "workspace", "list"))
    agent_payload = _run_json(runner, ("herdr", "agent", "list"))
    workspace_lines = compact_herdr_workspaces(workspace_payload)
    agent_lines = (
        compact_herdr_agents(agent_payload)
        if isinstance(agent_payload, Mapping)
        else []
    )
    if workspace_lines or agent_lines:
        lines.append("Herdr:")
        lines.extend(f"- {line}" for line in workspace_lines + agent_lines)
    else:
        lines.append(
            "Herdr: not running (say 'open herdr' to run omarchy launch terminal-herdr)"
        )
    browser_mode = str((config or {}).get("browser_mode", "unavailable"))
    lines.append(f"Browser: unprobed:{browser_mode}")
    return "\n".join(lines)


def _cache_key(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-") or "unknown"


def load_catalog(
    *,
    runner: Callable[[Sequence[str], float], CommandOutput] = _default_runner,
    cache_dir: Path | None = None,
    version: str | None = None,
) -> Catalog:
    cache_root = cache_dir or Path.home() / ".cache" / "omarvis"
    if version is None:
        try:
            version_output = runner(("omarchy", "version"), 2.0)
            version = (
                version_output.stdout.strip()
                if version_output.returncode == 0
                else "unknown"
            )
        except (OSError, subprocess.SubprocessError):
            version = "fixture"
    cache_path = cache_root / f"catalog-{_cache_key(version)}.json"
    if cache_path.exists():
        return catalog_from_data(json.loads(cache_path.read_text()))
    try:
        output = runner(("omarchy", "commands", "--json"), 5.0)
        if output.returncode != 0:
            raise RuntimeError(output.stderr.strip() or "omarchy commands failed")
        payload = json.loads(output.stdout)
    except (OSError, subprocess.SubprocessError, RuntimeError, json.JSONDecodeError):
        fixture = (
            Path(__file__).parent.parent
            / "tests"
            / "fixtures"
            / "omarchy-commands.json"
        )
        if not fixture.exists():
            raise
        payload = json.loads(fixture.read_text())
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, ensure_ascii=False))
    return catalog_from_data(payload)


HERDR_GROUPS = (
    "agent",
    "pane",
    "tab",
    "workspace",
    "worktree",
    "notification",
    "session",
    "server",
    "api",
)

HYPR_DISPATCHER_DOCS = {
    "workspace": "workspace <n|+1|-1>",
    "movetoworkspace": "movetoworkspace <n>",
    "focuswindow": "focuswindow class:<class>",
    "killactive": "killactive",
    "fullscreen": "fullscreen [0|1]",
    "togglefloating": "togglefloating",
    "movefocus": "movefocus l|r|u|d",
    "swapwindow": "swapwindow l|r|u|d",
    "togglesplit": "togglesplit",
    "centerwindow": "centerwindow",
    "pin": "pin",
    "togglegroup": "togglegroup",
    "changegroupactive": "changegroupactive f|b",
    "cyclenext": "cyclenext",
    "focusmonitor": "focusmonitor +1",
    "exit": "exit (requires confirmation)",
}
HYPR_DISPATCHERS = frozenset(HYPR_DISPATCHER_DOCS)


def _empty_catalog() -> Catalog:
    return Catalog(frozenset(), MappingProxyType({}), "")


def load_herdr_catalog(
    *,
    runner: Callable[[Sequence[str], float], CommandOutput] = _default_runner,
    cache_dir: Path | None = None,
) -> Catalog:
    try:
        status = runner(("herdr", "status"), 2.0)
    except (OSError, subprocess.SubprocessError):
        return _empty_catalog()
    if status.returncode != 0 or "status: running" not in status.stdout.lower():
        return _empty_catalog()
    try:
        version_output = runner(("herdr", "--version"), 2.0)
        version = (
            version_output.stdout.strip()
            if version_output.returncode == 0
            else "unknown"
        )
    except (OSError, subprocess.SubprocessError):
        version = "unknown"
    cache_root = cache_dir or Path.home() / ".cache" / "omarvis"
    cache_path = cache_root / f"herdr-catalog-{_cache_key(version)}.json"
    if cache_path.exists():
        return herdr_catalog_from_help(json.loads(cache_path.read_text()))
    help_by_group: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(HERDR_GROUPS)) as executor:
        futures = {
            executor.submit(runner, ("herdr", group, "--help"), 3.0): group
            for group in HERDR_GROUPS
        }
        for future in as_completed(futures):
            group = futures[future]
            try:
                output = future.result()
            except (OSError, subprocess.SubprocessError):
                continue
            if output.returncode == 0:
                help_by_group[group] = output.stdout
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(help_by_group, ensure_ascii=False))
    return herdr_catalog_from_help(help_by_group)


def hyprland_prompt() -> str:
    return "\n".join(
        f"hyprctl dispatch {usage}" for usage in HYPR_DISPATCHER_DOCS.values()
    )


def catalog_variables(
    *,
    config: Mapping[str, Any] | None = None,
    runner: Callable[[Sequence[str], float], CommandOutput] = _default_runner,
) -> dict[str, str]:
    return {
        "command_catalog": load_catalog(runner=runner).prompt_text,
        "hyprland_dispatchers": hyprland_prompt(),
        "herdr_catalog": load_herdr_catalog(runner=runner).prompt_text,
        "browser_catalog": browser_catalog().prompt_text,
        "current_state": current_state(config=config, runner=runner),
    }


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Build Omarvis prompt catalogs")
    parser.add_argument("--print", action="store_true", dest="print_catalogs")
    arguments = parser.parse_args(argv)
    if not arguments.print_catalogs:
        parser.error("use --print")
    variables = catalog_variables()
    for name, value in variables.items():
        print(f"## {name}\n{value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
