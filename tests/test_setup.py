import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SHIPPED = (
    *sorted(path for path in (ROOT / "omarvis").glob("*.py") if path.name != "setupfiles.py"),
    *sorted(ROOT.glob("*.qml")),
    *sorted(path for path in (ROOT / "bin").iterdir() if path.name != "omarvis-setup"),
    ROOT / "assets" / "web" / "index.html",
    ROOT / "manifest.json",
    ROOT / "qmldir",
    ROOT / "agent" / "prompt.md",
)
# Everything Ask mode and the typed one-shot left behind. bin/omarvis-setup and
# omarvis/setupfiles.py are excluded above because they name these on purpose,
# to delete stale bindings and config keys.
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
    from omarvis import setupfiles

    assert setupfiles.DICTATE_UNBIND == 'hl.unbind("SUPER + J")'
    # Any install without the submap gets the whole dictation unit rewritten:
    # the pre-submap pair is deleted, then unbind + pair + submaps appended.
    plan = setupfiles.plan_bindings(
        'o.bind("SUPER + J", "Omarvis Dictate", "omarchy-shell omarvis dictate start")\n'
        'o.bind("SUPER + J", "Omarvis Dictate Stop", "omarchy-shell omarvis dictate stop", { release = true })\n'
    )
    assert plan.rebind_dictation
    start = plan.missing.index(setupfiles.DICTATE_UNBIND)
    assert plan.missing[start : start + len(setupfiles.DICTATION_UNIT)] == setupfiles.DICTATION_UNIT
    updated = setupfiles.apply_plan("keep me\n" + 'hl.unbind("SUPER + J")\n', plan)
    assert updated.startswith("keep me\n")
    assert updated.count('hl.unbind("SUPER + J")') == 1
    assert updated.index('hl.unbind("SUPER + J")') < updated.index(setupfiles.DICTATE_START_BINDING)



def test_setup_removes_retired_ask_and_text_bindings():
    from omarvis import setupfiles

    script = (Path(__file__).parent.parent / "bin" / "omarvis-setup").read_text()
    # Ask mode and the typed one-shot no longer exist. Setup must not offer
    # them, and must delete whatever an older install left behind.
    assert "ASK_BINDING" not in script and "TEXT_BINDING" not in script
    assert not hasattr(setupfiles, "ASK_BINDING")
    stale = (
        'o.bind("SUPER + SHIFT + J", "Omarvis Ask", "omarchy-shell omarvis toggle ask")\n'
        'o.bind("SUPER + ALT + J", "Omarvis Text", "~/.config/omarchy/plugins/x/bin/omarvis-text")\n'
    )
    plan = setupfiles.plan_bindings(stale)
    assert plan.retired
    assert "omarvis-text" not in setupfiles.apply_plan(stale, plan)
    assert 'toggle ask"' not in setupfiles.apply_plan(stale, plan)
    merged = setupfiles.merged_config(
        {"ask_agent_id": "old", "vision": {"x": 1}, "agent_id": "a"},
        browser_mode="unavailable",
        agent_browser_path="",
        browser_executable_path="",
    )
    assert "ask_agent_id" not in merged and "vision" not in merged



def test_setup_offers_only_the_surviving_bindings():
    from omarvis import setupfiles

    # SUPER+SHIFT+J was freed by the Ask removal and now toggles remote
    # access; SUPER+ALT+J (freed by the typed one-shot's removal) opens the
    # panel — SUPER+ALT is Omarchy's most common two-modifier tier, and the
    # stock config never stacks CTRL+ALT.
    assert setupfiles.REMOTE_BINDING == (
        'o.bind("SUPER + SHIFT + J", "Omarvis Remote", "omarchy-shell omarvis toggleRemote")'
    )
    assert setupfiles.PANEL_BINDING == (
        'o.bind("SUPER + ALT + J", "Omarvis Panel", "omarchy-shell omarvis panel")'
    )
    assert all("SUPER + CTRL + ALT + J" not in binding for binding in setupfiles.ALL_BINDINGS)
    assert setupfiles.plan_bindings("\n".join(setupfiles.ALL_BINDINGS)).changes is False



def test_setup_removes_legacy_external_vision_configuration():
    script = (Path(__file__).parent.parent / "bin" / "omarvis-setup").read_text()
    module = (Path(__file__).parent.parent / "omarvis" / "setupfiles.py").read_text()

    assert '"vision"' in module
    assert "anthropic" not in (script + module).lower()
    assert "vision_api_key" not in script + module



