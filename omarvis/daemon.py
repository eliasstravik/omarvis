from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import shlex
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .catalog import (
    Catalog,
    clean_hypr_error,
    compact_herdr_agents,
    compact_herdr_workspaces,
    compact_hypr_clients,
    hyprland_speaks_lua,
    translate_dispatch,
)
from .policy import (
    PendingConfirmation,
    always_requires_confirmation,
    confirmation_category,
    decide,
)
from .levels import LevelThrottle, rms_level
from .privatefiles import (
    MAX_CONFIG_BYTES,
    MAX_SECRET_BYTES,
    PrivateFileError,
    read_private_path,
)
from .process import ExecutionResult, ProcessSupervisor, execute_process



@dataclass(frozen=True)
class CategoryApprovalOffer:
    argv: tuple[str, ...]
    category: str
    ts: float
    user_turns_since: int = 0


def compact_browser_tabs(raw_payload: str, *, limit: int = 15) -> str:
    """Turn agent-browser's tab JSON into bounded conversational context."""
    try:
        payload = json.loads(raw_payload)
    except (json.JSONDecodeError, TypeError):
        return "Browser tabs: unavailable"
    data = payload.get("data", payload) if isinstance(payload, Mapping) else payload
    tabs = data.get("tabs", []) if isinstance(data, Mapping) else data
    if not isinstance(tabs, list):
        return "Browser tabs: unavailable"
    lines: list[str] = []
    for tab in tabs[:limit]:
        if not isinstance(tab, Mapping):
            continue
        tab_id = str(tab.get("id") or tab.get("tabId") or "tab")
        title = " ".join(str(tab.get("title") or "untitled").split())[:100]
        host = urlsplit(str(tab.get("url") or "")).hostname or "unknown-host"
        lines.append(f"{tab_id} {title} {host}")
    return "Browser tabs: " + ("; ".join(lines) if lines else "none")


