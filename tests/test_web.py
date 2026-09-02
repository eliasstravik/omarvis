from __future__ import annotations

import http.client
import json
import os
import queue
import re
import stat
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from omarvis.web import (
    BIND_FAILURE_EXIT,
    FONT_DIR,
    FONT_FILES,
    CommandResult,
    EventBroker,
    RemoteApplication,
    TailnetController,
    ThemeAssets,
    _route_path,
    make_handler,
    main,
    parse_tailscale_status,
    remove_mount,
    run_command,
    stable_secret,
    BoundedHTTPServer,
    BrokerFull,
)
from omarvis.privatefiles import PrivateFileError


def test_stable_secret_is_reused_and_private(tmp_path: Path) -> None:
    path = tmp_path / "state" / "web-secret"
    first = stable_secret(path)
    second = stable_secret(path)

    assert first == second
    assert len(first) >= 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_stable_secret_refuses_a_planted_symlink(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("not-a-secret")
    path = tmp_path / "state" / "web-secret"
    path.parent.mkdir()
    path.symlink_to(elsewhere)

    with pytest.raises(PrivateFileError):
        stable_secret(path)


def test_stable_secret_refuses_a_fifo(tmp_path: Path) -> None:
    path = tmp_path / "state" / "web-secret"
    path.parent.mkdir()
    os.mkfifo(path)

    with pytest.raises(PrivateFileError):
        stable_secret(path)


def test_event_broker_drops_oldest_events_for_slow_subscribers() -> None:
    broker = EventBroker(backlog=3)
    stream = broker.subscribe()
    for index in range(5):
        broker.publish({"event": "context", "index": index})

    drained = []
    while True:
        try:
            drained.append(stream.get_nowait()["index"])
        except queue.Empty:
            break
    assert drained == [2, 3, 4]
    assert broker.dropped == 2


def test_run_command_kills_output_floods_and_reports_them() -> None:
    result = run_command(["yes"])

    assert result.returncode == 125
    assert "too much output" in result.stderr


def test_run_command_times_out_process_group(monkeypatch) -> None:
    monkeypatch.setattr("omarvis.web.HELPER_TIMEOUT_SECONDS", 0.2)

    result = run_command(["bash", "-c", "sleep 34.5 & sleep 34.5"])

    assert result.returncode == 124
    assert subprocess.run(
        ["pgrep", "-f", "^sleep 34.5$"], capture_output=True, text=True, check=False
    ).stdout == ""


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

    monkeypatch.setattr("omarvis.web.BoundedHTTPServer", fail_bind)

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
        assert first_handler._approved_categories == set()
        assert [event for event in events if event.get("event") == "phone"][-2:] == [
            {"event": "phone", "active": False, "reason": "takeover"},
            {"event": "phone", "active": True},
        ]
    finally:
        app.stop()


def test_takeover_and_end_session_terminate_the_old_handlers_processes(monkeypatch) -> None:
    from omarvis.process import execute_process

    app, _events = _simulate_app(monkeypatch)
    try:
        app.mint_token()
        first_handler = app.session().handler
        launched = execute_process(
            ["sleep", "35.5"],
            timeout=0.05,
            kill_on_timeout=False,
            stdout_limit=10,
            supervisor=first_handler.supervisor,
        )
        assert launched.started
        assert len(first_handler.supervisor.live()) == 1

        app.mint_token()

        deadline = time.monotonic() + 5.0
        while first_handler.supervisor.live() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert first_handler.supervisor.live() == ()
        assert subprocess.run(
            ["pgrep", "-f", "^sleep 35.5$"], capture_output=True, text=True, check=False
        ).stdout == ""
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
            "/fonts/jetbrains-mono-nf-regular.woff2?k=wrong",
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

        # The phone is not an Omarchy machine, so the Nerd Font the state
        # glyphs live in has to be served alongside the page.
        status, headers, body = request(
            "GET",
            "/omarvis/fonts/jetbrains-mono-nf-regular.woff2?k=pairing-secret",
            headers=identity_headers,
        )
        assert status == 200
        assert headers["Content-Type"] == "font/woff2"
        assert headers["Cache-Control"] == "private, max-age=31536000, immutable"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert body[:4] == b"wOF2"
        assert request(
            "GET",
            "/fonts/jetbrains-mono-nf-regular.woff2?k=pairing-secret",
            headers={**identity_headers, "If-None-Match": headers["ETag"]},
        )[0] == 304
        # Only the two shipped faces are reachable; the route is not a
        # directory server.
        assert request(
            "GET", "/fonts/../web-secret?k=pairing-secret", headers=identity_headers
        )[0] == 404
        assert request(
            "GET", "/background?k=pairing-secret", headers=identity_headers
        )[0] == 404

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


def test_theme_tokens_cover_every_surface_the_phone_page_paints(tmp_path: Path) -> None:
    theme = tmp_path / "theme"
    theme.mkdir()
    (theme / "shell.toml").write_text(
        "[bar]\nactive = \"#ff0055\"\n"
        "[popups]\nbackground = \"#101010\"\ntext = \"#eeeeee\"\n"
        "border = \"hyprland.active-border\"\n"
        "[hyprland]\nactive-border = \"#123456\"\n"
        "[polkit]\ntext-error = \"#ff2200\"\n",
        encoding="utf-8",
    )
    (theme / "colors.toml").write_text(
        "accent = '#00ffaa'\nbackground = '#050505'\nforeground = '#cccccc'\n",
        encoding="utf-8",
    )
    index = tmp_path / "index.html"
    index.write_text("<style>/* OMARVIS_THEME */</style>", encoding="utf-8")

    html = ThemeAssets(theme, index).html().decode()

    # The page ground is the theme background; the card is the popups
    # surface; accent is the theme accent and bar.active stays the separate
    # hot-microphone color, exactly as the QML surfaces split them.
    assert "--omarvis-background:#050505" in html
    assert "--omarvis-surface:#101010" in html
    assert "--omarvis-foreground:#cccccc" in html
    assert "--omarvis-text:#eeeeee" in html
    assert "--omarvis-accent:#00ffaa" in html
    assert "--omarvis-active:#ff0055" in html
    assert "--omarvis-urgent:#ff2200" in html
    # Indirections through shell sections still resolve.
    assert "--omarvis-border:#123456" in html


def test_theme_tokens_fall_back_without_a_theme(tmp_path: Path) -> None:
    index = tmp_path / "index.html"
    index.write_text("<style>/* OMARVIS_THEME */</style>", encoding="utf-8")

    html = ThemeAssets(tmp_path / "missing", index).html().decode()

    for token in (
        "background",
        "surface",
        "foreground",
        "text",
        "accent",
        "active",
        "urgent",
        "border",
    ):
        assert f"--omarvis-{token}:#" in html


def test_every_theme_token_the_page_uses_is_substituted() -> None:
    page = (Path(__file__).parents[1] / "assets" / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    html = ThemeAssets().html().decode()

    used = set(re.findall(r"var\(--omarvis-([a-z]+)\)", page))
    defined = set(re.findall(r"--omarvis-([a-z]+):", html))

    assert used
    assert used <= defined


def test_phone_page_contains_pinned_session_and_relay_contract() -> None:
    page = (Path(__file__).parents[1] / "assets" / "web" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "elevenlabs-client-1.23.0.iife.js?k=OMARVIS_PAIRING_KEY" in page
    assert 'await post("./api/token")' in page
    assert "await openEvents()" in page
    assert page.index("await openEvents()") < page.index("Conversation.startSession")
    assert "dynamicVariables: token.dynamic_variables || {}" in page
    assert "clientTools: {run: relayRun}" in page
    assert "started.getId() !== expectedConversationId" in page
    assert 'role === "user"' in page
    assert "{text: message, final: true}" in page
    assert 'post("./api/ping")' in page
    assert "}, 5000);" in page
    assert "sendContextualUpdate" in page
    assert "sendMultimodalMessage" in page
    assert page.index("return JSON.stringify(result)") < page.index("setTimeout(flushSeeReady, 0)")
    assert "env(safe-area-inset-bottom)" in page
    assert "@media (prefers-reduced-motion: reduce)" in page
    assert "@media (max-width: 360px)" in page


def test_phone_page_is_a_touchable_osd_not_a_glassy_card() -> None:
    page = (Path(__file__).parents[1] / "assets" / "web" / "index.html").read_text(
        encoding="utf-8"
    )

    # Omarchy surfaces are opaque, square and hairline-bordered. Nothing on
    # this page may be rounded, blurred, shadowed, or laid over the wallpaper.
    assert "backdrop-filter" not in page
    assert "box-shadow" not in page
    assert "./background" not in page
    assert "OMARVIS_BACKGROUND_KEY" not in page
    for radius in re.findall(r"border-radius:\s*([^;]+);", page):
        assert radius.strip() in {"0"}, radius
    assert not re.search(r"\btransform:\s*scale", page)

    # One card, one state glyph, one flat 6px meter, one full-width action.
    assert "border: 2px solid var(--omarvis-accent)" in page
    assert "background: var(--omarvis-surface)" in page
    assert "background: var(--omarvis-background)" in page
    assert "height: 6px" in page
    assert "color-mix(in srgb, var(--omarvis-text) 45%, transparent)" in page
    assert "transition: width 140ms cubic-bezier(0.33, 1, 0.68, 1)" in page
    assert "min-height: 72px" in page
    assert "color: var(--omarvis-background)" in page
    assert "background: var(--omarvis-accent)" in page
    # Secondary states use the [controls] foreground-tint alphas.
    assert "color-mix(in srgb, var(--omarvis-text) 8%, transparent)" in page
    assert "color-mix(in srgb, var(--omarvis-text) 4%, transparent)" in page

    # The 2x2 MIC/AGENT grid, the tailnet pill and the brand heading are gone.
    assert "tailnet only" not in page
    assert "<h1" not in page
    assert 'class="dot"' not in page
    assert ">MIC<" not in page
    assert ">AGENT<" not in page


def test_phone_page_ships_its_own_nerd_font_and_shares_the_hud_vocabulary() -> None:
    page = (Path(__file__).parents[1] / "assets" / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    hud = (Path(__file__).parents[1] / "HudWindow.qml").read_text(encoding="utf-8")

    assert page.count("@font-face") == 2
    assert 'font-family: "JetBrainsMono NF", monospace' in page
    assert "./fonts/jetbrains-mono-nf-regular.woff2?k=OMARVIS_PAIRING_KEY" in page
    assert "./fonts/jetbrains-mono-nf-bold.woff2?k=OMARVIS_PAIRING_KEY" in page
    assert "font-display: swap" in page
    for name in FONT_FILES:
        font = FONT_DIR / name
        assert font.exists()
        assert font.read_bytes()[:4] == b"wOF2"

    # Same four-glyph vocabulary as the desktop HUD: hourglass (waiting),
    # microphone (your floor), speaker (agent's voice), wave (goodbye), plus
    # the failure alert. Nothing else — no tool glyphs, no ✓, no thinking.
    for codepoint in ("0xF036C", "0xF051F", "0xF057E", "0xF0026"):
        assert codepoint in page
        assert codepoint in hud
    for retired in ("toolGlyphFor", '"✓"', "0xF0100", "0xF03D8", "0xF0425",
                    "0xF05B7", "0xF01D8", "0xF0772"):
        assert retired not in page
    assert page.index("waiting: String.fromCodePoint(0xF051F)") > 0
    # The header names the product, not the hostname.
    assert '.textContent = "Omarvis"' in page
    assert "animation: omarvis-pulse 1900ms ease-in-out infinite" in page
    assert "@keyframes omarvis-pulse" in page
    assert 'elements.talkLabel.textContent = sweeping ? "Connecting"' in page

    # No visible prose on the page: status is screen-reader-only, and the
    # explainer sentence is gone for good.
    assert "Starting here ends" not in page
    assert "<footer>" not in page
    assert 'class="sr" id="status"' in page
    # The microphone is requested once and kept muted between calls so the
    # permission prompt cannot repeat within a page load.
    assert "getUserMedia" in page
    assert "function ensureMic" in page
    assert "pagehide" in page


def test_setup_pins_the_browser_sdk_download() -> None:
    # The SDK bundle is downloaded by omarvis-setup rather than vendored, so
    # the pin lives in the setup helper: exact version, registry source, the
    # sha256 of the tarball, and the sha256 of dist/lib.iife.js from
    # @elevenlabs/client 1.23.0.
    from omarvis.setupfiles import ELEVENLABS_CLIENT, ELEVENLABS_CLIENT_VERSION
    from omarvis.web import VENDOR_ASSET, VENDOR_ROUTE

    assert ELEVENLABS_CLIENT_VERSION == "1.23.0"
    assert ELEVENLABS_CLIENT.url == (
        "https://registry.npmjs.org/@elevenlabs/client/-/client-1.23.0.tgz"
    )
    assert ELEVENLABS_CLIENT.member == "package/dist/lib.iife.js"
    assert ELEVENLABS_CLIENT.member_sha256 == (
        "b6adb12bd5df649af3ce3ac9205fd0e7d1c099513481c58bd1990f2d50903204"
    )
    assert ELEVENLABS_CLIENT.tarball_sha256 == (
        "4c7be4be814674f625d7aa71c79ea4c36913a81413b353498e136953f06f570c"
    )
    # The installed filename, the served route, and the page's script tag
    # must all agree or the phone page loads nothing.
    filename = VENDOR_ROUTE.rsplit("/", 1)[-1]
    assert filename == "elevenlabs-client-1.23.0.iife.js"
    assert ELEVENLABS_CLIENT.destination == VENDOR_ASSET
    page = (Path(__file__).parents[1] / "assets" / "web" / "index.html").read_text(encoding="utf-8")
    assert f'src="./vendor/{filename}?k=OMARVIS_PAIRING_KEY"' in page


def test_phone_infers_thinking_from_the_final_user_transcript() -> None:
    page = (
        Path(__file__).parents[1] / "assets" / "web" / "index.html"
    ).read_text(encoding="utf-8")

    # The SDK emits no thinking event; the final user transcript is the
    # thinking signal, cleared on any mode change and by a failsafe timer.
    assert "if (!agentSpeaking) setPondering(true);" in page
    assert "setPondering(false);" in page
    assert "(runInFlight || pondering) && live" in page
    assert "}, 12000);" in page


# ----------------------------------------------- Connection cardinality


def _bounded_server(monkeypatch, tmp_path: Path, *, max_connections: int, timeout: float = 1.0):
    app, _events = _simulate_app(monkeypatch)
    monkeypatch.setattr("omarvis.web.REQUEST_TIMEOUT_SECONDS", timeout)
    index = tmp_path / "index.html"
    index.write_text("<style>/* OMARVIS_THEME */</style>ready", encoding="utf-8")
    vendor = tmp_path / "client.js"
    vendor.write_text("window.ElevenLabsClient = {};", encoding="utf-8")
    assets = ThemeAssets(tmp_path / "theme", index, vendor)
    server = BoundedHTTPServer(("127.0.0.1", 0), make_handler(app, assets), max_connections=max_connections)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return app, server, thread


def _wait(predicate, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_connection_cap_rejects_excess_clients_before_any_header_is_read(monkeypatch, tmp_path: Path) -> None:
    import socket

    app, server, thread = _bounded_server(monkeypatch, tmp_path, max_connections=2, timeout=1.0)
    port = server.server_address[1]
    stalled = []
    try:
        # Two clients connect and send nothing: they hold both slots.
        for _ in range(2):
            stalled.append(socket.create_connection(("127.0.0.1", port)))
        assert _wait(lambda: server.active_connections() == 2)

        # The third gets a bare 503 and is closed without a thread or parse.
        third = socket.create_connection(("127.0.0.1", port))
        third.settimeout(3.0)
        answer = third.recv(200)
        assert answer.startswith(b"HTTP/1.1 503")
        assert third.recv(10) == b""
        third.close()
        assert server.rejected == 1

        # The stalled clients hit the read deadline and free their slots.
        assert _wait(lambda: server.active_connections() == 0, seconds=5.0)
        for connection in stalled:
            connection.settimeout(2.0)
            assert connection.recv(10) == b""

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request("GET", "/?k=wrong")
        assert connection.getresponse().status == 403
        connection.close()
    finally:
        for connection in stalled:
            connection.close()
        app.stop()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_event_stream_slots_are_capped_independently_of_queues() -> None:
    broker = EventBroker(backlog=2, max_subscribers=1)
    first = broker.subscribe()
    with pytest.raises(BrokerFull):
        broker.subscribe()
    assert broker.refused == 1
    assert broker.subscriber_count() == 1
    broker.unsubscribe(first)
    broker.subscribe()
    assert broker.subscriber_count() == 1


def test_event_stream_request_gets_503_when_no_slot_is_free(monkeypatch, tmp_path: Path) -> None:
    app, server, thread = _bounded_server(monkeypatch, tmp_path, max_connections=4, timeout=3.0)
    app.broker = EventBroker(max_subscribers=0)
    port = server.server_address[1]
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request("GET", "/api/events?k=pairing-secret")
        response = connection.getresponse()
        assert response.status == 503
        assert json.loads(response.read())["error"] == "event stream busy"
        connection.close()
        # The refused stream also released the session it had attached to.
        assert app.session() is None
    finally:
        app.stop()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_server_close_shuts_every_open_connection_deterministically(monkeypatch, tmp_path: Path) -> None:
    import socket

    app, server, thread = _bounded_server(monkeypatch, tmp_path, max_connections=4, timeout=30.0)
    port = server.server_address[1]
    held = [socket.create_connection(("127.0.0.1", port)) for _ in range(3)]
    try:
        assert _wait(lambda: server.active_connections() == 3)
        app.stop()
        server.shutdown()
        started = time.monotonic()
        server.server_close()
        for connection in held:
            connection.settimeout(3.0)
            assert connection.recv(10) == b""
        assert time.monotonic() - started < 3.0
        assert _wait(lambda: server.active_connections() == 0)
    finally:
        for connection in held:
            connection.close()
        thread.join(timeout=2)


def test_request_log_never_contains_the_pairing_key(monkeypatch, tmp_path: Path, capsys) -> None:
    app, server, thread = _bounded_server(monkeypatch, tmp_path, max_connections=4, timeout=3.0)
    port = server.server_address[1]
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request("GET", "/omarvis/?k=pairing-secret")
        assert connection.getresponse().status == 200
        connection.close()
    finally:
        app.stop()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    logged = capsys.readouterr().err
    assert '"GET /omarvis/" 200' in logged
    assert "pairing-secret" not in logged


def test_served_assets_are_read_once_through_descriptors_and_bounded(tmp_path: Path) -> None:
    index = tmp_path / "index.html"
    index.write_text("<style>/* OMARVIS_THEME */</style>ready", encoding="utf-8")
    victim = tmp_path / "victim.js"
    victim.write_text("leak", encoding="utf-8")
    vendor = tmp_path / "vendor" / "client.js"
    vendor.parent.mkdir()
    vendor.symlink_to(victim)
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    font = fonts / "jetbrains-mono-nf-regular.woff2"
    font.write_bytes(b"wOF2" + b"\0" * 10)
    assets = ThemeAssets(tmp_path / "theme", index, vendor, fonts)

    # A planted symlink at the vendor path is refused, not followed.
    assert assets.vendor() is None
    served = assets.font("jetbrains-mono-nf-regular.woff2")
    assert served is not None
    body, etag, _modified, content_type = served
    assert body.startswith(b"wOF2") and content_type == "font/woff2" and etag.startswith('"')
    # Later changes on disk are irrelevant: the bytes were taken once.
    font.write_bytes(b"changed")
    assert assets.font("jetbrains-mono-nf-regular.woff2")[0].startswith(b"wOF2")
    assert assets.font("../web-secret") is None
