from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass

from .catalog import HYPR_READ_COMMANDS, Catalog

HERDR_IMMEDIATE = frozenset(
    {
        *(
            ("agent", command)
            for command in (
                "list",
                "get",
                "read",
                "focus",
                "explain",
                "prompt",
                "start",
                "rename",
                "send-keys",
            )
        ),
        *(
            ("pane", command)
            for command in (
                "list",
                "current",
                "get",
                "layout",
                "process-info",
                "neighbor",
                "edges",
                "focus",
                "read",
                "split",
                "swap",
                "move",
                "resize",
                "zoom",
                "rename",
                "input",
                "send-keys",
            )
        ),
        *(("tab", command) for command in ("list", "get", "focus", "create", "rename")),
        *(
            ("workspace", command)
            for command in ("list", "get", "focus", "create", "rename")
        ),
        ("session", "list"),
        ("notification", "show"),
        ("api", "snapshot"),
        ("status",),
    }
)
HERDR_CONFIRM = frozenset(
    {
        ("pane", "run"),
        ("pane", "send-text"),
        ("pane", "close"),
        ("tab", "close"),
        ("workspace", "close"),
        ("session", "stop"),
        ("session", "delete"),
        ("server", "stop"),
        ("server", "reload-config"),
        *(("worktree", command) for command in ("create", "open", "remove")),
    }
)
HERDR_IMMEDIATE = HERDR_IMMEDIATE | frozenset({("worktree", "list")})
HERDR_ALLOW = HERDR_IMMEDIATE | HERDR_CONFIRM

BROWSER_SINGLE_ALLOW = frozenset(
    {
        "open",
        "back",
        "forward",
        "reload",
        "scroll",
        "click",
        "dblclick",
        "hover",
        "focus",
        "fill",
        "type",
        "press",
        "select",
        "check",
        "uncheck",
        "find",
        "snapshot",
        "wait",
        "screenshot",
        "scrollintoview",
        "close",
        "download",
        "upload",
        "drag",
    }
)
BROWSER_GET_ALLOW = frozenset(
    {"text", "html", "value", "attr", "title", "url", "count", "box", "styles"}
)
BROWSER_IS_ALLOW = frozenset({"visible", "enabled", "checked"})
BROWSER_CONFIRM = frozenset({("close",), ("download",), ("upload",), ("drag",)})

ASK_HERDR_ROUTES = frozenset(
    route
    for route in HERDR_ALLOW
    if route == ("status",)
    or route == ("api", "snapshot")
    or route[-1]
    in {"list", "get", "read", "explain", "process-info", "neighbor", "edges"}
)
ASK_BROWSER_ROUTES = frozenset(
    {
        ("snapshot",),
        ("tab", "list"),
        ("screenshot",),
        ("scroll",),
        ("scrollintoview",),
        *(("get", command) for command in BROWSER_GET_ALLOW),
        *(("is", command) for command in BROWSER_IS_ALLOW),
    }
)
ASK_OMARCHY_ROUTES = frozenset(
    {
        ("omarchy", "capture", "text"),
        ("omarchy", "capture", "screenshot"),
        ("omarchy", "theme", "current"),
        ("omarchy", "theme", "list"),
        ("omarchy", "system", "stats"),
        ("omarchy", "commands"),
    }
)
ASK_REFUSAL_REASON = (
    "Omarvis is in ask mode and cannot perform that action; "
    "tell the user to press SUPER + CTRL + J for Agent mode."
)


@dataclass(frozen=True)
class Decision:
    kind: str
    argv: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class PendingConfirmation:
    argv: tuple[str, ...]
    ts: float
    user_turns_since: int = 0