class RunToolHandler:
    def __init__(
        self,
        *,
        catalog: Catalog,
        dispatchers: set[str] | frozenset[str],
        config: Mapping[str, Any],
        executor: Callable[..., ExecutionResult] | None = None,
        clock: Callable[[], float] = time.monotonic,
        confirmation_wait: float = 2.0,
        context_sink: Callable[[str], None] | None = None,
        event_sink: Callable[[Mapping[str, Any]], None] | None = None,
        state_provider: Callable[[], str] | None = None,
        state_refresh_delay: float = 2.0,
        screenshot_sender: Callable[[], str] | None = None,
    ) -> None:
        self.catalog = catalog
        self.dispatchers = frozenset(dispatchers)
        self.config = dict(config)
        self.executor = executor or self._execute_default
        self.clock = clock
        self.confirmation_wait = confirmation_wait
        self.context_sink = context_sink
        self.event_sink = event_sink
        self.state_provider = state_provider
        self.state_refresh_delay = state_refresh_delay
        self.screenshot_sender = screenshot_sender
        self._condition = threading.Condition()
        self._pending: PendingConfirmation | None = None
        self._category_offer: CategoryApprovalOffer | None = None
        self._approved_categories: set[str] = set()
        self._hypr_lua: bool | None = None
        self._browser_mode: str | None = None
        self._browser_tab_owned = False
        self._refresh_thread: threading.Thread | None = None
        self._pending_screenshots: dict[str, str] = {}
        self.supervisor = ProcessSupervisor()

    def terminate_processes(self) -> int:
        """Kill every process group this handler still has running."""
        return self.supervisor.terminate_all()

    def _execute_default(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        kill_on_timeout: bool,
        stdout_limit: int,
    ) -> ExecutionResult:
        # Helpers get the fixed per-program environment allowlist from
        # execute_process; the only value Omarvis adds is the browser binary
        # agent-browser should drive.
        extra_env: dict[str, str] = {}
        browser_path = str(self.config.get("agent_browser_path") or "agent-browser")
        if (
            argv
            and argv[0] == browser_path
            and self.config.get("browser_executable_path")
        ):
            extra_env["AGENT_BROWSER_EXECUTABLE_PATH"] = str(
                self.config["browser_executable_path"]
            )
        return execute_process(
            argv,
            timeout=timeout,
            kill_on_timeout=kill_on_timeout,
            stdout_limit=stdout_limit,
            extra_env=extra_env,
            supervisor=self.supervisor,
        )

    def note_user_transcript(self, _text: str) -> None:
        with self._condition:
            if self._pending is not None:
                self._pending = PendingConfirmation(
                    self._pending.argv,
                    self._pending.ts,
                    self._pending.user_turns_since + 1,
                )
            if self._category_offer is not None:
                self._category_offer = CategoryApprovalOffer(
                    self._category_offer.argv,
                    self._category_offer.category,
                    self._category_offer.ts,
                    self._category_offer.user_turns_since + 1,
                )
            self._condition.notify_all()

    def clear_session_approvals(self) -> None:
        with self._condition:
            self._pending = None
            self._category_offer = None
            self._approved_categories.clear()
            self._pending_screenshots.clear()

    def _approve_category(
        self, command: str, *, confirmed: bool
    ) -> dict[str, Any]:
        try:
            argv = tuple(shlex.split(command))
        except ValueError:
            argv = ()
        with self._condition:
            offer = self._category_offer
            if (
                confirmed
                and offer is not None
                and offer.argv == argv
                and self.clock() - offer.ts <= 30
                and offer.user_turns_since > 0
            ):
                self._approved_categories.add(offer.category)
                self._category_offer = None
                return {
                    "status": "category_approved",
                    "category": offer.category,
                }
        return {
            "status": "rejected",
            "reason": "A fresh user confirmation is required to approve this category.",
        }

    def _set_category_offer(
        self, argv: tuple[str, ...], category: str | None
    ) -> dict[str, Any]:
        if category is None or always_requires_confirmation(argv):
            return {}
        with self._condition:
            self._category_offer = CategoryApprovalOffer(
                argv=argv,
                category=category,
                ts=self.clock(),
            )
        return {
            "confirmation_category": category,
            "can_approve_category": True,
        }

    def _pending_for_decision(self, confirmed: bool) -> PendingConfirmation | None:
        with self._condition:
            if (
                confirmed
                and self._pending is not None
                and self._pending.user_turns_since == 0
            ):
                self._condition.wait_for(
                    lambda: self._pending is None or self._pending.user_turns_since > 0,
                    timeout=self.confirmation_wait,
                )
            return self._pending

    def _browser_prefix(self) -> tuple[str, ...]:
        path = str(self.config.get("agent_browser_path") or "agent-browser")
        # Keep the named daemon alive so Chrome sees one debugging session
        # instead of requiring approval again after agent-browser's idle timeout.
        common = (
            path,
            "--session",
            "omarvis",
            "--pin-tab",
            "--idle-timeout",
            "0",
        )
        if self._browser_mode == "own-browser":
            return common + ("--auto-connect",)
        if self._browser_mode == "real-profile":
            # A Chrome profile NAME makes agent-browser launch a separate
            # window from a snapshot copy of that profile: logins carry over,
            # nothing is written back, and no debugging consent prompt appears.
            profile_name = str(self.config.get("browser_profile") or "Default")
            return common + (
                "--profile",
                profile_name,
                "--args",
                "--no-startup-window",
                "--headed",
            )
        profile = os.path.expanduser(
            str(
                self.config.get("browser_profile")
                or "~/.local/share/omarvis/browser-profile"
            )
        )
        return common + (
            "--profile",
            profile,
            "--args",
            "--no-startup-window",
            "--headed",
        )

    def _probe_browser(self) -> dict[str, Any] | None:
        if self._browser_mode is not None:
            return None
        configured = str(self.config.get("browser_mode", "unavailable"))
        if configured == "unavailable":
            return {"status": "failed", "reason": "browser-unavailable"}
        if configured in {"omarvis-browser", "real-profile"}:
            self._browser_mode = configured
            return None
        path = str(self.config.get("agent_browser_path") or "agent-browser")
        probe_argv = (
            path,
            "--session",
            "omarvis",
            "--pin-tab",
            "--idle-timeout",
            "0",
            "--auto-connect",
            "tab",
            "list",
        )
        probe = self.executor(
            probe_argv,
            timeout=15.0,
            kill_on_timeout=True,
            stdout_limit=3000,
        )
        if probe.timed_out:
            return {"status": "failed", "reason": "browser-pending-approval"}
        if probe.exit_code != 0:
            return {
                "status": "failed",
                "reason": probe.stderr[:200] or "browser-auto-connect-unavailable",
            }
        self._browser_mode = "own-browser"
        tab_list = self.executor(
            self._browser_prefix() + ("tab", "list", "--json"),
            timeout=15.0,
            kill_on_timeout=True,
            stdout_limit=3000,
        )
        if tab_list.exit_code == 0 and self.context_sink is not None:
            self.context_sink(compact_browser_tabs(tab_list.stdout))
        return None

    def _prepare_browser(
        self, argv: tuple[str, ...]
    ) -> tuple[tuple[str, ...] | None, dict[str, Any] | None]:
        error = self._probe_browser()
        if error is not None:
            return None, error
        command = argv[1:]
        if (
            command[:1] == ("open",)
            and not self._browser_tab_owned
            and self._browser_mode == "own-browser"
        ):
            command = ("tab", "new", *command[1:])
        if command[:1] == ("screenshot",):
            cache_dir = os.path.expanduser(
                str(self.config.get("cache_dir") or "~/.cache/omarvis")
            )
            os.makedirs(cache_dir, exist_ok=True)
            command = (
                *command,
                os.path.join(cache_dir, f"screenshot-{int(time.time() * 1000)}.png"),
            )
        return self._browser_prefix() + command, None

    def _hyprland_speaks_lua(self) -> bool:
        if self._hypr_lua is None:
            probe = self.executor(
                ("hyprctl", "version"),
                timeout=2.0,
                kill_on_timeout=True,
                stdout_limit=1500,
            )
            self._hypr_lua = probe.exit_code == 0 and hyprland_speaks_lua(probe.stdout)
        return self._hypr_lua

    @staticmethod
    def _mutates_desktop(argv: tuple[str, ...]) -> bool:
        if argv[:2] == ("hyprctl", "dispatch"):
            return True
        return argv[:1] == ("omarchy",) and argv[1:2] in {("launch",), ("hyprland",)}

    def _schedule_state_refresh(self) -> None:
        if self.state_provider is None or self.context_sink is None:
            return
        if self._refresh_thread is not None and self._refresh_thread.is_alive():
            return

        def push() -> None:
            time.sleep(self.state_refresh_delay)
            self.context_sink(self.state_provider())

        self._refresh_thread = threading.Thread(target=push, daemon=True)
        self._refresh_thread.start()

    def _handle_screenshot(self, tool_call_id: str) -> dict[str, Any]:
        if self.screenshot_sender is None:
            return {
                "status": "unavailable",
                "reason": "Screenshot delivery requires an active ElevenLabs conversation.",
            }
        try:
            file_id = self.screenshot_sender().strip()
            if not file_id:
                raise RuntimeError("ElevenLabs screenshot upload returned no file ID")
            with self._condition:
                self._pending_screenshots[tool_call_id] = file_id
            return {
                "status": "screenshot_uploaded",
                "message": "The screenshot will arrive as the next ElevenLabs user turn.",
            }
        except Exception as error:  # noqa: BLE001 - tool errors become tool text
            return {"status": "failed", "reason": str(error)}

    def take_pending_screenshot(self, tool_call_id: str) -> str | None:
        with self._condition:
            return self._pending_screenshots.pop(tool_call_id, None)

    def handle(self, parameters: Mapping[str, Any]) -> dict[str, Any]:
        try:
            command = str(parameters.get("command", ""))
            confirmed = parameters.get("confirmed") is True
            if parameters.get("approve_category") is True:
                return self._approve_category(command, confirmed=confirmed)
            pending = self._pending_for_decision(confirmed)
            with self._condition:
                approved_categories = frozenset(self._approved_categories)
            decision = decide(
                command,
                catalog=self.catalog,
                dispatchers=self.dispatchers,
                confirmed=confirmed,
                pending=pending,
                now=self.clock(),
                approved_categories=approved_categories,
            )
            if decision.kind == "reject":
                return {"status": "rejected", "reason": decision.reason}
            if decision.kind == "confirm":
                with self._condition:
                    self._pending = PendingConfirmation(decision.argv, self.clock(), 0)
                return {"status": "needs_confirmation", "command": command}
            with self._condition:
                self._pending = None
            confirmed_category = None
            if (
                confirmed
                and pending is not None
                and pending.argv == decision.argv
                and pending.user_turns_since > 0
                and self.clock() - pending.ts <= 30
            ):
                confirmed_category = confirmation_category(decision.argv)
            if decision.argv == ("omarvis", "see"):
                return self._handle_screenshot(
                    str(parameters.get("tool_call_id") or "")
                )
            effective_argv = decision.argv
            if decision.argv == ("omarchy", "launch", "browser") and str(
                self.config.get("browser_mode", "unavailable")
            ) != "unavailable":
                effective_argv = (
                    ("agent-browser", "tab", "new")
                    if self.config.get("browser_mode") == "own-browser"
                    else ("agent-browser", "open", "about:blank")
                )
            execution_argv = effective_argv
            timeout = 3.0
            kill_on_timeout = False
            stdout_limit = 400
            execution_stdout_limit = stdout_limit
            if decision.argv[:2] == ("hyprctl", "clients"):
                stdout_limit = 1500
                execution_stdout_limit = 64_000
            elif decision.argv[:1] == ("herdr",):
                stdout_limit = 600
                execution_stdout_limit = 64_000
            if decision.argv[:2] == ("hyprctl", "dispatch") and (
                self._hyprland_speaks_lua()
            ):
                execution_argv = translate_dispatch(decision.argv)
            if effective_argv[:1] == ("agent-browser",):
                prepared, error = self._prepare_browser(effective_argv)
                if error is not None:
                    return error
                assert prepared is not None
                execution_argv = prepared
                timeout = 30.0
                kill_on_timeout = True
                stdout_limit = 6000 if effective_argv[1:2] == ("snapshot",) else 3000
                execution_stdout_limit = stdout_limit
            if self.event_sink is not None:
                self.event_sink({"event": "running", "command": command})
            result = self.executor(
                execution_argv,
                timeout=timeout,
                kill_on_timeout=kill_on_timeout,
                stdout_limit=execution_stdout_limit,
            )
            if result.timed_out:
                return {"status": "failed", "reason": "timeout"}
            dispatch_rejected = (
                decision.argv[:2] == ("hyprctl", "dispatch")
                and result.exit_code == 0
                and result.stdout.strip() not in ("", "ok")
            )
            succeeded = (
                result.started or result.exit_code == 0
            ) and not dispatch_rejected
            if succeeded and self._mutates_desktop(decision.argv):
                self._schedule_state_refresh()
            if result.started:
                if self.event_sink is not None:
                    self.event_sink({"event": "ran", "command": command, "exit": None})
                response = {"status": "started", "command": command}
                response.update(
                    self._set_category_offer(decision.argv, confirmed_category)
                )
                return response
            if result.exit_code == 0 and effective_argv[:1] == ("agent-browser",):
                if (
                    effective_argv[1:2] == ("open",) and not self._browser_tab_owned
                ) or effective_argv[1:3] == ("tab", "new"):
                    self._browser_tab_owned = True
                elif effective_argv[1:3] == ("tab", "list") and self._browser_mode in {
                    "real-profile",
                    "omarvis-browser",
                }:
                    self._browser_tab_owned = True
                elif effective_argv[1:2] == ("tab",) and effective_argv[2:3] not in {
                    ("list",),
                    ("new",),
                }:
                    self._browser_tab_owned = False
            stdout = result.stdout
            # A truncated producer response is never parsed as a whole; it
            # falls through to the plain bounded-text path instead.
            parseable = result.exit_code == 0 and not result.truncated
            if parseable and decision.argv[:1] == ("herdr",):
                try:
                    payload = json.loads(stdout)
                    if decision.argv[1:3] == ("agent", "list"):
                        stdout = "\n".join(compact_herdr_agents(payload))
                    elif decision.argv[1:3] == ("workspace", "list"):
                        stdout = "\n".join(compact_herdr_workspaces(payload))
                    else:
                        value = (
                            payload.get("result", payload)
                            if isinstance(payload, Mapping)
                            else payload
                        )
                        stdout = json.dumps(
                            value, ensure_ascii=False, separators=(",", ":")
                        )[:600]
                except (json.JSONDecodeError, TypeError):
                    stdout = stdout[:600]
            if parseable and decision.argv[:2] == ("hyprctl", "clients"):
                try:
                    stdout = "\n".join(compact_hypr_clients(json.loads(stdout)))
                except (json.JSONDecodeError, TypeError):
                    stdout = stdout[:600]
            if self.event_sink is not None:
                self.event_sink(
                    {"event": "ran", "command": command, "exit": result.exit_code}
                )
            response = {
                "status": "ok" if succeeded else "failed",
                "exit_code": result.exit_code,
                "stdout": stdout[:stdout_limit],
                "stderr": result.stderr[:200],
            }
            if succeeded:
                response.update(
                    self._set_category_offer(decision.argv, confirmed_category)
                )
            if not succeeded and decision.argv[:2] == ("hyprctl", "dispatch"):
                response["stdout"] = clean_hypr_error(stdout)[:stdout_limit]
                if self.state_provider is not None:
                    response["desktop"] = self.state_provider()
            return response
        except Exception as error:  # noqa: BLE001 - tool failures must become tool responses
            return {"status": "error", "reason": str(error)}

    def handle_client_tool(self, parameters: Mapping[str, Any]) -> str:
        """Serialize a tool result for ElevenLabs' string-only event field."""
        return json.dumps(
            self.handle(parameters), ensure_ascii=False, separators=(",", ":")
        )


