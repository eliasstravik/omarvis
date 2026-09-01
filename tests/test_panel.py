import json
import re
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
    # The state word is opt-in; the bar row is the glyph.
    assert manifest["barWidget"]["defaults"]["showLabel"] is False
    assert [entry["key"] for entry in manifest["barWidget"]["schema"]] == ["showLabel"]


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


def test_bar_click_and_glyph_contracts_are_preserved_with_no_hover_card():
    panel = panel_source()

    assert "BarIconButton {" in panel
    assert "if (buttonCode === Qt.LeftButton) root.toggle()" in panel
    assert "if (hasLocalError)" in panel
    assert "if (runningCommand)" in panel
    assert 'dictationState === "recording"' in panel
    assert 'sessionState === "speaking"' in panel
    assert "if (phoneRunningCommand)" in panel
    assert "if (phoneSessionActive)" in panel
    assert 'root.setting("showLabel", false)' in panel
    # No hover card anywhere: the glyph is the whole story and the panel is
    # one click away.
    assert "tooltipText" not in panel
    assert "showTooltip" not in panel


def test_panel_has_no_mode_choice_and_one_session_action():
    panel = panel_source()

    # Ask mode is gone: one button, "Start" or "End", and nothing that could
    # pick a session type.
    assert "Start Ask" not in panel
    assert "Start Agent" not in panel
    assert "0xF02D6" not in panel
    assert "currentMode" not in panel
    assert 'startMode' not in panel
    # "Talk" matches the phone page's button, so both surfaces speak the
    # same pair: Talk and End.
    assert 'root.localSessionLive ? "End" : "Talk"' in panel
    assert "onClicked: root.toggleSession()" in panel
    assert "foreground: root.localSessionLive ? root.urgent : root.foreground" in panel
    # One BarIconButton plus exactly three panel Buttons: the session action,
    # the QR copy glyph, and end-phone-session. Nothing else is clickable.
    assert panel.count("BarIconButton {") == 1
    assert panel.count("Button {") - panel.count("BarIconButton {") == 3


def test_panel_is_a_power_panel_sized_glance_not_a_scroll_view():
    panel = panel_source()

    # Same anatomy as the stock power panel: hero, one action, a separator,
    # one settings section. Nothing scrolls, nothing streams text.
    assert "Flickable" not in panel
    assert "ScrollBar" not in panel
    assert "LAST EXCHANGE" not in panel
    assert "lastUser" not in panel
    assert "lastAgent" not in panel
    assert "streamingAgent" not in panel
    assert panel.count("PanelHero {") == 1
    assert panel.count("PanelSeparator {") == 2
    assert panel.count("ToggleSwitch {") == 1
    # The single action rides the hero's trailing edge, not its own row.
    assert "trailingControl: Component {" in panel
    assert 'contentHeight: panel.fittedContentHeight(panelColumn.implicitHeight)' in panel


def test_hero_carries_the_status_in_the_caption_idiom():
    panel = panel_source()

    hero = panel.split("PanelHero {", 1)[1].split("// ----------", 1)[0]
    assert 'title: "Omarvis"' in hero
    assert "meta: root.displayState" in hero
    # PanelHero renders `meta` bold, uppercase, letterSpacing 1.2 and
    # Qt.darker(fg, 1.4); the panel must not hand-roll a second caption.
    assert "detail:" not in hero
    for state in ("Idle", "Connecting", "Ending", "Phone", "Needs attention"):
        assert f'"{state}"' in panel