def confirmation_category(argv: tuple[str, ...]) -> str | None:
    if not argv:
        return None
    if argv[0] == "omarchy" and len(argv) > 1:
        return f"omarchy:{argv[1]}"
    if argv[0] == "herdr" and len(argv) > 1:
        if argv[1:3] in {("agent", "send-keys"), ("pane", "send-keys")}:
            return "herdr:control-keys"
        return f"herdr:{argv[1]}"
    if argv[0] == "agent-browser" and len(argv) > 1:
        return f"browser:{argv[1]}"
    if argv[:3] == ("hyprctl", "dispatch", "exit"):
        return "system:power"
    return None


def always_requires_confirmation(argv: tuple[str, ...]) -> bool:
    if any(token in {"close", "delete", "remove"} for token in argv[1:]):
        return True
    if argv[:3] in {
        ("herdr", "session", "stop"),
        ("herdr", "session", "delete"),
        ("herdr", "server", "stop"),
    }:
        return True
    if argv[:3] == ("hyprctl", "dispatch", "exit"):
        return True
    return argv[:3] in {
        ("omarchy", "system", "shutdown"),
        ("omarchy", "system", "reboot"),
        ("omarchy", "system", "logout"),
        ("omarchy", "system", "suspend"),
        ("omarchy", "system", "hibernate"),
    }


def _omarchy_requires_confirmation(
    argv: tuple[str, ...], route: tuple[str, ...]
) -> bool:
    if route in {
        ("omarchy", "system", "shutdown"),
        ("omarchy", "system", "reboot"),
        ("omarchy", "system", "logout"),
        ("omarchy", "system", "suspend"),
        ("omarchy", "system", "hibernate"),
        ("omarchy", "hyprland", "window", "close", "all"),
        ("omarchy", "plugin", "add"),
        ("omarchy", "plugin", "clone"),
        ("omarchy", "plugin", "remove"),
        ("omarchy", "plugin", "update"),
        ("omarchy", "plugin", "enable"),
        ("omarchy", "pkg", "add"),
        ("omarchy", "pkg", "aur", "add"),
        ("omarchy", "pkg", "remove"),
        ("omarchy", "theme", "install"),
        ("omarchy", "theme", "remove"),
    }:
        return True
    if len(route) > 1 and route[1] in {
        "install",
        "remove",
        "reinstall",
        "refresh",
        "update",
    }:
        return True
    if route == ("omarchy", "bar") and argv[len(route) : len(route) + 1] in {
        ("set",),
        ("move",),
    }:
        return True
    return route == ("omarchy", "shell") and any(
        token in {"setPluginEnabled", "enablePlugin"} for token in argv[len(route) :]
    )


def _confirmation_decision(
    command: str,
    argv: tuple[str, ...],
    *,
    confirmed: bool,
    pending: PendingConfirmation | None,
    now: float,
    approved_categories: frozenset[str],
) -> Decision:
    category = confirmation_category(argv)
    if (
        category is not None
        and category in approved_categories
        and not always_requires_confirmation(argv)
    ):
        return Decision("run", argv)
    if (
        confirmed
        and pending is not None
        and pending.argv == argv
        and now - pending.ts <= 30
        and pending.user_turns_since > 0
    ):
        return Decision("run", argv)
    return Decision("confirm", argv, command)


def _decide_herdr(
    command: str,
    argv: tuple[str, ...],
    *,
    confirmed: bool,
    pending: PendingConfirmation | None,
    now: float,
    approved_categories: frozenset[str],
) -> Decision:
    if len(argv) < 2:
        return Decision("reject", reason="bare herdr is not allowed")
    route = (argv[1],) if argv[1] == "status" else tuple(argv[1:3])
    if route not in HERDR_ALLOW:
        return Decision("reject", reason="unknown herdr route")
    arguments = argv[1 + len(route) :]
    if any(
        token in {"--wait", "--remote", "--session"} or token.startswith("--until")
        for token in arguments
    ):
        return Decision("reject", reason="blocking or remote herdr flag")
    control_key = route in {("agent", "send-keys"), ("pane", "send-keys")} and any(
        "ctrl+" in token.lower() for token in arguments
    )
    if route in HERDR_CONFIRM or control_key:
        return _confirmation_decision(
            command,
            argv,
            confirmed=confirmed,
            pending=pending,
            now=now,
            approved_categories=approved_categories,
        )
    return Decision("run", argv)