DEFAULT_CONFIG: dict[str, Any] = {
    "agent_id": "",
    "llm": "gpt-5.6-sol",
    "voice_id": "JSWO6cw2AyFE324d5kEr",
    "input_device_index": None,
    "herdr_announcements": True,
    "web_port": 4763,
    "browser_mode": "unavailable",
    "agent_browser_path": "agent-browser",
    "screenshot_cache_max_age_seconds": 86_400,
    "profile_path": "~/.config/omarchy/omarvis/profile.md",
    "ui": {
        "hud_position": "top-center",
    },
    "dictation": {
        "language": "",
        "cleanup": True,
        "model_id": "scribe_v2",
    },
}
CONFIG_DIR = Path.home() / ".config" / "omarchy" / "omarvis"
CONFIG_PATH = CONFIG_DIR / "config.json"
API_KEY_PATH = CONFIG_DIR / "api_key"


def emit_event(event: Mapping[str, Any]) -> None:
    print(json.dumps(dict(event), ensure_ascii=False), flush=True)


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    try:
        raw = read_private_path(path, limit=MAX_CONFIG_BYTES, private=False)
    except PrivateFileError as error:
        raise ValueError(f"refusing to load {path}: {error}") from error
    if raw is not None:
        loaded = json.loads(raw.decode("utf-8"))
        if not isinstance(loaded, Mapping):
            raise ValueError(f"{path} must contain a JSON object")
        loaded = dict(loaded)
        loaded.pop("vision", None)
        config.update(loaded)
        configured_dictation = loaded.get("dictation", {})
        if isinstance(configured_dictation, Mapping):
            config["dictation"] = {
                **DEFAULT_CONFIG["dictation"],
                **configured_dictation,
            }
        configured_ui = loaded.get("ui", {})
        if isinstance(configured_ui, Mapping):
            config["ui"] = {
                **DEFAULT_CONFIG["ui"],
                **configured_ui,
            }
    return config