def test_remote_section_is_one_toggle_with_no_badge_or_url_text():
    panel = panel_source()
    remote = panel.split('text: "REMOTE"', 1)[1]

    # Only the bare switch is a button: a plain unclickable label beside a
    # ToggleSwitch, no row fill, no row border, no Toggle wrapper.
    assert "ToggleSwitch {" in remote
    assert 'text: "Remote access"' in remote
    assert "Toggle {" not in remote.replace("ToggleSwitch {", "")
    assert "onToggled: if (root.svc) root.svc.setRemoteEnabled(!root.remoteEnabled)" in remote
    # The switch alone carries the on-state in accent; the label text stays
    # in the plain foreground, and there is no subtitle under it.
    assert "foreground: root.foreground" in remote
    assert "checked ? root.accent" not in remote
    assert "accent: root.accent" in remote
    assert "description:" not in remote
    # The ON/OFF dot badge, the status paragraph and the QR-vs-clipboard
    # caption are all gone; the switch itself is the state.
    assert '"ON"' not in remote
    assert '"OFF"' not in remote
    assert "CopyRow" not in panel
    assert "remoteStatusText" not in panel
    # The pairing URL is never rendered as text — only as a full-width QR
    # with a labeled copy button below it.
    assert "text: root.remoteUrl" not in panel
    # Copying confirms itself: the label flips to "Copied" for a moment.
    assert 'text: copied ? "Copied" : "Copy link"' in remote
    assert "copiedTimer.restart()" in remote
    assert "root.copyText(root.remoteUrl)" in remote
    assert '["wl-copy", "--", value]' in panel
    assert remote.count("Canvas {") == 1
    assert "ctx.fillRect" in remote
    # The QR is Canvas-drawn; only the keybinding rows use a Repeater.
    assert "Repeater" not in panel.split('text: "REMOTE"', 1)[1]


def test_qr_is_full_width_and_yields_to_a_connected_phone():
    panel = panel_source()

    qr_block = panel.split("Canvas {", 1)[0].rsplit("Column {", 1)[1]
    assert "root.remoteEnabled && !root.phoneSessionActive" in qr_block
    assert "root.qrMatrix && root.qrMatrix.length > 0" in qr_block
    # The code takes the panel's full width and stays square.
    qr_slot = panel.split("id: qrSlot", 1)[1].split("Canvas {", 1)[0]
    assert "width: parent.width" in qr_slot
    assert "height: width" in qr_slot
    assert '"PHONE CONNECTED"' in panel
    assert "visible: root.phoneSessionActive" in panel
    assert 'text: "End"' in panel
    # A live phone call shows the in-talk handset in the attention color,
    # never the speaker or the mic.
    assert "0xF03F6" in panel
    assert "root.svc.endPhoneSession()" in panel


def test_remote_repair_is_one_line_and_the_rest_is_a_notification():
    panel = panel_source()

    assert "visible: root.remoteProblem" in panel
    assert "Tailscale is not connected" in panel
    assert "Tailscale operator access required" in panel
    assert "Tailscale Serve failed" in panel
    # Long repair text belongs in the notification Service.qml raises.
    assert "Open the Tailscale panel" not in panel
    assert "sudo tailscale set" not in panel


def test_starting_state_pulses_a_non_microphone_glyph():
    panel = panel_source()

    # Same grammar as the HUD and the phone: pulse means wait, and waiting
    # never borrows the microphone glyph.
    assert "0xF051F" in panel
    glyph = panel.split("readonly property string glyphText:", 1)[1].split("readonly property color", 1)[0]
    starting = glyph.index("0xF051F")
    assert starting < glyph.index("if (localSessionLive) return String.fromCodePoint(0xF036C)")
    assert 'connecting: sessionState === "starting"' in panel
    assert "duration: 950" in panel
    assert "Easing.InOutSine" in panel
    assert "opacity: root.connecting ? barPulse.value : 1.0" in panel


def test_panel_takes_all_chrome_from_the_shell_singletons():
    panel = panel_source()

    assert "import qs.Commons" in panel
    # No hand-mixed colors or pixel literals: Color/Style own both.
    assert not re.search(r'"#[0-9a-fA-F]{3,8}"', panel)
    assert "Style.space(" in panel


def test_dictation_history_is_delegated_exclusively_to_omarchy_clipboard():
    panel = panel_source()
    service = (ROOT / "Service.qml").read_text()

    assert "LAST DICTATION" not in panel
    assert "lastDictation" not in panel
    assert "lastDictation" not in service


def test_the_dead_bar_widget_entry_point_is_gone():
    assert not (ROOT / "BarWidget.qml").exists()
    assert "BarWidget" not in (ROOT / "qmldir").read_text()


def test_keybindings_section_lists_the_live_mappings():
    panel = panel_source()

    keys = panel.split('text: "KEYBINDINGS"', 1)[1].split('text: "REMOTE"', 1)[0]
    assert "model: root.svc ? root.svc.keybindings : []" in keys
    assert "modelData.label" in keys
    assert "modelData.keys" in keys
    # The section disappears entirely when nothing is bound.
    assert "visible: keybindingRows.count > 0" in panel
