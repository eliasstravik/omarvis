from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any
from .privatefiles import PrivateFileError, read_private_path, write_private_path
from .process import execute_process


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
    if text == home or text.startswith(home + "/"):
        return "~" + text[len(home) :]

    # Herdr state may originate on another host (for example when restoring a
    # workspace created on macOS). Keep the spoken context compact and avoid
    # exposing a username merely because that host uses a different home root.
    parts = Path(text).parts
    if len(parts) >= 3 and parts[:2] in (("/", "Users"), ("/", "home")):
        return "~" + ("/" + "/".join(parts[3:]) if len(parts) > 3 else "")

    return text


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


# `omarchy commands --json` is a few hundred kilobytes; everything else the
# catalog asks for is far smaller.
CATALOG_STDOUT_LIMIT = 2 * 1024 * 1024
CACHE_FILE_LIMIT = 4 * 1024 * 1024


def _read_cache(path: Path) -> str | None:
    """Return a cache file's text, or None when it is absent, odd, or corrupt."""
    try:
        raw = read_private_path(path, limit=CACHE_FILE_LIMIT, private=False)
    except PrivateFileError:
        return None
    if not raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _read_json_cache(path: Path) -> Any:
    text = _read_cache(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _write_cache(path: Path, text: str) -> None:
    """Publish a cache file atomically so a crash never leaves a corrupt one."""
    try:
        write_private_path(path, text.encode("utf-8"), mode=0o644)
    except (OSError, PrivateFileError):
        pass


def _default_runner(argv: Sequence[str], timeout: float) -> CommandOutput:
    result = execute_process(
        list(argv),
        timeout=timeout,
        kill_on_timeout=True,
        stdout_limit=CATALOG_STDOUT_LIMIT,
    )
    if result.timed_out:
        raise subprocess.TimeoutExpired(list(argv), timeout)
    if result.truncated or result.overflowed:
        return CommandOutput(1, "", "output exceeded the catalog limit")
    return CommandOutput(result.exit_code or 0, result.stdout, result.stderr)


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
    lines.extend(f"- {line}" for line in compact_hypr_clients(clients))
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
            "Herdr: not running (say 'open herdr' to run omarchy launch terminal herdr)"
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
    cached = _read_json_cache(cache_path)
    if isinstance(cached, Mapping):
        return catalog_from_data(cached)
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
    _write_cache(cache_path, json.dumps(payload, ensure_ascii=False))
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

HERDR_SKILL_PREFACE = """\
How you use Herdr: you control the focused Herdr session from outside a
pane, through the `run` tool. Ignore the HERDR_ENV check and the rule
against controlling Herdr from outside below — they are for agents embedded
inside panes; every command you issue is validated by Omarvis policy
instead. The flags --wait, --remote, --session and --until* are rejected.
To learn syntax, `herdr --help` and bare group listings like `herdr pane`
are allowed."""


def _adapt_herdr_skill(raw: str) -> str:
    """Turn `herdr --skill` into Omarvis guidance.

    The document ships as a skill for coding agents embedded in Herdr
    panes: YAML frontmatter plus an HERDR_ENV gate. Omarvis legitimately
    drives the focused session from outside, so the frontmatter is dropped
    and a preface overrides the embedded-agent rules.
    """
    text = raw.strip()
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4 :].lstrip("\n")
    return HERDR_SKILL_PREFACE + "\n\n" + text


def load_herdr_skill(
    *,
    runner: Callable[[Sequence[str], float], CommandOutput] = _default_runner,
    cache_dir: Path | None = None,
) -> str:
    """The `herdr --skill` expert guide, adapted for Omarvis and cached by
    herdr version. Empty when herdr is not installed."""
    try:
        version_output = runner(("herdr", "--version"), 2.0)
    except (OSError, subprocess.SubprocessError):
        return ""
    version = (
        version_output.stdout.strip() if version_output.returncode == 0 else "unknown"
    )
    cache_root = cache_dir or Path.home() / ".cache" / "omarvis"
    cache_path = cache_root / f"herdr-skill-{_cache_key(version)}.md"
    cached_skill = _read_cache(cache_path)
    if cached_skill:
        return cached_skill
    try:
        output = runner(("herdr", "--skill"), 3.0)
    except (OSError, subprocess.SubprocessError):
        return ""
    if output.returncode != 0 or not output.stdout.strip():
        return ""
    adapted = _adapt_herdr_skill(output.stdout)
    _write_cache(cache_path, adapted)
    return adapted