def test_setup_defaults_the_agent_to_the_omarvis_voice():
    from omarvis.setupfiles import merged_config

    merged = merged_config({}, browser_mode="unavailable", agent_browser_path="", browser_executable_path="")
    assert merged["voice_id"] == "JSWO6cw2AyFE324d5kEr"



def test_setup_pins_metered_audio_sdk_and_preserves_ui_defaults():
    from omarvis.setupfiles import merged_config

    # The SDK pin moved into the hash lock's top-level input.
    assert "elevenlabs==2.65.0" in (ROOT / "requirements.in").read_text()
    # Sounds are gone for good: no earcons default, and setup prunes the
    # stale key from existing configs while keeping the user's other values.
    merged = merged_config(
        {"ui": {"earcons": True, "hud_position": "bottom-center"}, "herdr_announcements": False},
        browser_mode="unavailable",
        agent_browser_path="",
        browser_executable_path="",
    )
    assert merged["ui"] == {"hud_position": "bottom-center"}
    assert merged["herdr_announcements"] is False
    assert merged_config({}, browser_mode="x", agent_browser_path="", browser_executable_path="")["ui"] == {
        "hud_position": "top-center"
    }



def test_setup_installs_qrcode_without_pillow_and_sets_web_port():
    from omarvis.setupfiles import merged_config

    assert "qrcode==8.2" in (ROOT / "requirements.in").read_text()
    assert "pillow" not in (ROOT / "requirements.lock").read_text().lower()
    merged = merged_config({}, browser_mode="x", agent_browser_path="", browser_executable_path="")
    assert merged["web_port"] == 4763


def test_setup_requires_wayland_clipboard_support():
    script = (Path(__file__).parent.parent / "bin" / "omarvis-setup").read_text()

    assert "need_command wl-copy" in script



def test_setup_installs_native_panel_keybinding():
    from omarvis import setupfiles

    assert "SUPER + ALT + J" in setupfiles.PANEL_BINDING
    assert "omarchy-shell omarvis panel" in setupfiles.PANEL_BINDING
    plan = setupfiles.plan_bindings("")
    assert setupfiles.PANEL_BINDING in plan.missing



def test_setup_teaches_key_creation_and_validates_the_needed_scope():
    from omarvis import setupfiles

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
    assert setupfiles.KEY_CHECK_URL.startswith("https://api.elevenlabs.io/v1/convai/agents")
    assert "missing_permissions" in (ROOT / "omarvis" / "setupfiles.py").read_text()
    # The key reaches the helper on stdin and leaves it as a request header:
    # no curl, no key in any argv, no shell variable interpolated into a
    # command line.
    assert "curl" not in script
    assert "xi-api-key" not in script
    assert "printf '%s' \"$1\" | setupfiles check-key" in script
    assert "printf '%s' \"$1\" | setupfiles store-key" in script


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
    # commit, so every artifact setup fetches must be exactly pinned and
    # digest-verified by files in this repository, and no package-manager
    # lifecycle script or unpinned installer may run.
    from omarvis import setupfiles

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

    # agent-browser: the native binary is taken out of the pinned npm
    # tarball by the setup helper. npm itself never runs (so neither does
    # the package's postinstall, which would fetch an unverified binary from
    # a GitHub release URL), no managed browser is ever downloaded, and both
    # the tarball and the extracted binary are verified against digests in
    # the repository.
    assert "npm ci" not in script and "npm install" not in script and "need_command npm" not in script
    assert "agent-browser install" not in script
    assert "setupfiles fetch-agent-browser" in script
    assert "setupfiles fetch-elevenlabs-client" in script
    assert not (ROOT / "npm").exists()
    assert setupfiles.AGENT_BROWSER_VERSION == "0.34.0"
    assert setupfiles.AGENT_BROWSER_URL == (
        "https://registry.npmjs.org/agent-browser/-/agent-browser-0.34.0.tgz"
    )
    assert len(setupfiles.AGENT_BROWSER_TARBALL_SHA256) == 64
    import base64

    assert len(base64.b64decode(setupfiles.AGENT_BROWSER_TARBALL_SHA512_B64)) == 64
    for arch in ("x86_64", "aarch64"):
        artifact = setupfiles.agent_browser_artifact(arch)
        assert artifact.member.startswith("package/bin/agent-browser-linux-")
        assert len(artifact.member_sha256) == 64
        assert artifact.mode == 0o755
    with pytest.raises(setupfiles.SetupError):
        setupfiles.agent_browser_artifact("mips")
    # Chromium, when missing, comes from the distribution's signed repository.
    assert "omarchy pkg add chromium" in script


