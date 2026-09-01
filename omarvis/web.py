from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import queue
import secrets
import signal
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from email.utils import formatdate
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .catalog import HYPR_DISPATCHERS, catalog_variables, desktop_state, load_catalog
from .daemon import ExecutionResult, RunToolHandler, load_api_key, load_config
from .screenshot import capture_and_upload_screenshot


DEFAULT_PORT = 4763
SESSION_LIMIT_SECONDS = 300.0
PING_LIMIT_SECONDS = 15.0
LOCAL_END_WAIT_SECONDS = 10.0
BIND_FAILURE_EXIT = 3
MOUNT_PATH = "/omarvis"
DATA_DIR = Path.home() / ".local" / "share" / "omarvis"
SECRET_PATH = DATA_DIR / "web-secret"
THEME_DIR = Path.home() / ".local" / "state" / "omarchy" / "current" / "theme"
WEB_ASSET = Path(__file__).resolve().parent.parent / "assets" / "web" / "index.html"
VENDOR_ASSET = (
    Path(__file__).resolve().parent.parent
    / "assets"
    / "web"
    / "vendor"
    / "elevenlabs-client-1.23.0.iife.js"
)
VENDOR_ROUTE = "/vendor/elevenlabs-client-1.23.0.iife.js"


def emit_json(payload: Mapping[str, Any]) -> None:
    print(json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")), flush=True)


def stable_secret(path: Path = SECRET_PATH) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            path.chmod(0o600)
            return value
    value = secrets.token_urlsafe(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    return value


def qr_matrix(value: str) -> list[list[bool]]:
    import qrcode

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
    qr.add_data(value)
    qr.make(fit=True)
    return [[bool(cell) for cell in row] for row in qr.get_matrix()]


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def run_command(argv: Sequence[str]) -> CommandResult:
    try:
        completed = subprocess.run(
            list(argv), capture_output=True, text=True, check=False, timeout=15
        )
    except FileNotFoundError as error:
        return CommandResult(127, stderr=str(error))
    except subprocess.TimeoutExpired:
        return CommandResult(124, stderr="command timed out")
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _message(result: CommandResult) -> str:
    return (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")[:500]


def _no_such_mount(result: CommandResult) -> bool:
    message = _message(result).lower()
    return any(
        marker in message
        for marker in (
            "no such mount",
            "handler does not exist",
            "not currently serving",
            "not configured",
            "no serve config",
        )
    )


def _operator_required(result: CommandResult) -> bool:
    message = _message(result).lower()
    return "operator" in message or "permission denied" in message or "access denied" in message


def remove_mount(runner: Callable[[Sequence[str]], CommandResult] = run_command) -> CommandResult:
    result = runner(("tailscale", "serve", "--set-path", MOUNT_PATH, "off"))
    if result.returncode == 0 or _no_such_mount(result):
        return CommandResult(0, result.stdout, result.stderr)
    return result


@dataclass(frozen=True)
class TailnetIdentity:
    backend_state: str
    dns_name: str
    login_name: str
    tagged: bool
    user_id: str


def parse_tailscale_status(raw: str) -> TailnetIdentity:
    payload = json.loads(raw)
    self_node = payload.get("Self") or {}
    user_id = str(self_node.get("UserID") or "")
    users = payload.get("User") or {}
    user = users.get(user_id) or users.get(int(user_id), {}) if user_id else {}
    tags = self_node.get("Tags") or []
    return TailnetIdentity(
        backend_state=str(payload.get("BackendState") or ""),
        dns_name=str(self_node.get("DNSName") or "").rstrip("."),
        login_name=str(user.get("LoginName") or ""),
        tagged=bool(tags),
        user_id=user_id,
    )


class TailnetController:
    def __init__(
        self,
        port: int,
        *,
        runner: Callable[[Sequence[str]], CommandResult] = run_command,
        simulate: bool = False,
    ) -> None:
        self.port = port
        self.runner = runner
        self.simulate = simulate
        self.identity = TailnetIdentity("Running", "omarvis.test.ts.net", "", True, "simulate")

    def status(self) -> TailnetIdentity:
        if self.simulate:
            return self.identity
        result = self.runner(("tailscale", "status", "--json"))
        if result.returncode != 0:
            return TailnetIdentity("Stopped", "", "", False, "")
        try:
            self.identity = parse_tailscale_status(result.stdout)
        except (json.JSONDecodeError, TypeError, ValueError):
            return TailnetIdentity("Stopped", "", "", False, "")
        return self.identity

    def mount(self) -> tuple[str, str]:
        identity = self.status()
        if identity.backend_state != "Running" or not identity.dns_name:
            return "needs-tailscale", "Tailscale is not connected"
        if self.simulate:
            return "serving", ""
        result = self.runner(
            (
                "tailscale",
                "serve",
                "--bg",
                "--set-path",
                MOUNT_PATH,
                f"http://127.0.0.1:{self.port}",
            )
        )
        if result.returncode == 0:
            return "serving", ""
        if _operator_required(result):
            return "needs-operator", _message(result)
        return "serve-failed", _message(result)

    def unmount(self) -> CommandResult:
        if self.simulate:
            return CommandResult(0)
        return remove_mount(self.runner)


class EventBroker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        stream: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._lock:
            self._subscribers.add(stream)
        return stream

    def unsubscribe(self, stream: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(stream)

    def publish(self, payload: Mapping[str, Any]) -> None:
        event = dict(payload)
        with self._lock:
            subscribers = tuple(self._subscribers)
        for stream in subscribers:
            stream.put(event)


@dataclass
class RemoteSession:
    conversation_id: str
    handler: RunToolHandler
    client: Any
    created_at: float
    last_ping: float
    stream_attached: bool = False
    tool_lock: threading.Lock = field(default_factory=threading.Lock)


class RemoteApplication:
    def __init__(
        self,
        config: Mapping[str, Any],
        api_key: str,
        *,
        secret: str,
        controller: TailnetController,
        simulate: bool = False,
        clock: Callable[[], float] = time.monotonic,
        command_sink: Callable[[Mapping[str, Any]], None] = emit_json,
        token_minter: Callable[[], tuple[str, str, Any]] | None = None,
    ) -> None:
        self.config = dict(config)
        self.api_key = api_key
        self.secret = secret
        self.controller = controller
        self.simulate = simulate
        self.clock = clock
        self.command_sink = command_sink
        self.token_minter = token_minter
        self.broker = EventBroker()
        self.local_ended = threading.Event()
        self._lock = threading.RLock()
        self._session: RemoteSession | None = None
        self._stopping = threading.Event()
        self.state = "off"
        self.error = ""
        self.url = ""
        self.matrix: list[list[bool]] = []
        self.expected_login = ""
        self.expected_user_id = ""
        self._client: Any = None
        self._watchdog = threading.Thread(target=self._watch_sessions, daemon=True)
        self._watchdog.start()

    def set_serve_state(self, state: str, error: str = "") -> None:
        identity = self.controller.identity
        self.state = state
        self.error = error
        self.expected_user_id = identity.user_id
        self.expected_login = "" if identity.tagged else identity.login_name
        if state == "serving":
            self.url = f"https://{identity.dns_name}{MOUNT_PATH}/?k={self.secret}"
            self.matrix = qr_matrix(self.url)
        else:
            self.url = ""
            self.matrix = []
        self.command_sink(
            {
                "event": "remote",
                "state": state,
                "error": error,
                "url": self.url,
                "qr_matrix": self.matrix,
                "phone_session": self.session() is not None,
            }
        )

    def refresh_identity(self) -> None:
        previous = self.expected_user_id
        identity = self.controller.status()
        if identity.user_id != previous:
            self.expected_user_id = identity.user_id
            self.expected_login = "" if identity.tagged else identity.login_name

    def authorized_identity(self, login: str) -> bool:
        return not self.expected_login or hmac.compare_digest(login.strip(), self.expected_login)

    def authorized_secret(self, supplied: str) -> bool:
        return bool(supplied) and hmac.compare_digest(supplied, self.secret)

    def ack_local_ended(self) -> None:
        self.local_ended.set()

    def _client_instance(self) -> Any:
        if self._client is None:
            from elevenlabs import ElevenLabs

            self._client = ElevenLabs(api_key=self.api_key)
        return self._client

    def _new_handler(self, conversation_id: str, client: Any) -> RunToolHandler:
        def context_sink(text: str) -> None:
            self.broker.publish({"event": "context", "text": text})

        def event_sink(event: Mapping[str, Any]) -> None:
            kind = str(event.get("event") or "")
            if kind == "running":
                self.command_sink({"event": "running", "command": str(event.get("command") or "")})
            elif kind == "ran":
                self.command_sink(
                    {
                        "event": "ran",
                        "command": str(event.get("command") or ""),
                        "exit": event.get("exit"),
                    }
                )

        def screenshot_sender() -> str:
            return capture_and_upload_screenshot(client, conversation_id, self.config)

        executor = None
        if self.simulate:
            executor = lambda _argv, **_kwargs: ExecutionResult(0, stdout="simulated")
        return RunToolHandler(
            catalog=load_catalog(),
            dispatchers=HYPR_DISPATCHERS,
            config=self.config,
            executor=executor,
            context_sink=context_sink,
            event_sink=event_sink,
            state_provider=desktop_state,
            scope="agent",
            screenshot_sender=screenshot_sender,
        )

    def _replace_session(self, conversation_id: str, client: Any) -> RemoteSession:
        self.end_session("takeover")
        now = self.clock()
        session = RemoteSession(
            conversation_id=conversation_id,
            handler=self._new_handler(conversation_id, client),
            client=client,
            created_at=now,
            last_ping=now,
        )
        with self._lock:
            self._session = session
        self.command_sink({"event": "phone", "active": True})
        if self.simulate:
            self.broker.publish({"event": "context", "text": "Simulated remote session ready"})
        return session

    def mint_token(self) -> dict[str, Any]:
        self.end_session("takeover")
        self.local_ended.clear()
        self.command_sink({"event": "action", "action": "end-local"})
        local_ended = self.local_ended.wait(LOCAL_END_WAIT_SECONDS)
        if not local_ended:
            return {"token": "", "dynamic_variables": {}, "local_ended": False}
        variables = catalog_variables(config=self.config)
        if self.token_minter is not None:
            token, conversation_id, client = self.token_minter()
        elif self.simulate:
            raise RuntimeError("--simulate does not mint ElevenLabs conversation tokens")
        else:
            if not self.api_key:
                raise RuntimeError("ElevenLabs API key is not configured")
            client = self._client_instance()
            response = client.conversational_ai.conversations.get_webrtc_token(
                agent_id=str(self.config.get("agent_id") or "")
            )
            token = str(getattr(response, "token", "") or "")
            conversation_id = str(getattr(response, "conversation_id", "") or "")
            if not token or not conversation_id:
                raise RuntimeError("ElevenLabs token mint returned no token or conversation ID")
        self._replace_session(conversation_id, client)
        return {
            "token": token,
            "conversation_id": conversation_id,
            "dynamic_variables": variables,
            "local_ended": True,
        }

    def session(self) -> RemoteSession | None:
        with self._lock:
            session = self._session
        if session is None:
            return None
        now = self.clock()
        if now - session.created_at > SESSION_LIMIT_SECONDS or now - session.last_ping > PING_LIMIT_SECONDS:
            self.end_session("expired")
            return None
        return session

    def ping(self) -> bool:
        session = self.session()
        if session is None:
            return False
        session.last_ping = self.clock()
        return True

    def note_transcript(self, text: str) -> bool:
        session = self.session()
        if session is None:
            return False
        session.handler.note_user_transcript(text)
        return True

    def run_tool(self, parameters: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
        session = self.session()
        if session is None:
            raise LookupError("no live remote session")
        safe_parameters = dict(parameters)
        safe_parameters["tool_call_id"] = "remote"
        with session.tool_lock:
            response = session.handler.handle(safe_parameters)
            file_id = session.handler.take_pending_screenshot("remote")
            if file_id:
                self.broker.publish({"event": "see-ready", "file_id": file_id})
            return response, file_id

    def attach_stream(self) -> RemoteSession | None:
        session = self.session()
        if session is None or session.stream_attached:
            return None
        session.stream_attached = True
        return session

    def stream_closed(self, session: RemoteSession) -> None:
        with self._lock:
            current = self._session
        if current is session:
            self.end_session("event-stream-closed")

    def end_session(self, reason: str = "ended") -> bool:
        with self._lock:
            session = self._session
            self._session = None
        if session is None:
            return False
        session.handler.clear_session_approvals()
        self.broker.publish({"event": "ended", "reason": reason})
        self.command_sink({"event": "phone", "active": False, "reason": reason})
        return True

    def stop(self) -> None:
        self._stopping.set()
        self.end_session("server-stopped")

    def start_simulation(self) -> None:
        if not self.simulate:
            raise RuntimeError("simulation is not enabled")
        self._replace_session("simulate-conversation", object())

    def _watch_sessions(self) -> None:
        while not self._stopping.wait(1.0):
            self.session()


class ThemeAssets:
    def __init__(
        self,
        theme_dir: Path = THEME_DIR,
        index_path: Path = WEB_ASSET,
        vendor_path: Path = VENDOR_ASSET,
    ) -> None:
        self.theme_dir = theme_dir
        self.index_path = index_path
        self.vendor_path = vendor_path
        self._signature: tuple[int, ...] | None = None
        self._html = b""

    @staticmethod
    def _load_toml(path: Path) -> dict[str, Any]:
        try:
            with path.open("rb") as handle:
                return tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            return {}

    @staticmethod
    def _color(shell: Mapping[str, Any], colors: Mapping[str, Any], value: Any, fallback: str) -> str:
        text = str(value or "")
        if text.startswith("#"):
            return text
        if "." in text:
            section, key = text.split(".", 1)
            candidate = shell.get(section, {})
            if isinstance(candidate, Mapping) and candidate.get(key):
                return str(candidate[key])
        return str(colors.get(text) or fallback)

    def html(self) -> bytes:
        shell_path = self.theme_dir / "shell.toml"
        colors_path = self.theme_dir / "colors.toml"
        paths = (self.index_path, shell_path, colors_path)
        signature = tuple(path.stat().st_mtime_ns if path.exists() else 0 for path in paths)
        if signature == self._signature:
            return self._html
        shell = self._load_toml(shell_path)
        colors = self._load_toml(colors_path)
        popups = shell.get("popups", {}) if isinstance(shell.get("popups"), Mapping) else {}
        bar = shell.get("bar", {}) if isinstance(shell.get("bar"), Mapping) else {}
        values = {
            "background": self._color(shell, colors, popups.get("background"), "#111827"),
            "foreground": self._color(shell, colors, popups.get("text"), "#f8fafc"),
            "accent": self._color(shell, colors, bar.get("active"), "#7dd3fc"),
            "urgent": self._color(shell, colors, popups.get("critical"), "#fb7185"),
            "border": self._color(shell, colors, popups.get("border"), "#334155"),
        }
        template = self.index_path.read_text(encoding="utf-8")
        css = ":root{" + "".join(f"--omarvis-{key}:{value};" for key, value in values.items()) + "}"
        self._html = template.replace("/* OMARVIS_THEME */", css).encode("utf-8")
        self._signature = signature
        return self._html

    def background(self) -> tuple[Path, str, str, str] | None:
        path = self.theme_dir.parent / "background"
        if not path.exists():
            return None
        resolved = path.resolve()
        stat = resolved.stat()
        etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
        modified = formatdate(stat.st_mtime, usegmt=True)
        content_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        return resolved, etag, modified, content_type

    def vendor(self) -> tuple[Path, str, str, str] | None:
        if not self.vendor_path.exists():
            return None
        stat = self.vendor_path.stat()
        etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
        modified = formatdate(stat.st_mtime, usegmt=True)
        return self.vendor_path, etag, modified, "text/javascript; charset=utf-8"


def _route_path(raw_path: str) -> tuple[str, dict[str, list[str]]]:
    parsed = urlsplit(raw_path)
    path = parsed.path
    if path == MOUNT_PATH:
        path = "/"
    elif path.startswith(MOUNT_PATH + "/"):
        path = path[len(MOUNT_PATH) :]
    return path or "/", parse_qs(parsed.query, keep_blank_values=True)


def make_handler(app: RemoteApplication, assets: ThemeAssets) -> type[BaseHTTPRequestHandler]:
    class RemoteHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "Omarvis"

        def log_message(self, format: str, *args: Any) -> None:
            print(f"omarvis web: {format % args}", file=sys.stderr, flush=True)

        def _json(self, status: int, payload: Mapping[str, Any]) -> None:
            body = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            self.close_connection = True

        def _host_ok(self) -> bool:
            host = self.headers.get("Host", "")
            try:
                hostname = urlsplit("//" + host).hostname or ""
            except ValueError:
                return False
            allowed = {"127.0.0.1"}
            if app.controller.identity.dns_name:
                allowed.add(app.controller.identity.dns_name)
            return hostname in allowed

        def _secret(self, query: Mapping[str, list[str]]) -> str:
            authorization = self.headers.get("Authorization", "")
            if authorization.startswith("Bearer "):
                return authorization[7:]
            return (query.get("k") or [""])[0]

        def _html_denial(self, message: str) -> None:
            body = (
                "<!doctype html><meta name=viewport content='width=device-width'>"
                "<title>Omarvis Remote</title><p>" + message + "</p>"
            ).encode()
            self.send_response(HTTPStatus.FORBIDDEN)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            self.close_connection = True

        def _file(
            self,
            asset: tuple[Path, str, str, str] | None,
            *,
            cache_control: str,
        ) -> None:
            if asset is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "asset unavailable"})
                return
            file_path, etag, modified, content_type = asset
            if self.headers.get("If-None-Match") == etag:
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("ETag", etag)
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                return
            body = file_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", modified)
            self.send_header("Cache-Control", cache_control)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            self.close_connection = True

        def _authorized(
            self,
            query: Mapping[str, list[str]],
            *,
            post: bool = False,
            top_level: bool = False,
        ) -> bool:
            if not self._host_ok():
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid host"})
                return False
            if not app.authorized_identity(self.headers.get("Tailscale-User-Login", "")):
                if top_level:
                    self._html_denial(
                        "No matching Tailscale identity. Sign this phone in to the same tailnet account."
                    )
                else:
                    self._json(HTTPStatus.FORBIDDEN, {"error": "tailnet identity rejected"})
                return False
            supplied = self._secret(query)
            if post and not self.headers.get("Authorization", "").startswith("Bearer "):
                supplied = ""
            if not app.authorized_secret(supplied):
                if top_level:
                    self._html_denial(
                        "Bad or missing pairing key. Re-scan the QR from the Omarvis panel."
                    )
                else:
                    self._json(HTTPStatus.FORBIDDEN, {"error": "authentication required"})
                return False
            return True

        def _post_guard(self, query: Mapping[str, list[str]]) -> dict[str, Any] | None:
            if not self._authorized(query, post=True):
                return None
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "application/json required"})
                return None
            origin = self.headers.get("Origin", "")
            fetch_site = self.headers.get("Sec-Fetch-Site", "")
            expected_origin = f"https://{app.controller.identity.dns_name}"
            if fetch_site != "same-origin" and origin != expected_origin:
                self._json(HTTPStatus.FORBIDDEN, {"error": "same-origin request required"})
                return None
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length < 0 or length > 64_000:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid body length"})
                return None
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
                return None
            if not isinstance(body, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON object required"})
                return None
            return body

        def do_GET(self) -> None:  # noqa: N802
            path, query = _route_path(self.path)
            if not self._authorized(query, top_level=path == "/"):
                return
            parsed = urlsplit(self.path)
            if parsed.path == MOUNT_PATH:
                location = MOUNT_PATH + "/"
                if parsed.query:
                    location += "?" + parsed.query
                self.send_response(HTTPStatus.PERMANENT_REDIRECT)
                self.send_header("Location", location)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                return
            if path == "/":
                body = assets.html().replace(b"OMARVIS_BACKGROUND_KEY", app.secret.encode())
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                self.close_connection = True
                return
            if path == "/background":
                self._file(assets.background(), cache_control="private, max-age=60")
                return
            if path == VENDOR_ROUTE:
                self._file(assets.vendor(), cache_control="private, max-age=31536000, immutable")
                return
            if path == "/api/events":
                if app.simulate and app.session() is None:
                    app.start_simulation()
                self._events()
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def _events(self) -> None:
            session = app.attach_stream()
            if session is None:
                self._json(HTTPStatus.CONFLICT, {"error": "no live remote session"})
                return
            stream = app.broker.subscribe()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                if app.simulate:
                    for event in (
                        {"event": "context", "text": "Simulated context update"},
                        {"event": "ended", "reason": "simulation-complete"},
                    ):
                        data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                        self.wfile.write(f"data: {data}\n\n".encode())
                        self.wfile.flush()
                    app.end_session("simulation-complete")
                    return
                while app.session() is session:
                    try:
                        event = stream.get(timeout=3.0)
                        data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                        self.wfile.write(f"data: {data}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                app.broker.unsubscribe(stream)
                app.stream_closed(session)
                self.close_connection = True

        def do_POST(self) -> None:  # noqa: N802
            path, query = _route_path(self.path)
            body = self._post_guard(query)
            if body is None:
                return
            try:
                if path == "/api/token":
                    self._json(HTTPStatus.OK, app.mint_token())
                    return
                if path == "/api/ping":
                    if not app.ping():
                        raise LookupError
                    self._json(HTTPStatus.OK, {"ok": True})
                    return
                if path == "/api/transcript":
                    text = str(body.get("text") or "").strip()
                    if body.get("final") is not True or not text:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "final transcript required"})
                        return
                    if not app.note_transcript(text):
                        raise LookupError
                    self._json(HTTPStatus.OK, {"ok": True})
                    return
                if path == "/api/run":
                    response, _file_id = app.run_tool(body)
                    self._json(HTTPStatus.OK, response)
                    return
                if path == "/api/session/end":
                    if app.session() is None:
                        raise LookupError
                    app.end_session("phone-ended")
                    self._json(HTTPStatus.OK, {"ok": True})
                    return
            except LookupError:
                self._json(HTTPStatus.CONFLICT, {"error": "no live remote session"})
                return
            except Exception as error:  # noqa: BLE001 - errors stay server-side except bounded text
                app.command_sink({"event": "error", "message": str(error)[:500]})
                self._json(HTTPStatus.BAD_GATEWAY, {"error": str(error)[:300]})
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    return RemoteHandler


def _stdin_loop(app: RemoteApplication, stop: threading.Event) -> None:
    for raw in sys.stdin:
        command = raw.strip()
        if command == "ack-local-ended":
            app.ack_local_ended()
        elif command == "end-session":
            app.end_session("computer-ended")
        elif command == "quit":
            stop.set()
            break


def _serve_loop(app: RemoteApplication, controller: TailnetController, stop: threading.Event) -> None:
    while not stop.is_set():
        state, error = controller.mount()
        app.set_serve_state(state, error)
        if state in {"serving", "needs-operator", "serve-failed"}:
            break
        stop.wait(3.0)
    while not stop.wait(5.0):
        identity = controller.status()
        if identity.backend_state != "Running" or not identity.dns_name:
            if app.state != "needs-tailscale":
                app.set_serve_state("needs-tailscale", "Tailscale is not connected")
            continue
        if app.state == "needs-tailscale":
            state, error = controller.mount()
            app.set_serve_state(state, error)
            continue
        app.refresh_identity()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Omarvis tailnet web service")
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--port", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.cleanup:
        result = remove_mount()
        if result.returncode != 0:
            print(_message(result), file=sys.stderr)
        return result.returncode

    config = load_config()
    port = int(arguments.port or config.get("web_port") or DEFAULT_PORT)
    controller = TailnetController(port, simulate=arguments.simulate)
    cleanup = controller.unmount()
    if cleanup.returncode != 0:
        emit_json({"event": "error", "message": _message(cleanup)})
    secret = "simulate-secret" if arguments.simulate else stable_secret()
    app = RemoteApplication(
        config,
        load_api_key(),
        secret=secret,
        controller=controller,
        simulate=arguments.simulate,
    )
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, ThemeAssets()))
        server.daemon_threads = True
    except OSError as error:
        emit_json({"event": "error", "message": f"Web bind failed: {error}"})
        return BIND_FAILURE_EXIT

    stop = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, request_stop)

    def stop_when_requested() -> None:
        stop.wait()
        server.shutdown()

    threading.Thread(target=_stdin_loop, args=(app, stop), daemon=True).start()
    threading.Thread(target=_serve_loop, args=(app, controller, stop), daemon=True).start()
    threading.Thread(target=stop_when_requested, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        stop.set()
        app.stop()
        server.server_close()
        cleanup = controller.unmount()
        if cleanup.returncode != 0:
            emit_json({"event": "error", "message": _message(cleanup)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