HYPR_DISPATCHER_DOCS = {
    "workspace": "workspace <n|+1|-1>",
    "movetoworkspace": "movetoworkspace <n> (moves the focused window; focus the target first)",
    "focuswindow": "focuswindow class:<class>",
    "killactive": "killactive",
    "closewindow": "closewindow class:<class>",
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

_LUA_DIRECTIONS = {"l": "left", "r": "right", "u": "up", "d": "down"}


def _lua_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# Prefixes whose payload Hyprland treats as an RE2 regex (case-sensitive full
# match); Omarvis injects (?i) so a spoken "Chromium" still matches app_id
# "chromium". The literal prefixes compare exact strings and must not change.
_REGEX_SELECTOR_PREFIXES = ("class:", "initialclass:", "title:", "initialtitle:", "tag:")
_LITERAL_SELECTOR_PREFIXES = ("address:", "pid:", "stableid:")
_KEYWORD_SELECTORS = ("active", "floating", "tiled")


def _window_selector(argument: str) -> str:
    if argument.startswith(_LITERAL_SELECTOR_PREFIXES) or argument.startswith(
        _KEYWORD_SELECTORS
    ):
        return argument
    for prefix in _REGEX_SELECTOR_PREFIXES:
        if argument.startswith(prefix):
            rest = argument[len(prefix) :]
            return argument if rest.startswith("(?i)") else f"{prefix}(?i){rest}"
    # A bare argument selects nothing on Hyprland 0.55 (empty class regex).
    return f"class:(?i){argument}"


def clean_hypr_error(text: str) -> str:
    """Strip the Lua eval chunk-source noise from a hyprctl error reply."""
    return re.sub(r'\[string "[^\n]*"\]:\d+:\s*', "", text)


def _lua_dispatch(dispatcher: str, argument: str) -> str | None:
    if dispatcher == "workspace":
        return f"hl.dsp.focus({{ workspace = {_lua_quote(argument)} }})"
    if dispatcher == "movetoworkspace":
        return f"hl.dsp.window.move({{ workspace = {_lua_quote(argument)} }})"
    if dispatcher == "focuswindow":
        return f"hl.dsp.focus({{ window = {_lua_quote(_window_selector(argument))} }})"
    if dispatcher == "killactive":
        return "hl.dsp.window.close()"
    if dispatcher == "closewindow":
        return f"hl.dsp.window.close({{ window = {_lua_quote(_window_selector(argument))} }})"
    if dispatcher == "fullscreen":
        if argument == "1":
            return 'hl.dsp.window.fullscreen({ mode = "maximized", action = "toggle" })'
        return 'hl.dsp.window.fullscreen({ action = "toggle" })'
    if dispatcher == "togglefloating":
        return 'hl.dsp.window.float({ action = "toggle" })'
    if dispatcher == "movefocus":
        direction = _LUA_DIRECTIONS.get(argument, argument)
        return f"hl.dsp.focus({{ direction = {_lua_quote(direction)} }})"
    if dispatcher == "swapwindow":
        direction = _LUA_DIRECTIONS.get(argument, argument)
        return f"hl.dsp.window.swap({{ direction = {_lua_quote(direction)} }})"
    if dispatcher == "togglesplit":
        return 'hl.dsp.layout("togglesplit")'
    if dispatcher == "centerwindow":
        return "hl.dsp.window.center()"
    if dispatcher == "pin":
        return 'hl.dsp.window.pin({ action = "toggle" })'
    if dispatcher == "togglegroup":
        return "hl.dsp.group.toggle()"
    if dispatcher == "changegroupactive":
        return "hl.dsp.group.prev()" if argument == "b" else "hl.dsp.group.next()"
    if dispatcher == "cyclenext":
        return "hl.dsp.window.cycle_next()"
    if dispatcher == "focusmonitor":
        monitor = {"+1": "+", "-1": "-"}.get(argument, argument)
        return f"hl.dsp.focus({{ monitor = {_lua_quote(monitor)} }})"
    if dispatcher == "exit":
        return "hl.dsp.exit()"
    return None


def translate_dispatch(argv: tuple[str, ...]) -> tuple[str, ...]:
    """Rewrite a legacy dispatcher into the Hyprland 0.55+ Lua form."""
    if argv[:2] != ("hyprctl", "dispatch") or len(argv) < 3:
        return argv
    lua = _lua_dispatch(argv[2], argv[3] if len(argv) > 3 else "")
    if lua is None:
        return argv
    return ("hyprctl", "dispatch", lua)


def hyprland_speaks_lua(version_output: str) -> bool:
    match = re.search(r"tag:\s*v?(\d+)\.(\d+)", version_output, re.IGNORECASE)
    if match is None:
        match = re.search(r"\bv?(\d+)\.(\d+)\.\d+", version_output)
    if match is None:
        return False
    return (int(match.group(1)), int(match.group(2))) >= (0, 55)


HYPR_READ_COMMANDS = frozenset(
    {
        ("hyprctl", "clients", "-j"),
        ("hyprctl", "activewindow", "-j"),
        ("hyprctl", "activeworkspace", "-j"),
    }
)


def compact_hypr_clients(payload: Any, *, limit: int = 20) -> list[str]:
    if not isinstance(payload, list):
        return []
    lines: list[str] = []
    for client in payload[:limit]:
        if not isinstance(client, Mapping):
            continue
        workspace = client.get("workspace")
        workspace_id = (
            workspace.get("id", "?") if isinstance(workspace, Mapping) else "?"
        )
        lines.append(
            f"{client.get('class', 'unknown')} — {client.get('title', '')} (ws {workspace_id})"
        )
    return lines


def desktop_state(
    *,
    runner: Callable[[Sequence[str], float], CommandOutput] = _default_runner,
) -> str:
    active_workspace = _run_json(runner, ("hyprctl", "activeworkspace", "-j"))
    active_window = _run_json(runner, ("hyprctl", "activewindow", "-j"))
    clients = _run_json(runner, ("hyprctl", "clients", "-j"))
    workspace_id = (
        active_workspace.get("id", "unknown")
        if isinstance(active_workspace, Mapping)
        else "unknown"
    )
    if isinstance(active_window, Mapping):
        focused = f"{active_window.get('class', 'unknown')} — {active_window.get('title', '')}".rstrip()
    else:
        focused = "unknown"
    windows = ", ".join(compact_hypr_clients(clients)) or "none"
    return f"Desktop: workspace {workspace_id}; focused {focused}; windows: {windows}"


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
    cached_help = _read_json_cache(cache_path)
    if isinstance(cached_help, Mapping):
        return herdr_catalog_from_help(cached_help)
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
    _write_cache(cache_path, json.dumps(help_by_group, ensure_ascii=False))
    return herdr_catalog_from_help(help_by_group)


def hyprland_prompt() -> str:
    lines = [f"hyprctl dispatch {usage}" for usage in HYPR_DISPATCHER_DOCS.values()]
    lines.extend(
        (
            "hyprctl clients -j — List windows with class, title, and workspace",
            "hyprctl activewindow -j — Show the focused window",
            "hyprctl activeworkspace -j — Show the active workspace",
        )
    )
    return "\n".join(lines)


def profile_memory(config: Mapping[str, Any] | None = None, *, limit: int = 2000) -> str:
    path = Path(
        os.path.expanduser(
            str(
                (config or {}).get("profile_path")
                or "~/.config/omarchy/omarvis/profile.md"
            )
        )
    )
    try:
        return path.read_text(errors="replace")[:limit]
    except OSError:
        return ""


def catalog_variables(
    *,
    config: Mapping[str, Any] | None = None,
    runner: Callable[[Sequence[str], float], CommandOutput] = _default_runner,
) -> dict[str, str]:
    return {
        "command_catalog": load_catalog(runner=runner).prompt_text,
        "hyprland_dispatchers": hyprland_prompt(),
        "herdr_catalog": load_herdr_catalog(runner=runner).prompt_text,
        "herdr_skill": load_herdr_skill(runner=runner),
        "browser_catalog": browser_catalog().prompt_text,
        "current_state": current_state(config=config, runner=runner),
        "profile": profile_memory(config),
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
