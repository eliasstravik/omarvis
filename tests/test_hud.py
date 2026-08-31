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
    # switches the strip border to bar.active. No lock glyph composition.
    assert "handsFree" in hud
    assert "dictationLocked" in hud
    assert "Border.flat(Color.bar.active" in hud
    assert "0xF033E" not in hud


def test_tool_activity_lives_in_the_state_glyph_slot():
    hud = HUD.read_text()

    # Running a command is a state: cog while executing, a ✓ flash when
    # done, both in accent. No separate chip, so the strip stays one width.
    assert "toolRunning" in hud
    # mdi-wrench — a literal tool — is the generic running-command glyph.
    assert "0xF05B7" in hud
    assert "0xF0493" not in hud
    assert '"✓"' in hud
    # Iconic actions carry their own glyph: camera for captures, palette
    # for theme changes, power for system actions.
    assert "toolGlyphFor" in hud
    assert "0xF0100" in hud
    assert "0xF03D8" in hud
    assert "0xF0425" in hud
    glyph_expr = hud.split("stateGlyph:")[1].split("}")[0]
    assert glyph_expr.index("toolSucceeded") < glyph_expr.index("toolRunning")
    assert glyph_expr.index("0xF0026") < glyph_expr.index("toolSucceeded")


def test_call_end_gets_a_goodbye_wave_beat():
    hud = HUD.read_text()

    # A finished call lingers ~900ms with a waving hand (mdi-hand-wave)
    # instead of vanishing mid-sentence; errors don't wave.
    assert "0xF1821" in hud
    assert "waving" in hud
    assert "waving || (service" in hud
    assert 'hud.lastSessionState === "listening"' in hud
    assert '"error"' not in hud.split("onSessionStateChanged")[1].split("}")[0]


def test_hud_clears_bar_edge_like_notifications():
    hud = HUD.read_text()

    assert "shell.bar.barSize" in hud
    assert "Style.gapsOut" in hud
    assert "shell.barConfig" in hud