def _browser_route(argv: tuple[str, ...]) -> tuple[tuple[str, ...], int] | None:
    if len(argv) < 2:
        return None
    command = argv[1]
    if command in BROWSER_SINGLE_ALLOW:
        return (command,), 2
    if command == "tab" and len(argv) >= 3:
        operation = argv[2]
        if operation in {"list", "new", "close"}:
            return ("tab", operation), 3
        if re.fullmatch(r"t\d+", operation) or re.fullmatch(
            r"[0-9a-fA-F]{16,64}", operation
        ):
            return ("tab", "switch"), 3
        return None
    if command == "get" and len(argv) >= 3 and argv[2] in BROWSER_GET_ALLOW:
        return ("get", argv[2]), 3
    if command == "is" and len(argv) >= 3 and argv[2] in BROWSER_IS_ALLOW:
        return ("is", argv[2]), 3
    if command == "keyboard" and len(argv) >= 3 and argv[2] == "type":
        return ("keyboard", "type"), 3
    return None


def _browser_flags_allowed(
    route: tuple[str, ...], argv: tuple[str, ...], start: int
) -> bool:
    if route == ("close",) and argv[start:] == ("--all",):
        return True
    if route == ("snapshot",):
        index = start
        while index < len(argv):
            token = argv[index]
            if token in {"-i", "-c"}:
                index += 1
                continue
            if token == "-d" and index + 1 < len(argv) and argv[index + 1].isdigit():
                index += 2
                continue
            return False
        return True
    exempt_indexes: set[int] = set()
    if route in {("fill",), ("type",)} and len(argv) > 3:
        exempt_indexes.add(3)
    elif route == ("keyboard", "type"):
        exempt_indexes.update(range(start, len(argv)))
    elif route == ("find",) and len(argv) > 3:
        exempt_indexes.add(3)
    return not any(
        token.startswith("-") and index not in exempt_indexes
        for index, token in enumerate(argv[start:], start=start)
    )


def _decide_browser(
    command: str,
    argv: tuple[str, ...],
    *,
    confirmed: bool,
    pending: PendingConfirmation | None,
    now: float,
    approved_categories: frozenset[str],
) -> Decision:
    matched = _browser_route(argv)
    if matched is None:
        return Decision("reject", reason="unknown browser route")
    route, argument_start = matched
    if route == ("tab", "switch") and len(argv) != 3:
        return Decision("reject", reason="tab switch takes one reference")
    if route == ("screenshot",) and len(argv) != 2:
        return Decision("reject", reason="screenshot paths are not allowed")
    if (
        route == ("wait",)
        and len(argv) > 2
        and argv[2].isdigit()
        and int(argv[2]) > 20_000
    ):
        return Decision("reject", reason="browser wait exceeds 20 seconds")
    if route == ("close",) and argv[2:] not in {(), ("--all",)}:
        return Decision("reject", reason="unsupported close arguments")
    if not _browser_flags_allowed(route, argv, argument_start):
        return Decision("reject", reason="browser flags are not allowed")
    if route in BROWSER_CONFIRM:
        return _confirmation_decision(
            command,
            argv,
            confirmed=confirmed,
            pending=pending,
            now=now,
            approved_categories=approved_categories,
        )
    return Decision("run", argv)


def _omarchy_route(
    argv: tuple[str, ...], catalog: Catalog
) -> tuple[str, ...] | None:
    for size in range(len(argv), 0, -1):
        candidate = argv[:size]
        if candidate in catalog.routes or candidate in catalog.aliases:
            return catalog.aliases.get(candidate, candidate)
    return None


