from pathlib import Path


def test_ipc_exports_agent_compatibility_and_typed_mode_routes():
    service = (Path(__file__).parent.parent / "Service.qml").read_text()

    assert 'function toggle(): string { return root.toggle("agent") }' in service
    assert 'function toggleMode(mode: string): string { return root.toggle(mode) }' in service
    assert 'function start(): string { return root.start("agent") }' in service
    assert 'function startMode(mode: string): string { return root.start(mode) }' in service
    assert 'function toggle(mode = "agent")' not in service.split("IpcHandler", 1)[1]
