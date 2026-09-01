import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
SHIPPED = (
    *sorted((ROOT / "omarvis").glob("*.py")),
    *sorted(ROOT.glob("*.qml")),
    *sorted(path for path in (ROOT / "bin").iterdir() if path.name != "omarvis-setup"),
    ROOT / "assets" / "web" / "index.html",
    ROOT / "manifest.json",
    ROOT / "qmldir",
    ROOT / "agent" / "prompt.md",
)
# Everything Ask mode and the typed one-shot left behind. bin/omarvis-setup is
# excluded above because it names these on purpose, to delete stale bindings.
RETIRED = (
    "text_only",
    "--text-only",
    "ask_agent_id",
    "prompt-ask",
    "toggleMode",
    "startMode",
    "currentMode",
    "ASK_REFUSAL",
    "bin/omarvis-text",
    "Start Ask",
    "Omarvis Ask",
)


def test_nothing_the_plugin_ships_still_mentions_the_retired_modes():
    offenders = [
        f"{path.relative_to(ROOT)}: {token}"
        for path in SHIPPED
        for token in RETIRED
        if token in path.read_text()
    ]

    assert offenders == []


def test_the_retired_entry_points_are_deleted():
    assert not (ROOT / "bin" / "omarvis-text").exists()
    assert not (ROOT / "agent" / "prompt-ask.md").exists()
    assert not (ROOT / "BarWidget.qml").exists()


def test_setup_unbinds_super_j_before_installing_press_and_release_pair():
    script = (Path(__file__).parent.parent / "bin" / "omarvis-setup").read_text()

    assert "DICTATE_UNBIND='hl.unbind(\"SUPER + J\")'" in script
    migration = script.index('missing_bindings+=("$DICTATE_UNBIND"')
    assert migration < script.index("printf '%s\\n' \"${missing_bindings[@]}\"")
    assert "needs_dictation_rebind=true" in script
    assert "sed -i" in script


def test_setup_removes_retired_ask_and_text_bindings():
    script = (Path(__file__).parent.parent / "bin" / "omarvis-setup").read_text()

    # Ask mode and the typed one-shot no longer exist. Setup must not offer
    # them, and must delete whatever an older install left behind.
    assert "ASK_BINDING" not in script
    assert "TEXT_BINDING" not in script
    assert "has_retired_bindings=true" in script
    assert (
        r"""sed -i -E '/omarvis (toggle|toggleMode) ask"|\/bin\/omarvis-text"/d'"""
        in script
    )
    assert "del(.ask_agent_id)" in script
    assert "ask_agent_id: (" not in script


def test_setup_offers_only_the_surviving_bindings():
    script = (Path(__file__).parent.parent / "bin" / "omarvis-setup").read_text()

    for binding in (
        "AGENT_BINDING",
        "DICTATE_START_BINDING",
        "DICTATE_STOP_BINDING",
        "PANEL_BINDING",
    ):
        assert f"{binding}=" in script
    # SUPER+SHIFT+J was freed by the Ask removal and now toggles remote
    # access; SUPER+ALT+J (freed by the typed one-shot's removal) opens the
    # panel — SUPER+ALT is Omarchy's most common two-modifier tier, and the
    # stock config never stacks CTRL+ALT.
    assert 'o.bind("SUPER + SHIFT + J", "Omarvis Remote", "omarchy-shell omarvis toggleRemote")' in script
    assert 'o.bind("SUPER + ALT + J", "Omarvis Panel", "omarchy-shell omarvis panel")' in script
    assert "SUPER + CTRL + ALT + J" not in script


def test_setup_removes_legacy_external_vision_configuration():
    script = (Path(__file__).parent.parent / "bin" / "omarvis-setup").read_text()

    assert "del(.vision)" in script
    assert "anthropic" not in script.lower()
    assert "vision_api_key" not in script


def test_setup_defaults_the_agent_to_the_omarvis_voice():
    script = (Path(__file__).parent.parent / "bin" / "omarvis-setup").read_text()

    assert 'voice_id: (.voice_id // "JSWO6cw2AyFE324d5kEr")' in script


def test_setup_pins_metered_audio_sdk_and_preserves_ui_defaults():
    script = (Path(__file__).parent.parent / "bin" / "omarvis-setup").read_text()

    # The SDK pin moved into the hash lock's top-level input.
    assert "elevenlabs==2.65.0" in (ROOT / "requirements.in").read_text()
    # Sounds are gone for good: no earcons default, and setup prunes the
    # stale key from existing configs.
    assert "del(.earcons)" in script
    assert "earcons: (" not in script
    assert 'hud_position: (.ui.hud_position // "top-center")' in script


def test_setup_installs_qrcode_without_pillow_and_sets_web_port():
    script = (Path(__file__).parent.parent / "bin" / "omarvis-setup").read_text()

    assert "qrcode==8.2" in (ROOT / "requirements.in").read_text()
    assert "pillow" not in (ROOT / "requirements.lock").read_text().lower()
    assert "web_port: (.web_port // 4763)" in script


def test_setup_requires_wayland_clipboard_support():
    script = (Path(__file__).parent.parent / "bin" / "omarvis-setup").read_text()

    assert "need_command wl-copy" in script