def _decide_ask(
    command: str,
    argv: tuple[str, ...],
    *,
    catalog: Catalog,
) -> Decision:
    if argv[0] == "hyprctl" and argv in HYPR_READ_COMMANDS:
        return Decision("run", argv)
    if argv[0] == "herdr":
        route = (argv[1],) if len(argv) > 1 and argv[1] == "status" else tuple(argv[1:3])
        if route in ASK_HERDR_ROUTES:
            arguments = argv[1 + len(route) :]
            if not any(
                token in {"--wait", "--remote", "--session"}
                or token.startswith("--until")
                for token in arguments
            ):
                return Decision("run", argv)
    if argv[0] == "agent-browser":
        matched = _browser_route(argv)
        if matched is not None and matched[0] in ASK_BROWSER_ROUTES:
            decision = _decide_browser(
                command,
                argv,
                confirmed=False,
                pending=None,
                now=time.monotonic(),
                approved_categories=frozenset(),
            )
            if decision.kind == "run":
                return decision
    if argv[0] == "omarchy":
        route = _omarchy_route(argv, catalog)
        if route in ASK_OMARCHY_ROUTES or argv[:2] == ("omarchy", "commands"):
            return Decision("run", argv)
    return Decision("reject", reason=ASK_REFUSAL_REASON)


def decide(
    command: str,
    *,
    catalog: Catalog,
    confirmed: bool = False,
    pending: PendingConfirmation | None = None,
    now: float | None = None,
    dispatchers: set[str] | frozenset[str] = frozenset(),
    scope: str = "agent",
    approved_categories: set[str] | frozenset[str] = frozenset(),
) -> Decision:
    approved = frozenset(approved_categories)
    if any(character in command for character in ("\n", "\r", "\0")):
        return Decision("reject", reason="control character")
    if len(command) > 2000:
        return Decision("reject", reason="command too long")
    try:
        argv = tuple(shlex.split(command))
    except ValueError:
        return Decision("reject", reason="invalid quoting")
    if not argv or argv[0] not in {
        "omarchy",
        "hyprctl",
        "herdr",
        "agent-browser",
        "omarvis",
    }:
        return Decision("reject", reason="unsupported command")
    if scope == "ask":
        if argv == ("omarvis", "see"):
            return Decision("run", argv)
        return _decide_ask(command, argv, catalog=catalog)
    if scope != "agent":
        return Decision("reject", reason="unknown policy scope")
    if argv[0] == "omarvis":
        if argv == ("omarvis", "see"):
            return Decision("run", argv)
        return Decision("reject", reason="unknown Omarvis route")
    if argv and argv[0] == "omarchy":
        route = _omarchy_route(argv, catalog)
        if route is not None:
            if _omarchy_requires_confirmation(argv, route):
                return _confirmation_decision(
                    command,
                    argv,
                    confirmed=confirmed,
                    pending=pending,
                    now=time.monotonic() if now is None else now,
                    approved_categories=approved,
                )
            return Decision("run", argv)
    if argv[0] == "hyprctl":
        if argv in HYPR_READ_COMMANDS:
            return Decision("run", argv)
        if len(argv) < 3 or argv[1] != "dispatch" or argv[2] not in dispatchers:
            return Decision("reject", reason="unknown dispatcher")
        if argv[2] == "exit":
            return _confirmation_decision(
                command,
                argv,
                confirmed=confirmed,
                pending=pending,
                now=time.monotonic() if now is None else now,
                approved_categories=approved,
            )
        return Decision("run", argv)
    if argv[0] == "herdr":
        return _decide_herdr(
            command,
            argv,
            confirmed=confirmed,
            pending=pending,
            now=time.monotonic() if now is None else now,
            approved_categories=approved,
        )
    if argv[0] == "agent-browser":
        return _decide_browser(
            command,
            argv,
            confirmed=confirmed,
            pending=pending,
            now=time.monotonic() if now is None else now,
            approved_categories=approved,
        )
    return Decision("reject", reason="unknown route")
