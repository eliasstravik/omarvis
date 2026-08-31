import subprocess
from pathlib import Path

from omarvis import sounds


def test_play_is_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_options: (_ for _ in ()).throw(AssertionError("called")))

    sounds.play("mic-open", enabled=False)


def test_play_builds_pw_play_command(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **options: calls.append((argv, options)))

    sounds.play("mic-close", enabled=True)

    assert calls[0][0] == ["pw-play", str(sounds.ASSET_DIR / "mic-close.wav")]
    assert calls[0][1]["stdout"] is subprocess.DEVNULL
    assert calls[0][1]["stderr"] is subprocess.DEVNULL


def test_missing_player_or_file_does_not_raise(monkeypatch, tmp_path):
    monkeypatch.setattr(sounds, "ASSET_DIR", tmp_path)
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_options: (_ for _ in ()).throw(FileNotFoundError()))

    sounds.play("error", enabled=True)


def test_generated_sound_assets_are_small_and_present():
    files = [sounds.ASSET_DIR / name for name in sounds.SOUND_FILES.values()]

    assert all(path.is_file() for path in files)
    assert sum(path.stat().st_size for path in files) < 100_000
    assert (Path(__file__).parent.parent / "bin" / "omarvis-make-sounds").is_file()
