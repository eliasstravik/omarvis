from pathlib import Path


HUD = Path(__file__).parent.parent / "HudWindow.qml"


def test_hud_binds_levels_and_hides_when_both_services_are_idle():
    hud = HUD.read_text()

    assert "service.inLevel" in hud
    assert "service.outLevel" in hud
    assert "service.dictationLevel" in hud
    assert 'service.sessionState !== "idle" || service.dictationState !== "idle"' in hud
    assert "visible: hudVisible" in hud


def test_hud_has_speaking_silence_fallback_and_visibility_gated_animations():
    hud = HUD.read_text()

    assert "interval: 1500" in hud
    assert 'service.sessionState === "speaking"' in hud
    assert "speakingFallback = true" in hud
    assert 'running: hud.visible && hud.visualState === "thinking"' in hud
    assert "running: hud.visible && !hud.toolSucceeded && toolChip.visible" in hud


def test_hud_is_passive_overlay_with_configurable_position_and_error_style():
    hud = HUD.read_text()

    assert "WlrLayershell.layer: WlrLayer.Overlay" in hud
    assert "WlrLayershell.keyboardFocus: WlrKeyboardFocus.None" in hud
    assert "exclusionMode: ExclusionMode.Ignore" in hud
    assert "hud.hudPosition" in hud
    assert "hud.urgentColor" in hud
