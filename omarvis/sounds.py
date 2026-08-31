from __future__ import annotations

import subprocess
from pathlib import Path


ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "sounds"
SOUND_FILES = {
    "mic-open": "mic-open.wav",
    "mic-close": "mic-close.wav",
    "error": "error.wav",
}


def play(name: str, *, enabled: bool) -> None:
    """Play an Omarvis earcon without affecting the caller on failure."""
    if not enabled or name not in SOUND_FILES:
        return
    try:
        subprocess.Popen(
            ["pw-play", str(ASSET_DIR / SOUND_FILES[name])],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        pass
