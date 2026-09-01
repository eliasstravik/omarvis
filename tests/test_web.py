from __future__ import annotations

import http.client
import hashlib
import json
import stat
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from omarvis.web import (
    BIND_FAILURE_EXIT,
    CommandResult,
    RemoteApplication,
    TailnetController,
    ThemeAssets,
    _route_path,
    make_handler,
    main,
    parse_tailscale_status,
    remove_mount,
    stable_secret,
)


def test_stable_secret_is_reused_and_private(tmp_path: Path) -> None:
    path = tmp_path / "state" / "web-secret"
    first = stable_secret(path)
    second = stable_secret(path)

    assert first == second
    assert len(first) >= 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("message", ["no such mount", "handler does not exist"])
def test_remove_mount_uses_pinned_command_and_accepts_missing_mount(message) -> None:
    calls = []

    def runner(argv):
        calls.append(tuple(argv))
        return CommandResult(1, stderr=message)

    assert remove_mount(runner).returncode == 0
    assert calls == [("tailscale", "serve", "--set-path", "/omarvis", "off")]


@pytest.mark.parametrize(
    ("serve_result", "expected_state"),
    [
        (CommandResult(1, stderr="permission denied: set an operator"), "needs-operator"),
        (CommandResult(1, stderr="backend rejected the mount"), "serve-failed"),
        (CommandResult(0), "serving"),
    ],
)
def test_tailnet_mount_state_machine_preserves_verbatim_failure(
    serve_result: CommandResult, expected_state: str
) -> None:
    calls = []
    status = json.dumps(
        {
            "BackendState": "Running",
            "Self": {"DNSName": "desk.example.ts.net.", "UserID": 42},
            "User": {"42": {"LoginName": "person@example.com"}},
        }
    )

    def runner(argv):
        calls.append(tuple(argv))
        if tuple(argv) == ("tailscale", "status", "--json"):
            return CommandResult(0, stdout=status)
        return serve_result

    controller = TailnetController(4763, runner=runner)
    state, error = controller.mount()

    assert state == expected_state
    assert error == ("" if serve_result.returncode == 0 else serve_result.stderr)
    assert calls[-1] == (
        "tailscale",
        "serve",
        "--bg",
        "--set-path",
        "/omarvis",
        "http://127.0.0.1:4763",
    )


def test_tailnet_mount_requires_a_running_tailnet() -> None:
    controller = TailnetController(
        4763,
        runner=lambda _argv: CommandResult(0, stdout='{"BackendState":"Stopped"}'),
    )

    assert controller.mount() == ("needs-tailscale", "Tailscale is not connected")


def test_bind_failure_has_a_distinct_process_exit_code(monkeypatch, capsys) -> None:
    unmounts = []

    class Controller:
        def unmount(self):
            unmounts.append(True)
            return CommandResult(0)

    monkeypatch.setattr("omarvis.web.load_config", lambda: {})
    monkeypatch.setattr("omarvis.web.load_api_key", lambda: "")
    monkeypatch.setattr("omarvis.web.stable_secret", lambda: "secret")
    monkeypatch.setattr("omarvis.web.TailnetController", lambda *_args, **_kwargs: Controller())
    monkeypatch.setattr("omarvis.web.RemoteApplication", lambda *_args, **_kwargs: object())

    def fail_bind(*_args, **_kwargs):
        raise OSError("address already in use")

    monkeypatch.setattr("omarvis.web.ThreadingHTTPServer", fail_bind)

    assert main(["--port", "4763"]) == BIND_FAILURE_EXIT
    assert unmounts == [True]
    assert "Web bind failed: address already in use" in capsys.readouterr().out


def test_tailnet_identity_comes_from_self_user_id_and_tags_disable_login_check() -> None:
    untagged = parse_tailscale_status(
        json.dumps(
            {
                "BackendState": "Running",
                "Self": {"DNSName": "desk.example.ts.net.", "UserID": 42},
                "User": {"42": {"LoginName": "person@example.com"}},
            }
        )
    )
    tagged = parse_tailscale_status(
        json.dumps(
            {
                "BackendState": "Running",
                "Self": {"DNSName": "desk.example.ts.net.", "UserID": 42, "Tags": ["tag:server"]},
                "User": {"42": {"LoginName": "person@example.com"}},
            }
        )
    )

    assert untagged.login_name == "person@example.com"
    assert untagged.dns_name == "desk.example.ts.net"
    assert not untagged.tagged
    assert tagged.tagged


def test_prefixed_and_bare_routes_are_equivalent() -> None:
    assert _route_path("/api/ping?k=one") == ("/api/ping", {"k": ["one"]})
    assert _route_path("/omarvis/api/ping?k=one") == ("/api/ping", {"k": ["one"]})
    assert _route_path("/omarvis/?k=one") == ("/", {"k": ["one"]})