def load_api_key(path: Path = API_KEY_PATH) -> str:
    value = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if value:
        return value
    try:
        raw = read_private_path(path, limit=MAX_SECRET_BYTES)
    except PrivateFileError as error:
        raise ValueError(f"refusing to read {path}: {error}") from error
    return raw.decode("utf-8", "replace").strip() if raw else ""


def sweep_screenshot_cache(
    config: Mapping[str, Any], *, now: float | None = None
) -> int:
    cache_dir = Path(
        os.path.expanduser(str(config.get("cache_dir") or "~/.cache/omarvis"))
    )
    if not cache_dir.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - float(
        config.get("screenshot_cache_max_age_seconds", 86_400)
    )
    removed = 0
    for path in cache_dir.glob("screenshot-*.png"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


HERDR_LIST_STDOUT_LIMIT = 64_000
HERDR_MAX_AGENTS = 100
HERDR_MAX_FIELD_CHARS = 80


def _agent_statuses() -> dict[str, tuple[str, str]]:
    completed = execute_process(
        ["herdr", "agent", "list"],
        timeout=2.0,
        kill_on_timeout=True,
        stdout_limit=HERDR_LIST_STDOUT_LIMIT,
    )
    if completed.timed_out:
        raise RuntimeError("herdr agent list timed out")
    if completed.exit_code != 0:
        raise RuntimeError(completed.stderr.strip() or "herdr agent list failed")
    if completed.truncated:
        raise RuntimeError("herdr agent list exceeded the inventory limit")
    payload = json.loads(completed.stdout)
    result = payload.get("result", payload) if isinstance(payload, Mapping) else None
    if not isinstance(result, Mapping):
        raise ValueError("herdr agent list returned no object")
    agents = result.get("agents", [])
    if not isinstance(agents, list):
        raise ValueError("herdr agent list returned no agent list")
    statuses = {}
    for agent in agents[:HERDR_MAX_AGENTS]:
        if not isinstance(agent, Mapping):
            continue
        pane = str(agent.get("pane_id", ""))[:HERDR_MAX_FIELD_CHARS]
        if pane:
            name = str(agent.get("name") or agent.get("agent") or pane)
            statuses[pane] = (
                " ".join(name.split())[:HERDR_MAX_FIELD_CHARS],
                " ".join(str(agent.get("agent_status", "unknown")).split())[
                    :HERDR_MAX_FIELD_CHARS
                ],
            )
    return statuses


CONTEXT_UPDATE_BACKLOG = 32


def offer_update(updates: queue.Queue[str], text: str) -> None:
    """Queue a contextual update, dropping the oldest when the session lags."""
    while True:
        try:
            updates.put_nowait(text)
            return
        except queue.Full:
            try:
                updates.get_nowait()
            except queue.Empty:
                continue


def watch_herdr(
    stop_requested: threading.Event,
    contextual_updates: queue.Queue[str],
) -> None:
    try:
        previous = _agent_statuses()
    except (
        OSError,
        subprocess.SubprocessError,
        RuntimeError,
        ValueError,
        TypeError,
    ) as error:
        print(
            f"omarvis: Herdr announcements disabled: {error}",
            file=sys.stderr,
            flush=True,
        )
        return
    while not stop_requested.wait(3.0):
        try:
            current = _agent_statuses()
        except (
            OSError,
            subprocess.SubprocessError,
            RuntimeError,
            ValueError,
            TypeError,
        ) as error:
            print(
                f"omarvis: Herdr announcements disabled: {error}",
                file=sys.stderr,
                flush=True,
            )
            return
        changes = []
        for pane, (name, status) in current.items():
            if pane not in previous:
                changes.append(f"{name} appeared {status}")
            elif previous[pane][1] != status:
                changes.append(f"{name} went {status}")
        for pane, (name, _status) in previous.items():
            if pane not in current:
                changes.append(f"{name} vanished")
        if changes:
            offer_update(contextual_updates, "Herdr: " + "; ".join(changes))
        previous = current


def _metered_audio_interface(
    input_device_index: int | None, level_sink: LevelThrottle
) -> Any:
    from elevenlabs.conversational_ai.default_audio_interface import (
        DefaultAudioInterface,
    )

    # Copied from elevenlabs 2.65.0's private DefaultAudioInterface loops.
    # Keep this implementation and bin/omarvis-setup's exact SDK pin in sync.
    class MeteredAudioInterface(DefaultAudioInterface):
        def __init__(self) -> None:
            super().__init__()
            self._stop_once = threading.Lock()
            self._stop_started = False
            self._teardown_thread: threading.Thread | None = None

        def start(self, input_callback: Callable[[bytes], None]) -> None:
            self.input_callback = input_callback
            self.output_queue = queue.Queue()
            self.should_stop = threading.Event()
            self.output_thread = threading.Thread(
                target=self._output_thread, daemon=True
            )
            self.p = self.pyaudio.PyAudio()
            input_options: dict[str, Any] = {
                "format": self.pyaudio.paInt16,
                "channels": 1,
                "rate": 16000,
                "input": True,
                "stream_callback": self._in_callback,
                "frames_per_buffer": self.INPUT_FRAMES_PER_BUFFER,
                "start": True,
            }
            if input_device_index is not None:
                input_options["input_device_index"] = input_device_index
            self.in_stream = self.p.open(
                **input_options,
            )
            self.out_stream = self.p.open(
                format=self.pyaudio.paInt16,
                channels=1,
                rate=16000,
                output=True,
                frames_per_buffer=self.OUTPUT_FRAMES_PER_BUFFER,
                start=True,
            )
            self.output_thread.start()

        def _in_callback(
            self, in_data: bytes, frame_count: int, time_info: Any, status: int
        ) -> tuple[None, int]:
            level_sink.update_in(rms_level(in_data))
            return super()._in_callback(in_data, frame_count, time_info, status)

        # The SDK calls stop() from whichever thread notices the closed
        # websocket first — including the PortAudio input-callback thread
        # (its ws.send raises once the server hangs up after end_call).
        # The stock stop() closes in_stream synchronously, and closing a
        # stream from inside its own callback deadlocks PortAudio, leaving
        # end_session() stuck BEFORE callback_end_session — the session
        # then never ended. Make stop idempotent and hand the blocking
        # teardown to a dedicated thread so end_session() always returns.
        def stop(self) -> None:
            with self._stop_once:
                if self._stop_started:
                    return
                self._stop_started = True
            if not hasattr(self, "should_stop"):
                return
            self.should_stop.set()

            def teardown() -> None:
                try:
                    self.output_thread.join(2.0)
                    self.in_stream.stop_stream()
                    self.in_stream.close()
                    self.out_stream.close()
                finally:
                    self.p.terminate()

            self._teardown_thread = threading.Thread(target=teardown, daemon=True)
            self._teardown_thread.start()

        # Called at session end so PortAudio's streams are actually closed
        # before the process exits; without it the input-callback thread can
        # still be calling into Python while the interpreter finalizes,
        # which segfaults (observed as PyGILState_Ensure crashes in the
        # PyAudio callback thread).
        def join_teardown(self, timeout: float) -> None:
            thread = self._teardown_thread
            if thread is not None:
                thread.join(timeout)

        def _output_thread(self) -> None:
            while not self.should_stop.is_set():
                try:
                    audio = self.output_queue.get(timeout=0.25)
                    level_sink.update_out(rms_level(audio))
                    self.out_stream.write(audio)
                except queue.Empty:
                    level_sink.update_out(0.0)

    return MeteredAudioInterface()


def list_devices() -> int:
    try:
        import pyaudio
    except ImportError:
        print("PyAudio is not installed. Run bin/omarvis-setup.", file=sys.stderr)
        return 2
    audio = pyaudio.PyAudio()
    try:
        for index in range(audio.get_device_count()):
            device = audio.get_device_info_by_index(index)
            if int(device.get("maxInputChannels", 0)) > 0:
                print(
                    f"{index}: {device.get('name', 'unknown')} ({device.get('maxInputChannels')} inputs)"
                )
    finally:
        audio.terminate()
    return 0


def _end_conversation(conversation: Any) -> None:
    try:
        conversation.end_session()
    finally:
        conversation.wait_for_session_end()


def wait_for_conversation_connection(
    conversation: Any,
    stop_requested: threading.Event,
    *,
    timeout: float = 15.0,
) -> None:
    """Wait until the SDK's background thread has opened its WebSocket."""
    deadline = time.monotonic() + timeout
    while not stop_requested.is_set() and time.monotonic() < deadline:
        if getattr(conversation, "_ws", None) is not None:
            return
        thread = getattr(conversation, "_thread", None)
        if thread is not None and not thread.is_alive():
            raise RuntimeError("ElevenLabs connection ended before it became ready")
        stop_requested.wait(0.05)
    if stop_requested.is_set():
        raise RuntimeError("Omarvis stopped before ElevenLabs connected")
    raise RuntimeError("Timed out waiting for ElevenLabs to connect")


SCREENSHOT_FOLLOWUP = (
    "This current desktop screenshot was requested by the preceding user question. "
    "Inspect the attached image and answer that question directly."
)


def send_tool_result_then_screenshot(
    response: Mapping[str, Any],
    parameters: Mapping[str, Any],
    handler: RunToolHandler,
    *,
    send_response: Callable[[Mapping[str, Any]], None],
    send_multimodal: Callable[..., None],
    error_sink: Callable[[str], None] | None = None,
) -> None:
    """Preserve protocol ordering: tool result first, screenshot user turn second."""
    send_response(response)
    tool_call_id = str(parameters.get("tool_call_id") or "")
    file_id = handler.take_pending_screenshot(tool_call_id)
    if file_id is None:
        return
    try:
        send_multimodal(text=SCREENSHOT_FOLLOWUP, file_id=file_id)
    except Exception as error:  # noqa: BLE001 - keep the tool callback thread alive
        if error_sink is not None:
            error_sink(str(error))


def run_session(config: Mapping[str, Any], api_key: str) -> int:
    from elevenlabs import ElevenLabs
    from elevenlabs.conversational_ai.conversation import (
        ClientTools,
        Conversation,
        ConversationInitiationData,
    )

    from .catalog import (
        HYPR_DISPATCHERS,
        catalog_variables,
        desktop_state,
        load_catalog,
    )
    from .screenshot import capture_and_upload_screenshot

    stop_requested = threading.Event()
    session_ended = threading.Event()
    contextual_updates: queue.Queue[str] = queue.Queue(maxsize=CONTEXT_UPDATE_BACKLOG)
    levels = LevelThrottle(
        lambda in_level, out_level: emit_event(
            {"event": "level", "in": in_level, "out": out_level}
        )
    )

    def emit_state(state: str) -> None:
        emit_event({"event": "state", "state": state})

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_requested.set()

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, request_stop)

    variables = catalog_variables(config=config)
    catalog = load_catalog()

    def on_tool_event(event: Mapping[str, Any]) -> None:
        emit_event(event)

    client = ElevenLabs(api_key=api_key)
    conversation_holder: dict[str, Any] = {}

    def upload_screenshot() -> str:
        conversation = conversation_holder.get("conversation")
        conversation_id = str(
            getattr(conversation, "_conversation_id", "") or ""
        )
        return capture_and_upload_screenshot(
            client,
            conversation_id,
            config,
        )

    handler = RunToolHandler(
        catalog=catalog,
        dispatchers=HYPR_DISPATCHERS,
        config=config,
        context_sink=lambda text: offer_update(contextual_updates, text),
        event_sink=on_tool_event,
        state_provider=desktop_state,
        screenshot_sender=upload_screenshot,
    )

    class ScreenshotClientTools(ClientTools):
        def execute_tool(
            self,
            tool_name: str,
            parameters: dict[str, Any],
            callback: Callable[[dict[str, Any]], None],
        ) -> None:
            def ordered_callback(response: dict[str, Any]) -> None:
                conversation = conversation_holder.get("conversation")
                send_tool_result_then_screenshot(
                    response,
                    parameters,
                    handler,
                    send_response=callback,
                    send_multimodal=conversation.send_multimodal_message,
                    error_sink=lambda message: emit_event(
                        {
                            "event": "error",
                            "message": f"Screenshot delivery failed: {message}",
                        }
                    ),
                )

            super().execute_tool(tool_name, parameters, ordered_callback)

    client_tools = ScreenshotClientTools()
    client_tools.register("run", handler.handle_client_tool)

    first_agent_delta = True

    def on_user(text: str) -> None:
        nonlocal first_agent_delta
        first_agent_delta = True
        handler.note_user_transcript(text)
        emit_state("thinking")
        emit_event({"event": "user", "text": text})

    def on_agent_part(text: str, part_type: Any) -> None:
        nonlocal first_agent_delta
        if first_agent_delta:
            first_agent_delta = False
            emit_state("speaking")
        emit_event(
            {
                "event": "agent_part",
                "text": text,
                "type": str(getattr(part_type, "value", part_type)),
            }
        )

    def on_agent(text: str) -> None:
        if first_agent_delta:
            on_agent_part(text, "final")
        else:
            emit_state("speaking")
        emit_event({"event": "agent", "text": text})

    def on_end(*_args: Any) -> None:
        session_ended.set()

    audio_interface = _metered_audio_interface(config.get("input_device_index"), levels)
    conversation = Conversation(
        client=client,
        agent_id=str(config["agent_id"]),
        requires_auth=True,
        audio_interface=audio_interface,
        config=ConversationInitiationData(dynamic_variables=variables),
        client_tools=client_tools,
        callback_user_transcript=on_user,
        callback_agent_response=on_agent,
        callback_agent_chat_response_part=on_agent_part,
        callback_end_session=on_end,
    )
    conversation_holder["conversation"] = conversation
    emit_state("starting")
    if stop_requested.is_set():
        return 0
    conversation.start_session()
    watcher = None
    try:
        wait_for_conversation_connection(conversation, stop_requested)
        if config.get("herdr_announcements", True) and not stop_requested.is_set():
            watcher = threading.Thread(
                target=watch_herdr,
                args=(stop_requested, contextual_updates),
                daemon=True,
            )
            watcher.start()
        emit_state("listening")
        while not stop_requested.is_set() and not session_ended.is_set():
            # Watchdog: don't depend solely on callback_end_session — if the
            # SDK's session thread died or it decided to stop without the
            # callback reaching us, treat the session as over.
            sdk_thread = getattr(conversation, "_thread", None)
            sdk_should_stop = getattr(conversation, "_should_stop", None)
            if (sdk_thread is not None and not sdk_thread.is_alive()) or (
                sdk_should_stop is not None and sdk_should_stop.is_set()
            ):
                session_ended.set()
                break
            try:
                update = contextual_updates.get(timeout=0.2)
            except queue.Empty:
                continue
            conversation.send_contextual_update(update)
    finally:
        stop_requested.set()
        teardown = threading.Thread(
            target=_end_conversation, args=(conversation,), daemon=True
        )
        teardown.start()
        teardown.join(5.0)
        if watcher is not None:
            watcher.join(5.0)
        join_audio = getattr(audio_interface, "join_teardown", None)
        if join_audio is not None:
            join_audio(3.0)
        levels.force_zero()
        emit_state("idle")
        handler.clear_session_approvals()
        handler.terminate_processes()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one Omarvis voice session")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--simulate", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.list_devices:
        return list_devices()
    if arguments.simulate or os.environ.get("OMARVIS_SIMULATE") == "1":
        from .simulate import run_simulation

        run_simulation(
            emit_event,
            delay_scale=float(os.environ.get("OMARVIS_SIMULATE_DELAY_SCALE", "1")),
            include_error=os.environ.get("OMARVIS_SIMULATE_ERROR") == "1",
        )
        return 0
    config: Mapping[str, Any] = DEFAULT_CONFIG
    try:
        config = load_config()
        sweep_screenshot_cache(config)
        if not config.get("agent_id"):
            emit_event(
                {
                    "event": "error",
                    "message": "Omarvis is not provisioned. Run bin/omarvis-setup.",
                }
            )
            return 2
        api_key = load_api_key()
        if not api_key:
            emit_event(
                {
                    "event": "error",
                    "message": "ELEVENLABS_API_KEY is missing. Run bin/omarvis-setup.",
                }
            )
            return 2
        return run_session(config, api_key)
    except Exception as error:  # noqa: BLE001 - keep the CLI failure machine-readable
        emit_event({"event": "error", "message": str(error)})
        return 1


def _exit_now(code: int) -> None:
    """Exit without interpreter finalization.

    PortAudio's input-callback thread and the SDK's websocket thread are
    daemon threads that may still be executing Python when main() returns.
    CPython finalization frees the interpreter state underneath them and the
    next callback segfaults (SIGSEGV in PyGILState_Ensure on the PyAudio
    callback thread — the recurring "Process crashed: python 3.14"). The
    joined teardown above closes the streams in the healthy case; skipping
    finalization makes even the unhealthy case a clean exit.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    _exit_now(main())
