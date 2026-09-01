#!/usr/bin/env python3
"""Regenerate the phone page's woff2 faces from the system JetBrainsMono NF.

The phone is not an Omarchy machine, so the Nerd Font the state glyphs live
in travels with the page — but only as a subset: printable ASCII plus the
page's exact glyph vocabulary. Re-run this whenever index.html gains a new
codepoint. Needs fontTools + brotli (any venv):

    python assets/web/fonts/make-subsets.py
"""
from pathlib import Path

from fontTools.subset import Options, Subsetter
from fontTools.ttLib import TTFont

SOURCE_DIR = Path("/usr/share/fonts/TTF")
OUT_DIR = Path(__file__).resolve().parent

# Keep in lockstep with the GLYPH map in assets/web/index.html (and the
# desktop vocabulary in HudWindow.qml / Panel.qml it mirrors): hourglass,
# mic, speaker — plus alert for failure and stop for the End button.
GLYPHS = [
    0xF0026,  # alert — failure
    0xF036C,  # microphone — the floor is yours
    0xF04DB,  # stop — the End button
    0xF051F,  # hourglass — any waiting or busywork
    0xF057E,  # speaker — the agent's voice
]

FACES = {
    "JetBrainsMonoNerdFont-Regular.ttf": "jetbrains-mono-nf-regular.woff2",
    "JetBrainsMonoNerdFont-Bold.ttf": "jetbrains-mono-nf-bold.woff2",
}


def main() -> None:
    unicodes = list(range(0x20, 0x7F)) + GLYPHS
    for source, target in FACES.items():
        options = Options(hinting=False, desubroutinize=True)
        font = TTFont(SOURCE_DIR / source)
        subsetter = Subsetter(options)
        subsetter.populate(unicodes=unicodes)
        subsetter.subset(font)
        font.flavor = "woff2"
        out = OUT_DIR / target
        font.save(out)
        print(f"{target}: {out.stat().st_size} bytes")


if __name__ == "__main__":
    main()