def test_setup_installs_native_panel_keybinding():
    script = (Path(__file__).parent.parent / "bin" / "omarvis-setup").read_text()

    assert 'PANEL_BINDING=' in script
    assert 'SUPER + ALT + J' in script
    assert 'omarchy-shell omarvis panel' in script
    assert 'missing_bindings+=("$PANEL_BINDING")' in script


def test_setup_teaches_key_creation_and_validates_the_needed_scope():
    script = (Path(__file__).parent.parent / "bin" / "omarvis-setup").read_text()

    # The key prompt must carry its own instructions: where to create the
    # key, which product scopes it needs, and that ElevenLabs shows it once.
    assert "https://elevenlabs.io" in script
    assert "API Keys" in script
    assert "Agents Platform / Conversational AI" in script
    assert "Speech to Text" in script
    assert "ElevenLabs shows it only once" in script
    # Validation exercises the exact scope provisioning needs (listing
    # agents), so an under-scoped key fails at the prompt, not phases later.
    assert "api.elevenlabs.io/v1/convai/agents" in script
    assert "xi-api-key" in script
    assert "missing_permissions" in script
    assert "need_command curl" in script


def test_setup_asks_everything_up_front_then_runs_unattended():
    script = (Path(__file__).parent.parent / "bin" / "omarvis-setup").read_text()

    # Every prompt precedes the first work phase, so the run never stalls
    # waiting for input once the installing starts.
    first_phase = script.index('phase 1 "')
    for prompt_marker in (
        'confirm "Dictation needs wtype.',
        'read -r -s -p "ElevenLabs API key',
        'confirm "Update the missing or outdated bindings',
    ):
        assert script.index(prompt_marker) < first_phase


def test_setup_keeps_subprocess_noise_in_a_log():
    script = (Path(__file__).parent.parent / "bin" / "omarvis-setup").read_text()

    assert "setup.log" in script
    assert 'pip" install -q' in script
    # Failures must surface the hidden output, never swallow it.
    assert "tail -n 20" in script
    # Non-interactive parity and terminal etiquette.
    assert "--yes" in script
    assert "NO_COLOR" in script


def test_setup_explains_remote_prerequisite_and_credential_risk():
    script = (Path(__file__).parent.parent / "bin" / "omarvis-setup").read_text()

    assert "Tailscale Serve only (never Funnel)" in script
    assert "phone must be on your tailnet" in script
    assert "whoever holds either can install software and run terminal commands" in script
    assert "copied URLs remain in clipboard history" in script


def test_reference_discloses_full_remote_access_and_secret_lifetime():
    # The complete security model lives in the reference; the README carries
    # only the headlines (asserted separately below).
    reference = (
        Path(__file__).parent.parent / "docs" / "reference.md"
    ).read_text().lower()

    assert "whoever holds this url, or photographs the qr" in reference
    assert "can install software and run terminal commands" in reference
    assert "never through funnel or the public internet" in reference
    assert "processes running on any of them" in reference
    assert "may sync to a cloud account and other devices" in reference
    assert "persistent, browsable clipboard history" in reference
    assert "delete that file while remote access is off" in reference
    assert "persists across reboots and re-arms at login" in reference
    assert "real-profile" in reference
    assert "uploads a current desktop screenshot to elevenlabs" in reference
    assert "phone and elevenlabs over webrtc" in reference


def test_readme_headlines_remote_risk_and_data_flows():
    readme = (Path(__file__).parent.parent / "README.md").read_text().lower()

    # A reader who never opens the reference must still learn the remote URL
    # is a credential, what the browser runs as, and what leaves the machine.
    assert "anyone holding that url can drive your machine" in readme
    assert "treat the qr like a credential" in readme
    assert "tailscale serve only, never funnel" in readme
    assert "snapshot copy of your real profile" in readme
    assert "nothing is written back" in readme
    assert "screenshot is uploaded only when you explicitly ask" in readme
    assert "nothing is sent while idle" in readme


def test_setup_installs_only_locked_verified_artifacts():
    # The marketplace review binds runtime authorization to the reviewed
    # commit, so every registry artifact setup fetches must be exactly
    # pinned and hash-verified by files in this repository.
    script = (ROOT / "bin" / "omarvis-setup").read_text(encoding="utf-8")

    # Python: the venv is built from the full transitive lock, and pip
    # refuses any artifact whose sha256 is not recorded there.
    assert "--require-hashes" in script
    assert 'requirements.lock"' in script
    lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    pins = [
        line for line in lock.splitlines()
        if "==" in line and not line.startswith(("#", " ", "\t"))
    ]
    assert any(line.startswith("elevenlabs==2.65.0") for line in pins)
    assert any(line.startswith("qrcode==8.2") for line in pins)
    assert lock.count("--hash=sha256:") >= len(pins)

    # npm: agent-browser comes from the committed integrity lock via npm ci
    # into the plugin's own state dir — never a mutable global install.
    assert "npm install -g" not in script
    assert "npm ci" in script
    pkg = json.loads((ROOT / "npm" / "package.json").read_text(encoding="utf-8"))
    npm_lock = json.loads((ROOT / "npm" / "package-lock.json").read_text(encoding="utf-8"))
    assert pkg["dependencies"]["agent-browser"] == "0.34.0"
    locked = npm_lock["packages"]["node_modules/agent-browser"]
    assert locked["version"] == "0.34.0"
    assert locked["integrity"].startswith("sha512-")
