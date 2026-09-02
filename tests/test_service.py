from pathlib import Path


def test_ipc_exports_one_modeless_session_route():
    service = (Path(__file__).parent.parent / "Service.qml").read_text()

    # Agent is the only session type, so start/stop/toggle take no arguments
    # and the mode-selecting IPC routes are gone for good.
    assert "function toggle(): string { return root.toggle() }" in service
    assert "function start(): string { return root.start() }" in service
    assert "function stop(): string { return root.stop() }" in service
    assert "toggleMode" not in service
    assert "startMode" not in service
    assert "currentMode" not in service
    assert "pendingMode" not in service
    assert "normalizeMode" not in service
    assert '"--mode"' not in service


def test_remote_failures_go_to_the_notification_server_once_per_state():
    service = (Path(__file__).parent.parent / "Service.qml").read_text()

    # The panel gets one short line; the repair instructions are too long for
    # it, so they take the same route the voice errors do.
    assert "onRemoteStateChanged:" in service
    assert "notifiedRemoteState" in service
    assert "function remoteProblemDetail(state): string" in service
    assert '"Omarvis remote access"' in service
    assert "sudo tailscale set --operator=" in service


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


def test_service_routes_handsfree_and_cancel_and_manages_escape_bind():
    service = (Path(__file__).parent.parent / "Service.qml").read_text()

    assert "handsfree: \"locked\"" in service
    assert "cancel: \"canceled\"" in service
    assert "omarvis-dictate" in service  # the Hyprland submap from bindings.lua
    # Omarchy's lua parser rejects keyword-style binds; must go via eval.
    assert "hyprctl eval" in service
    assert '_G.omarvis_escape = hl.bind(\\"ESCAPE\\"' in service
    assert "omarchy-shell omarvis esc" in service
    # Teardown removes only this handle; a global hl.unbind("ESCAPE") would
    # also strip the ESCAPE binds inside the dictation submaps.
    assert "_G.omarvis_escape:unbind()" in service
    assert 'hl.unbind(\\"ESCAPE\\")' not in service
    assert "omarvis-handsfree" in service
    # Hyprland's Lua config layer rejects `hyprctl dispatch submap NAME`; the
    # switch has to go through the Lua dispatcher API.
    assert 'hl.dispatch(hl.dsp.submap(' in service
    assert '"dispatch", "submap"' not in service
    assert "onEscapeBindWantedChanged: updateEscapeBind()" in service


def test_escape_ends_whatever_is_live_with_dictation_first():
    service = (Path(__file__).parent.parent / "Service.qml").read_text()

    # Escape is live for a dictation recording OR any non-idle session, and
    # the hands-free chord lives in the dictation submap, so the menu
    # keeps working during voice sessions.
    assert 'escapeLive: sessionState !== "idle" && sessionState !== "error"' in service
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
    assert 'message + "\\nDictation was copied to clipboard."' in service


def test_service_tracks_hands_free_dictation_lock():
    service = (Path(__file__).parent.parent / "Service.qml").read_text()

    assert "property bool dictationLocked: false" in service
    assert "root.dictationLocked = !!event.locked" in service
    assert 'if (nextState !== "recording") root.dictationLocked = false' in service
    assert "dictationLocked: root.dictationLocked" in service


def test_panel_ipc_and_escape_precedence_are_service_owned():
    service = (Path(__file__).parent.parent / "Service.qml").read_text()

    assert "property bool panelOpen: false" in service
    assert "signal panelRequested()" in service
    assert 'escapeBindWanted: escapeLive && !panelOpen' in service
    assert "if (root.escapeBindWanted)" in service
    assert 'function panel(): string { root.panelRequested(); return "opening" }' in service


def test_phone_state_is_separate_from_local_hud_state():
    service = (Path(__file__).parent.parent / "Service.qml").read_text()

    for declaration in (
        'property bool remoteEnabled: false',
        'property string remoteState: "off"',
        'property string remoteError: ""',
        'property string remoteUrl: ""',
        'property var qrMatrix: []',
        'property bool phoneSessionActive: false',
        'property string phoneRunningCommand: ""',
    ):
        assert declaration in service
    assert 'escapeLive: sessionState !== "idle" && sessionState !== "error"' in service
    escape_live = service.split("readonly property bool escapeLive", 1)[1].split("readonly property bool escapeBindWanted", 1)[0]
    assert "phoneSessionActive" not in escape_live


def test_remote_daemon_is_supervised_and_marker_is_direction_of_truth():
    service = (Path(__file__).parent.parent / "Service.qml").read_text()

    for expected in (
        'command: [root.pluginDir + "/bin/omarvis-web"]',
        'command: [root.pluginDir + "/bin/omarvis-web", "--cleanup"]',
        'webDaemon.write("end-session\\n")',
        'webDaemon.write("ack-local-ended\\n")',
        "if (exitCode === 3)",
        "webRestart.restart()",
        'path: Quickshell.env("HOME") + "/.local/share/omarvis/remote-enabled"',
        "onFileChanged: reload()",
        "function setRemote(enabled: bool): string { return root.setRemoteEnabled(enabled) }",
        'return "retrying"',
    ):
        assert expected in service


def test_keybindings_are_parsed_live_from_the_hyprland_bindings_file():
    service = (Path(__file__).parent.parent / "Service.qml").read_text()

    # The panel shows the user's real mappings, not the defaults: the
    # bindings file is watched and re-parsed on every save.
    assert '"/.config/hypr/bindings.lua"' in service
    assert "onLoaded: root.parseKeybindings(text())" in service
    assert "onFileChanged: reload()" in service
    # Release-pair lines and Lua comments never produce rows.
    assert 'indexOf("--") === 0' in service
    for match in (
        'omarchy-shell omarvis dictate start"',
        'omarchy-shell omarvis dictate handsfree"',
        'omarchy-shell omarvis toggle"',
        'omarchy-shell omarvis toggleRemote"',
        'omarchy-shell omarvis panel"',
    ):
        assert match in service
    # Hands-free is a chord: it displays as the dictation keys plus the
    # chord binding's final key (SUPER + J + SPACE), composed from both
    # live bindings rather than showing the raw SUPER + SPACE bind.
    assert 'found[d].keys + " + " + chordKey' in service


def test_hands_free_displays_as_a_chord_on_top_of_the_dictation_keys():
    service = (Path(__file__).parent.parent / "Service.qml").read_text()

    # SPACE is pressed while the dictation keys are still held, so the row
    # composes both live bindings ("SUPER + J + SPACE"), never the raw
    # SUPER+SPACE binding on its own.
    assert 'found[d].keys + " + " + chordKey' in service
    assert 'split("+").pop().trim()' in service
