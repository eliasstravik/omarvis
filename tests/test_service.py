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
    assert "root.inLevel = 0.0" in service.split("onExited", 1)[1]
    assert "root.outLevel = 0.0" in service.split("onExited", 1)[1]
    assert 'root.streamingAgent = ""' in service.split("onExited", 1)[1]
    assert 'root.runningCommand = ""' in service.split("onExited", 1)[1]
    assert "HudWindow" in service
