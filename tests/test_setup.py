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
