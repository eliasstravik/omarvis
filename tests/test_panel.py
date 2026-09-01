import json
from pathlib import Path


ROOT = Path(__file__).parent.parent


def panel_source():
    return (ROOT / "Panel.qml").read_text()


def test_manifest_repoints_existing_bar_widget_key_without_identity_migration():
    manifest = json.loads((ROOT / "manifest.json").read_text())

    assert manifest["id"] == "omarvis.voice"
    assert manifest["kinds"] == ["service", "bar-widget"]
    assert manifest["entryPoints"]["barWidget"] == "Panel.qml"
    assert manifest["barWidget"]["allowMultiple"] is False
    assert manifest["barWidget"]["defaults"]["showLabel"] is False


def test_native_panel_uses_stock_popup_coordinator_and_service_ipc():
    panel = panel_source()

    assert "import qs.Ui" in panel
    assert "Panel {" in panel
    assert "KeyboardPanel {" in panel
    assert "owner: root" in panel
    assert "manageIpc: false" in panel
    assert 'ipcTarget: "omarvis"' not in panel
    assert "function onPanelRequested() { root.open() }" in panel
    assert "service.panelOpen = root.opened" in panel


def test_bar_click_glyph_and_tooltip_contracts_are_preserved():
    panel = panel_source()

    assert "BarIconButton {" in panel
    assert "if (buttonCode === Qt.LeftButton) root.toggle()" in panel
    assert "if (hasLocalError)" in panel
    assert "if (runningCommand)" in panel
    assert 'dictationState === "recording"' in panel
    assert 'sessionState === "thinking"' in panel
    assert 'sessionState === "speaking"' in panel
    assert "if (phoneRunningCommand)" in panel
    assert "if (phoneSessionActive)" in panel
    assert 'root.remoteState !== "off"' in panel


def test_panel_contains_all_approved_stage_one_sections():
    panel = panel_source()

    for text in (
        "Start Agent",
        "Start Ask",
        "End session",
        "LAST EXCHANGE",
        "REMOTE",
        "End phone session",
        "Open the Tailscale panel",
        "sudo tailscale set --operator=",
    ):
        assert text in panel
    assert '["wl-copy", "--", value]' in panel
    assert "Canvas {" in panel
    assert "ctx.fillRect" in panel
    assert "Repeater" not in panel


def test_dictation_history_is_delegated_exclusively_to_omarchy_clipboard():
    panel = panel_source()
    service = (ROOT / "Service.qml").read_text()

    assert "LAST DICTATION" not in panel
    assert "lastDictation" not in panel
    assert "lastDictation" not in service


def test_remote_switch_is_live_and_qr_uses_one_canvas():
    panel = panel_source()
    remote = panel.split('text: "REMOTE"', 1)[1]

    assert "Toggle {" in remote
    assert 'label: "Remote access"' in remote
    assert "onClicked: if (root.svc) root.svc.setRemoteEnabled(!root.svc.remoteEnabled)" in remote
    assert 'text: root.svc && root.svc.remoteEnabled ? "ON" : "OFF"' in remote
    assert "foreground: checked ? root.accent : root.foreground" in remote
    assert "accent: root.accent" in remote
    assert "The phone must be on your tailnet." in remote
    assert remote.count("Canvas {") == 1
    assert "Repeater" not in remote