def _simulate_app(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("omarvis.web.catalog_variables", lambda **_kwargs: {"machine": "test"})
    monkeypatch.setattr("omarvis.web.load_catalog", lambda: {})
    events = []
    holder = {}

    def sink(event):
        events.append(dict(event))
        if event.get("action") == "end-local":
            holder["app"].ack_local_ended()

    controller = TailnetController(0, simulate=True)
    app = RemoteApplication(
        {"agent_id": "agent"},
        "",
        secret="pairing-secret",
        controller=controller,
        simulate=True,
        command_sink=sink,
        token_minter=lambda: ("simulate-token", "simulate-conversation", object()),
    )
    holder["app"] = app
    return app, events


def test_token_mint_registers_fresh_bound_session_without_start_grace(monkeypatch) -> None:
    app, events = _simulate_app(monkeypatch)
    try:
        assert app.session() is None
        first = app.mint_token()
        first_handler = app.session().handler
        first_handler._approved_categories.add("software")
        second = app.mint_token()

        assert first == {
            "token": "simulate-token",
            "conversation_id": "simulate-conversation",
            "dynamic_variables": {"machine": "test"},
            "local_ended": True,
        }
        assert second["local_ended"] is True
        assert app.session().conversation_id == "simulate-conversation"
        assert app.session().handler is not first_handler
        assert app.session().handler.scope == "agent"
        assert first_handler._approved_categories == set()
        assert [event for event in events if event.get("event") == "phone"][-2:] == [
            {"event": "phone", "active": False, "reason": "takeover"},
            {"event": "phone", "active": True},
        ]
    finally:
        app.stop()


def test_session_cap_and_ping_lifeline_are_authoritative(monkeypatch) -> None:
    app, _events = _simulate_app(monkeypatch)
    now = [0.0]
    app.clock = lambda: now[0]
    stream = app.broker.subscribe()
    try:
        app.mint_token()
        assert stream.get(timeout=0.1)["event"] == "context"
        now[0] = 14.0
        assert app.ping()
        now[0] = 28.0
        assert app.session() is not None
        now[0] = 30.0
        assert app.session() is None
        assert stream.get(timeout=0.1) == {"event": "ended", "reason": "expired"}

        now[0] = 100.0
        app.mint_token()
        now[0] = 401.0
        assert app.session() is None
    finally:
        app.broker.unsubscribe(stream)
        app.stop()


def test_token_reports_local_end_timeout_without_minting(monkeypatch) -> None:
    monkeypatch.setattr("omarvis.web.LOCAL_END_WAIT_SECONDS", 0.01)
    minted = []
    app = RemoteApplication(
        {"agent_id": "agent"},
        "",
        secret="pairing-secret",
        controller=TailnetController(0, simulate=True),
        command_sink=lambda _event: None,
        token_minter=lambda: minted.append(True),
    )
    try:
        assert app.mint_token() == {
            "token": "",
            "dynamic_variables": {},
            "local_ended": False,
        }
        assert minted == []
        assert app.session() is None
    finally:
        app.stop()


def test_remote_see_emits_file_only_after_handler_result(monkeypatch) -> None:
    app, _events = _simulate_app(monkeypatch)
    stream = app.broker.subscribe()
    try:
        app.mint_token()
        session = app.session()
        assert stream.get(timeout=0.1)["event"] == "context"
        session.handler.handle = lambda _parameters: {"status": "screenshot_uploaded"}
        session.handler.take_pending_screenshot = lambda key: "file-123" if key == "remote" else None

        result, file_id = app.run_tool({"command": "omarvis see"})

        assert result == {"status": "screenshot_uploaded"}
        assert file_id == "file-123"
        assert stream.get(timeout=0.1) == {"event": "see-ready", "file_id": "file-123"}
    finally:
        app.broker.unsubscribe(stream)
        app.stop()


def test_session_binding_rejects_calls_after_event_stream_closes(monkeypatch) -> None:
    app, _events = _simulate_app(monkeypatch)
    try:
        app.mint_token()
        session = app.attach_stream()
        assert session is not None
        assert app.note_transcript("yes")
        app.stream_closed(session)
        assert app.session() is None
        assert not app.note_transcript("no")
    finally:
        app.stop()


def test_http_security_and_session_contract(monkeypatch, tmp_path: Path) -> None:
    app, _events = _simulate_app(monkeypatch)
    index = tmp_path / "index.html"
    index.write_text("<style>/* OMARVIS_THEME */</style>ready", encoding="utf-8")
    vendor = tmp_path / "client.js"
    vendor.write_text("window.ElevenLabsClient = {};", encoding="utf-8")
    assets = ThemeAssets(tmp_path / "theme", index, vendor)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app, assets))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    def request(method, path, *, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        returned = response.status, dict(response.getheaders()), data
        connection.close()
        return returned

    post_headers = {
        "Authorization": "Bearer pairing-secret",
        "Content-Type": "application/json",
        "Origin": "https://omarvis.test.ts.net",
    }
    try:
        for path in (
            "/?k=wrong",
            "/background?k=wrong",
            "/api/events?k=wrong",
            "/vendor/elevenlabs-client-1.23.0.iife.js?k=wrong",
        ):
            assert request("GET", path)[0] == 403
        for path in ("/api/token", "/api/ping", "/api/transcript", "/api/run", "/api/session/end"):
            assert request(
                "POST",
                path,
                body="{}",
                headers={**post_headers, "Authorization": "Bearer wrong"},
            )[0] == 403
        assert request("GET", "/?k=pairing-secret", headers={"Host": "evil.example"})[0] == 400
        assert request(
            "POST",
            "/api/token",
            body="{}",
            headers={**post_headers, "Content-Type": "text/plain"},
        )[0] == 415
        app.expected_login = "person@example.com"
        assert request("GET", "/?k=pairing-secret")[0] == 403
        identity_headers = {"Tailscale-User-Login": "person@example.com"}
        post_headers.update(identity_headers)
        status, headers, body = request(
            "GET", "/omarvis/?k=pairing-secret", headers=identity_headers
        )
        assert status == 200
        assert b"ready" in body
        assert "Access-Control-Allow-Origin" not in headers

        status, headers, body = request(
            "GET",
            "/omarvis/vendor/elevenlabs-client-1.23.0.iife.js?k=pairing-secret",
            headers=identity_headers,
        )
        assert status == 200
        assert headers["Content-Type"] == "text/javascript; charset=utf-8"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert b"ElevenLabsClient" in body
        etag = headers["ETag"]
        assert request(
            "GET",
            "/vendor/elevenlabs-client-1.23.0.iife.js?k=pairing-secret",
            headers={**identity_headers, "If-None-Match": etag},
        )[0] == 304
        assert request("GET", "/vendor/elevenlabs-client-1.23.0.iife.js?k=wrong")[0] == 403

        assert request(
            "POST", "/api/ping", body="{}", headers={**post_headers, "Origin": "https://evil.test"}
        )[0] == 403
        assert request("POST", "/api/ping", body="{}", headers=post_headers)[0] == 409
        assert request("POST", "/api/run", body="{}", headers=post_headers)[0] == 409
        status, _headers, body = request(
            "POST", "/omarvis/api/token", body="{}", headers=post_headers
        )
        assert status == 200
        assert json.loads(body)["local_ended"] is True
        assert request(
            "POST",
            "/api/transcript",
            body=json.dumps({"text": "hello", "final": False}),
            headers=post_headers,
        )[0] == 400
        assert request(
            "POST",
            "/api/transcript",
            body=json.dumps({"text": "hello", "final": True}),
            headers=post_headers,
        )[0] == 200
    finally:
        app.stop()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_theme_and_background_are_inlined_with_conditional_metadata(tmp_path: Path) -> None:
    theme = tmp_path / "theme"
    theme.mkdir()
    (theme / "shell.toml").write_text(
        '[popups]\nbackground = "#101010"\ntext = "#eeeeee"\nborder = "#333333"\n',
        encoding="utf-8",
    )
    (theme / "colors.toml").write_text("accent = '#00ffaa'\n", encoding="utf-8")
    background = theme / "wall.png"
    background.write_bytes(b"png")
    (tmp_path / "background").symlink_to(Path("theme") / background.name)
    index = tmp_path / "index.html"
    index.write_text("<style>/* OMARVIS_THEME */</style>", encoding="utf-8")

    assets = ThemeAssets(theme, index)
    html = assets.html()
    result = assets.background()

    assert b"--omarvis-background:#101010" in html
    assert b"--omarvis-foreground:#eeeeee" in html
    assert result is not None
    assert result[0] == background
    assert result[1].startswith('"')
    assert result[2].endswith("GMT")
    assert result[3] == "image/png"


def test_phone_page_contains_pinned_session_and_relay_contract() -> None:
    page = (Path(__file__).parents[1] / "assets" / "web" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "elevenlabs-client-1.23.0.iife.js?k=OMARVIS_BACKGROUND_KEY" in page
    assert 'await post("./api/token")' in page
    assert "await openEvents()" in page
    assert "Conversation.startSession" in page
    assert "dynamicVariables: token.dynamic_variables || {}" in page
    assert "clientTools: {run: relayRun}" in page
    assert "started.getId() !== expectedConversationId" in page
    assert 'role === "user"' in page
    assert '{text: message, final: true}' in page
    assert 'post("./api/ping")' in page
    assert "sendContextualUpdate" in page
    assert "sendMultimodalMessage" in page
    assert page.index("return JSON.stringify(result)") < page.index("setTimeout(flushSeeReady, 0)")


def test_vendored_browser_sdk_matches_recorded_integrity() -> None:
    bundle = (
        Path(__file__).parents[1]
        / "assets"
        / "web"
        / "vendor"
        / "elevenlabs-client-1.23.0.iife.js"
    ).read_bytes()

    assert hashlib.sha256(bundle).hexdigest() == (
        "b6adb12bd5df649af3ce3ac9205fd0e7d1c099513481c58bd1990f2d50903204"
    )
