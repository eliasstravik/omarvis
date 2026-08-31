from pathlib import Path


def test_ipc_exports_agent_compatibility_and_typed_mode_routes():
    service = (Path(__file__).parent.parent / "Service.qml").read_text()

    assert 'function toggle(): string { return root.toggle("agent") }' in service
    assert 'function toggleMode(mode: string): string { return root.toggle(mode) }' in service
    assert 'function start(): string { return root.start("agent") }' in service
    assert 'function startMode(mode: string): string { return root.start(mode) }' in service
    assert 'function toggle(mode = "agent")' not in service.split("IpcHandler", 1)[1]


def test_service_wires_hud_event_protocol_and_clears_stale_state():
    service = (Path(__file__).parent.parent / "Service.qml").read_text()

    assert 'event.event === "level"' in service
    assert 'event.event === "agent_part"' in service
    assert 'event.event === "running"' in service
    assert 'event.event === "ran"' in service
    # The HUD shows no command text, so the bar tooltip needs the last
    # executed command from the service.
    assert "root.lastCommand = String(event.command" in service
    assert "lastCommand: root.lastCommand" in service
    assert "root.inLevel = 0.0" in service.split("onExited", 1)[1]
    assert "root.outLevel = 0.0" in service.split("onExited", 1)[1]
    assert 'root.streamingAgent = ""' in service.split("onExited", 1)[1]
    assert 'root.runningCommand = ""' in service.split("onExited", 1)[1]
    assert "HudWindow" in service


def test_service_routes_handsfree_and_cancel_and_manages_keybind_marker():
    service = (Path(__file__).parent.parent / "Service.qml").read_text()

    assert "handsfree: \"locked\"" in service
    assert "cancel: \"canceled\"" in service
    assert "omarvis-dictating" in service
    # Omarchy's lua parser rejects keyword-style binds; must go via eval.
    assert "hyprctl eval" in service
    assert 'o.bind(\\"ESCAPE\\"' in service
    assert "omarchy-shell omarvis esc" in service
    assert 'hl.unbind(\\"ESCAPE\\")' in service
    assert "onDictationStateChanged: updateDictationMarker()" in service
    assert "onEscapeLiveChanged: updateEscapeBind()" in service


def test_escape_ends_whatever_is_live_with_dictation_first():
    service = (Path(__file__).parent.parent / "Service.qml").read_text()

    # Escape is live for a dictation recording OR any non-idle session, and
    # the marker (SUPER+SPACE hands-free) stays dictation-only so the menu
    # keeps working during voice sessions.
    assert 'escapeLive: dictationState === "recording"' in service
    assert '(sessionState !== "idle" && sessionState !== "error")' in service
    assert "function esc(): string { return root.escapeAction() }" in service
    body = service.split("function escapeAction")[1]
    assert body.index('root.dictate("cancel")') < body.index("root.stop()")
    assert 'root.sessionState = "idle"' in body


def test_errors_are_routed_to_the_notification_server():
    service = (Path(__file__).parent.parent / "Service.qml").read_text()

    # The HUD is text-free, so error text goes where readable text belongs.
    assert "onLastErrorChanged: if (lastError) notifyError(lastError)" in service
    assert '"omarchy-notification-send"' in service
    assert '"Omarvis voice error"' in service


def test_service_tracks_hands_free_dictation_lock():
    service = (Path(__file__).parent.parent / "Service.qml").read_text()

    assert "property bool dictationLocked: false" in service
    assert "root.dictationLocked = !!event.locked" in service
    assert 'if (nextState !== "recording") root.dictationLocked = false' in service
    assert "dictationLocked: root.dictationLocked" in service
