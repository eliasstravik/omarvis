from __future__ import annotations

import argparse
import json
import os
import queue
import signal
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
    compact_herdr_agents,
    compact_herdr_workspaces,
    compact_hypr_clients,
    hyprland_speaks_lua,
    translate_dispatch,
)
from .policy import PendingConfirmation, decide


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    started: bool = False


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


def _reap_process(process: subprocess.Popen[str]) -> None:
    try:
        process.communicate()
    except OSError:
        pass


def execute_process(
    argv: Sequence[str],
    *,
    timeout: float,
    kill_on_timeout: bool,
    stdout_limit: int,
    env: Mapping[str, str] | None = None,
) -> ExecutionResult:
    process = subprocess.Popen(
        list(argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=dict(env) if env is not None else None,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return ExecutionResult(process.returncode, stdout[:stdout_limit], stderr[:200])
    except subprocess.TimeoutExpired:
        if kill_on_timeout:
            process.kill()
            stdout, stderr = process.communicate()
            return ExecutionResult(
                process.returncode,
                stdout[:stdout_limit],
                stderr[:200],
                timed_out=True,
            )
        threading.Thread(target=_reap_process, args=(process,), daemon=True).start()
        return ExecutionResult(None, started=True)


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
        self._condition = threading.Condition()
        self._pending: PendingConfirmation | None = None
        self._hypr_lua: bool | None = None
        self._browser_mode: str | None = None
        self._browser_tab_owned = False
        self._refresh_thread: threading.Thread | None = None

    def _execute_default(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        kill_on_timeout: bool,
        stdout_limit: int,
    ) -> ExecutionResult:
        env = None
        browser_path = str(self.config.get("agent_browser_path") or "agent-browser")
        if (
            argv
            and argv[0] == browser_path
            and self.config.get("browser_executable_path")
        ):
            env = os.environ.copy()
            env["AGENT_BROWSER_EXECUTABLE_PATH"] = str(
                self.config["browser_executable_path"]
            )
        return execute_process(
            argv,
            timeout=timeout,
            kill_on_timeout=kill_on_timeout,
            stdout_limit=stdout_limit,
            env=env,
        )

    def note_user_transcript(self, _text: str) -> None:
        with self._condition:
            if self._pending is not None:
                self._pending = PendingConfirmation(
                    self._pending.argv,
                    self._pending.ts,
                    self._pending.user_turns_since + 1,
                )
            self._condition.notify_all()

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
        common = (path, "--session", "omarvis", "--pin-tab")
        if self._browser_mode == "own-browser":
            return common + ("--auto-connect",)
        profile = os.path.expanduser(
            str(
                self.config.get("browser_profile")
                or "~/.local/share/omarvis/browser-profile"
            )
        )
        return common + ("--profile", profile, "--headed")

    def _probe_browser(self) -> dict[str, Any] | None:
        if self._browser_mode is not None:
            return None
        configured = str(self.config.get("browser_mode", "unavailable"))
        if configured == "unavailable":
            return {"status": "failed", "reason": "browser-unavailable"}
        if configured == "omarvis-browser":
            self._browser_mode = configured
            return None
        path = str(self.config.get("agent_browser_path") or "agent-browser")
        probe_argv = (path, "--session", "omarvis", "--auto-connect", "tab", "list")
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
        if command[:1] == ("open",) and not self._browser_tab_owned:
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

    def handle(self, parameters: Mapping[str, Any]) -> dict[str, Any]:
        try:
            command = str(parameters.get("command", ""))
            confirmed = parameters.get("confirmed") is True
            decision = decide(
                command,
                catalog=self.catalog,
                dispatchers=self.dispatchers,
                confirmed=confirmed,
                pending=self._pending_for_decision(confirmed),
                now=self.clock(),
            )
            if decision.kind == "reject":
                return {"status": "rejected", "reason": decision.reason}
            if decision.kind == "confirm":
                with self._condition:
                    self._pending = PendingConfirmation(decision.argv, self.clock(), 0)
                return {"status": "needs_confirmation", "command": command}
            with self._condition:
                self._pending = None
            execution_argv = decision.argv
            timeout = 3.0
            kill_on_timeout = False
            stdout_limit = 400
            if decision.argv[:2] == ("hyprctl", "clients"):
                stdout_limit = 1500
            if decision.argv[:2] == ("hyprctl", "dispatch") and (
                self._hyprland_speaks_lua()
            ):
                execution_argv = translate_dispatch(decision.argv)
            if decision.argv[:1] == ("agent-browser",):
                prepared, error = self._prepare_browser(decision.argv)
                if error is not None:
                    return error
                assert prepared is not None
                execution_argv = prepared
                timeout = 30.0
                kill_on_timeout = True
                stdout_limit = 6000 if decision.argv[1:2] == ("snapshot",) else 3000
            result = self.executor(
                execution_argv,
                timeout=timeout,
                kill_on_timeout=kill_on_timeout,
                stdout_limit=stdout_limit,
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
                return {"status": "started", "command": command}
            if result.exit_code == 0 and decision.argv[:1] == ("agent-browser",):
                if (
                    decision.argv[1:2] == ("open",) and not self._browser_tab_owned
                ) or decision.argv[1:3] == ("tab", "new"):
                    self._browser_tab_owned = True
                elif decision.argv[1:2] == ("tab",) and decision.argv[2:3] not in {
                    ("list",),
                    ("new",),
                }:
                    self._browser_tab_owned = False
            stdout = result.stdout
            if result.exit_code == 0 and decision.argv[:1] == ("herdr",):
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
            if result.exit_code == 0 and decision.argv[:2] == ("hyprctl", "clients"):
                try:
                    stdout = "\n".join(compact_hypr_clients(json.loads(stdout)))
                except (json.JSONDecodeError, TypeError):
                    stdout = stdout[:600]
            if self.event_sink is not None:
                self.event_sink(
                    {"event": "ran", "command": command, "exit": result.exit_code}
                )
            return {
                "status": "ok" if succeeded else "failed",
                "exit_code": result.exit_code,
                "stdout": stdout[:stdout_limit],
                "stderr": result.stderr[:200],
            }
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
    "input_device_index": None,
    "herdr_announcements": True,
    "browser_mode": "unavailable",
    "agent_browser_path": "agent-browser",
}
CONFIG_DIR = Path.home() / ".config" / "omarchy" / "omarvis"
CONFIG_PATH = CONFIG_DIR / "config.json"
API_KEY_PATH = CONFIG_DIR / "api_key"


def emit_event(event: Mapping[str, Any]) -> None:
    print(json.dumps(dict(event), ensure_ascii=False), flush=True)


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if path.exists():
        loaded = json.loads(path.read_text())
        if not isinstance(loaded, Mapping):
            raise ValueError(f"{path} must contain a JSON object")
        config.update(loaded)
    return config


def load_api_key(path: Path = API_KEY_PATH) -> str:
    value = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if value:
        return value
    return path.read_text().strip() if path.exists() else ""


class Notifier:
    def __init__(self) -> None:
        self.notification_id = ""

    def start(self) -> None:
        try:
            completed = subprocess.run(
                [
                    "omarchy-notification-send",
                    "-p",
                    "-g",
                    chr(0xF130),
                    "Omarvis",
                    "Listening",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if completed.returncode == 0:
                self.notification_id = completed.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass

    def update(self, headline: str, description: str) -> None:
        command = ["omarchy-notification-send"]
        if self.notification_id:
            command.extend(("-r", self.notification_id))
        command.extend((headline, description[:240]))
        try:
            subprocess.run(
                command,
                timeout=2,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            pass


def _agent_statuses() -> dict[str, tuple[str, str]]:
    completed = subprocess.run(
        ["herdr", "agent", "list"],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "herdr agent list failed")
    payload = json.loads(completed.stdout)
    result = payload.get("result", payload)
    statuses = {}
    for agent in result.get("agents", []):
        pane = str(agent.get("pane_id", ""))
        if pane:
            statuses[pane] = (
                str(agent.get("name") or agent.get("agent") or pane),
                str(agent.get("agent_status", "unknown")),
            )
    return statuses


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
            contextual_updates.put("Herdr: " + "; ".join(changes))
        previous = current


def _selected_audio_interface(input_device_index: int | None) -> Any:
    from elevenlabs.conversational_ai.default_audio_interface import (
        DefaultAudioInterface,
    )

    if input_device_index is None:
        return DefaultAudioInterface()

    class SelectedAudioInterface(DefaultAudioInterface):
        def start(self, input_callback: Callable[[bytes], None]) -> None:
            self.input_callback = input_callback
            self.output_queue = queue.Queue()
            self.should_stop = threading.Event()
            self.output_thread = threading.Thread(
                target=self._output_thread, daemon=True
            )
            self.p = self.pyaudio.PyAudio()
            self.in_stream = self.p.open(
                format=self.pyaudio.paInt16,
                channels=1,
                rate=16000,
                input=True,
                input_device_index=input_device_index,
                stream_callback=self._in_callback,
                frames_per_buffer=self.INPUT_FRAMES_PER_BUFFER,
                start=True,
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

    return SelectedAudioInterface()


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


def run_session(
    config: Mapping[str, Any], api_key: str, *, text_only: bool, messages: Sequence[str]
) -> int:
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

    stop_requested = threading.Event()
    session_ended = threading.Event()
    contextual_updates: queue.Queue[str] = queue.Queue()
    notifier = Notifier()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_requested.set()

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, request_stop)

    variables = catalog_variables(config=config)
    catalog = load_catalog()

    def on_tool_event(event: Mapping[str, Any]) -> None:
        emit_event(event)
        if event.get("event") == "ran":
            notifier.update("Omarvis", f"Ran: {event.get('command', 'command')}")

    handler = RunToolHandler(
        catalog=catalog,
        dispatchers=HYPR_DISPATCHERS,
        config=config,
        context_sink=contextual_updates.put,
        event_sink=on_tool_event,
        state_provider=desktop_state,
    )
    client_tools = ClientTools()
    client_tools.register("run", handler.handle_client_tool)

    def on_user(text: str) -> None:
        handler.note_user_transcript(text)
        emit_event({"event": "state", "state": "listening"})
        emit_event({"event": "user", "text": text})
        notifier.update("You", text)

    def on_agent(text: str) -> None:
        emit_event({"event": "state", "state": "speaking"})
        emit_event({"event": "agent", "text": text})
        notifier.update("Omarvis", text)

    def on_end(*_args: Any) -> None:
        session_ended.set()

    audio_interface = (
        None
        if text_only
        else _selected_audio_interface(config.get("input_device_index"))
    )
    conversation = Conversation(
        client=ElevenLabs(api_key=api_key),
        agent_id=str(config["agent_id"]),
        requires_auth=True,
        audio_interface=audio_interface,
        config=ConversationInitiationData(dynamic_variables=variables),
        client_tools=client_tools,
        callback_user_transcript=on_user,
        callback_agent_response=on_agent,
        callback_end_session=on_end,
    )
    notifier.start()
    emit_event({"event": "state", "state": "starting"})
    if stop_requested.is_set():
        return 0
    conversation.start_session()
    if stop_requested.is_set():
        session_ended.set()
    watcher = None
    if config.get("herdr_announcements", True) and not stop_requested.is_set():
        watcher = threading.Thread(
            target=watch_herdr,
            args=(stop_requested, contextual_updates),
            daemon=True,
        )
        watcher.start()
    emit_event({"event": "state", "state": "listening"})
    for message in messages:
        conversation.send_user_message(message)
    while not stop_requested.is_set() and not session_ended.is_set():
        try:
            update = contextual_updates.get(timeout=0.2)
        except queue.Empty:
            continue
        conversation.send_contextual_update(update)
    teardown = threading.Thread(
        target=_end_conversation, args=(conversation,), daemon=True
    )
    teardown.start()
    teardown.join(5.0)
    stop_requested.set()
    if watcher is not None:
        watcher.join(5.0)
    emit_event({"event": "state", "state": "idle"})
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one Omarvis voice session")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--text-only", action="store_true")
    parser.add_argument("--message", action="append", default=[])
    arguments = parser.parse_args(argv)
    if arguments.list_devices:
        return list_devices()
    try:
        config = load_config()
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
        return run_session(
            config, api_key, text_only=arguments.text_only, messages=arguments.message
        )
    except Exception as error:  # noqa: BLE001 - keep the CLI failure machine-readable
        emit_event({"event": "error", "message": str(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
