from pathlib import Path


HUD = Path(__file__).parent.parent / "HudWindow.qml"


def test_hud_binds_levels_and_hides_when_idle_or_errored():
    hud = HUD.read_text()

    assert "service.inLevel" in hud
    assert "service.outLevel" in hud
    assert "service.dictationLevel" in hud
    # Idle hides the strip; so does session error — errors live in a
    # desktop notification instead of on the HUD.
    assert 'service.sessionState !== "idle" && service.sessionState !== "error"' in hud
    assert 'service.dictationState !== "idle"' in hud
    assert "visible: hudVisible" in hud


def test_hud_has_speaking_silence_fallback_and_visibility_gated_animations():
    hud = HUD.read_text()

    assert "interval: 1500" in hud
    assert 'service.sessionState === "speaking"' in hud
    assert "speakingFallback = true" in hud
    # The level meter animates only while the HUD is on screen, with the
    # OSD volume bar's 140ms OutCubic width behavior.
    assert "enabled: hud.visible" in hud
    assert "duration: 140" in hud


def test_hud_is_passive_overlay_with_configurable_position_and_error_style():
    hud = HUD.read_text()

    assert "WlrLayershell.layer: WlrLayer.Overlay" in hud
    assert "WlrLayershell.keyboardFocus: WlrKeyboardFocus.None" in hud
    assert "exclusionMode: ExclusionMode.Ignore" in hud
    assert "hud.hudPosition" in hud
    assert "Color.urgent" in hud


def test_hud_uses_shell_theme_tokens_not_hardcoded_chrome():
    hud = HUD.read_text()

    # Native chrome comes from the shell singletons: Hyprland-mirrored corner
    # radius, popups surface tokens, and the shared border spec factory.
    assert "import qs.Commons" in hud
    assert "radius: Style.cornerRadius" in hud
    assert 'Border.surfaceSpec("popups", "border", Color.popups.border' in hud
    assert "Util.alpha(Color.popups.background, 0.97)" in hud
    assert "font.family: Style.font.family" in hud
    # No hand-mixed hex colors left anywhere in the HUD.
    assert "#" not in hud


def test_hud_meter_uses_decibel_scale_not_raw_rms():
    hud = HUD.read_text()

    # Raw speech RMS is a few percent of full scale; the meter must map
    # through dBFS or it reads as permanently empty.
    assert "meterFill" in hud
    assert "20 * Math.log(level) / Math.LN10" in hud
    # -60dB floor, -20dB ceiling: normal speech sits in the upper half.
    assert "(db + 60) / 40" in hud


def test_hud_is_completely_text_free():
    hud = HUD.read_text()

    # No conversation text, no command text, nothing that could truncate —
    # the strip is glyph + amplitude bar only.
    assert "displayText" not in hud
    assert "lastAgent" not in hud
    assert "streamingAgent" not in hud
    assert "lastUser" not in hud
    assert "lastError" not in hud
    assert "stateLabel" not in hud
    assert "toolText" not in hud
    assert "elide" not in hud.lower()
    assert "omarvisToolChip" not in hud


def test_hands_free_is_signaled_by_the_attention_border():
    hud = HUD.read_text()

    # Border carries mode, like the lock screen's border-active: hands-free
    # gets a full accent frame, while ephemeral hold-to-talk reuses
    # Omarchy's native unfocused-window border (read live from Hyprland's
    # general:col.inactive_border by the service) so the two dictation
    # modes never look alike. No lock glyph composition.
    assert "handsFree" in hud
    assert "dictationLocked" in hud
    assert "Border.flat(Color.accent" in hud
    assert "hud.service.inactiveBorderColor" in hud
    service = (Path(__file__).parents[1] / "Service.qml").read_text()
    assert '"general:col.inactive_border"' in service
    assert "applyInactiveBorderJson" in service
    assert "0xF033E" not in hud


