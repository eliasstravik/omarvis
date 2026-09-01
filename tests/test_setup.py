from pathlib import Path


def test_setup_unbinds_super_j_before_installing_press_and_release_pair():
    script = (Path(__file__).parent.parent / "bin" / "omarvis-setup").read_text()

    assert "DICTATE_UNBIND='hl.unbind(\"SUPER + J\")'" in script
    migration = script.index('missing_bindings+=("$DICTATE_UNBIND"')
    assert migration < script.index("printf '%s\\n' \"${missing_bindings[@]}\"")
    assert "needs_dictation_rebind=true" in script
    assert "sed -i" in script


def test_setup_migrates_legacy_ask_ipc_binding():
    script = (Path(__file__).parent.parent / "bin" / "omarvis-setup").read_text()

    assert 'ASK_LEGACY_BINDING=' in script
    assert 'omarchy-shell omarvis toggleMode ask' in script
    assert 'needs_ask_rebind=true' in script
    assert 'sed -i "\\|$ASK_LEGACY_BINDING|d"' in script


def test_setup_removes_legacy_external_vision_configuration():
    script = (Path(__file__).parent.parent / "bin" / "omarvis-setup").read_text()

    assert "del(.vision)" in script
    assert "anthropic" not in script.lower()
    assert "vision_api_key" not in script


def test_setup_defaults_both_agents_to_the_omarvis_voice():
    script = (Path(__file__).parent.parent / "bin" / "omarvis-setup").read_text()

    assert 'voice_id: (.voice_id // "JSWO6cw2AyFE324d5kEr")' in script


def test_setup_pins_metered_audio_sdk_and_preserves_ui_defaults():
    script = (Path(__file__).parent.parent / "bin" / "omarvis-setup").read_text()

    assert '"elevenlabs==2.65.0"' in script
    assert "earcons: (.ui.earcons // true)" in script
    assert 'hud_position: (.ui.hud_position // "top-center")' in script


def test_setup_installs_qrcode_without_pillow_and_sets_web_port():
    script = (Path(__file__).parent.parent / "bin" / "omarvis-setup").read_text()

    assert '"qrcode==8.2"' in script
    assert "Pillow" not in script
    assert "web_port: (.web_port // 4763)" in script


def test_setup_requires_wayland_clipboard_support():
    script = (Path(__file__).parent.parent / "bin" / "omarvis-setup").read_text()

    assert "need_command wl-copy" in script


def test_setup_installs_native_panel_keybinding():
    script = (Path(__file__).parent.parent / "bin" / "omarvis-setup").read_text()

    assert 'PANEL_BINDING=' in script
    assert 'SUPER + CTRL + ALT + J' in script
    assert 'omarchy-shell omarvis panel' in script
    assert 'missing_bindings+=("$PANEL_BINDING")' in script


def test_setup_explains_remote_prerequisite_and_credential_risk():
    script = (Path(__file__).parent.parent / "bin" / "omarvis-setup").read_text()

    assert "Tailscale Serve only (never Funnel)" in script
    assert "phone must be on your tailnet" in script
    assert "whoever holds either can install software and run terminal commands" in script
    assert "copied URLs remain in clipboard history" in script


def test_readme_discloses_full_remote_access_and_secret_lifetime():
    readme = (Path(__file__).parent.parent / "README.md").read_text().lower()

    assert "whoever holds this url, or photographs the qr" in readme
    assert "can install software and run terminal commands" in readme
    assert "never through funnel or the public internet" in readme
    assert "processes running on any of them" in readme
    assert "may sync to a cloud account and other devices" in readme
    assert "persistent, browsable clipboard history" in readme
    assert "delete that file while remote access is off" in readme
    assert "persists across reboots and re-arms at login" in readme
    assert "real-profile" in readme
    assert "uploads a current desktop screenshot to elevenlabs" in readme
    assert "phone and elevenlabs over webrtc" in readme