def test_glyph_vocabulary_is_four_glyphs_and_never_churns_mid_call():
    hud = HUD.read_text()

    # Exactly four glyphs plus the failure alert: hourglass (waiting),
    # microphone (your floor), speaker (agent's voice), wave (goodbye).
    # Tool runs, ✓ flashes and a thinking glyph are deliberately gone —
    # swapping pictures while you talk reads as glitchy.
    for kept in ("0xF051F", "0xF036C", "0xF057E", "0xF0026"):
        assert kept in hud
    for retired in ("toolRunning", "toolSucceeded", "toolGlyphFor", '"✓"',
                    "0xF05B7", "0xF0100", "0xF03D8", "0xF0425", "0xF06D7",
                    "0xF01D8", "0xF0772"):
        assert retired not in hud
    # Thinking has no glyph of its own: mid-call it stays on the mic.
    glyph_expr = hud.split("stateGlyph:")[1].split("\n  }")[0]
    assert "thinking" not in glyph_expr


def test_call_end_disappears_with_no_goodbye_beat():
    hud = HUD.read_text()

    # A finished call simply vanishes: no wave glyph, no lingering timer.
    assert "0xF1821" not in hud
    assert "waving" not in hud
    assert "waveTimer" not in hud
    assert "lastSessionState" not in hud


def test_starting_gets_its_own_glyph_and_never_the_microphone():
    hud = HUD.read_text()

    # Up to 15s of websocket handshake used to show the same mic glyph at 45%
    # opacity, which reads as "speak now". Waiting is its own state: the
    # hourglass in the ordinary popups text color, and the mic glyph is
    # reserved for the moment the session is actually live.
    assert 'waitingToConnect: visualState === "starting"' in hud
    assert "0xF051F" in hud
    assert "opacity: hud.visualState === \"starting\" ? 0.45 : 1.0" not in hud
    glyph_expr = hud.split("stateGlyph:")[1].split("\n  }")[0]
    assert glyph_expr.index("0xF051F") < glyph_expr.index("0xF036C")


def test_waiting_pulses_and_sweeps_instead_of_showing_a_dead_meter():
    hud = HUD.read_text()

    # The shell's 950ms InOutSine wait pulse on the glyph, and an
    # indeterminate sweep in the meter — never a zero-length amplitude fill,
    # which would read as a dead microphone.
    pulse = hud.split("objectName: \"omarvisStateGlyph\"", 1)[1].split("Rectangle {", 1)[0]
    assert "SequentialAnimation on opacity" in pulse
    assert "running: hud.waitingToConnect && hud.visible" in pulse
    assert "duration: 950" in pulse
    assert "Easing.InOutSine" in pulse
    assert "loops: Animation.Infinite" in pulse

    assert 'objectName: "omarvisMeterSweep"' in hud
    assert "visible: !hud.waitingToConnect" in hud
    assert "visible: hud.waitingToConnect" in hud
    sweep = hud.split('objectName: "omarvisMeterSweep"', 1)[1]
    assert "color: Color.accent" in sweep
    assert "SequentialAnimation on x" in sweep
    assert "meter.width - meterSweep.width" in sweep


def test_listening_returns_to_the_solid_microphone_and_the_live_meter():
    hud = HUD.read_text()

    # Pulse stops, the mic goes solid — attention color for the agent call's
    # hot microphone, theme accent for dictation — and the amplitude fill
    # takes the meter back.
    assert 'visualState === "speaking" || visualState === "recording"' in hud
    assert "Color.bar.active" in hud
    assert 'objectName: "omarvisLevelFill"' in hud
    fill = hud.split('objectName: "omarvisLevelFill"', 1)[1]
    assert "width: parent.width * hud.meterLevel" in fill


def test_hud_clears_bar_edge_like_notifications():
    hud = HUD.read_text()

    assert "shell.bar.barSize" in hud
    assert "Style.gapsOut" in hud
    assert "shell.barConfig" in hud
