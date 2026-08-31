from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


def capture_screenshot(config: Mapping[str, Any]) -> Path:
    cache_dir = Path(
        os.path.expanduser(str(config.get("cache_dir") or "~/.cache/omarvis"))
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["OMARCHY_SCREENSHOT_DIR"] = str(cache_dir)
    completed = subprocess.run(
        ["omarchy", "capture", "screenshot", "fullscreen", "save"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        env=env,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "Screenshot capture failed")
    output_lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not output_lines:
        raise RuntimeError("Screenshot capture returned no path")
    path = Path(output_lines[-1])
    try:
        path.resolve().relative_to(cache_dir.resolve())
    except ValueError as error:
        raise RuntimeError("Screenshot path escaped the Omarvis cache") from error
    if not path.is_file():
        raise RuntimeError("Screenshot capture did not create a file")
    return path


def capture_and_upload_screenshot(
    client: Any,
    conversation_id: str,
    config: Mapping[str, Any],
    *,
    capture: Callable[[Mapping[str, Any]], Path] = capture_screenshot,
) -> str:
    if not conversation_id:
        raise RuntimeError("ElevenLabs conversation ID is not ready")
    screenshot: Path | None = None
    try:
        screenshot = capture(config)
        with screenshot.open("rb") as image:
            response = client.conversational_ai.conversations.files.create(
                conversation_id=conversation_id,
                file=image,
            )
        file_id = str(getattr(response, "file_id", "")).strip()
        if not file_id:
            raise RuntimeError("ElevenLabs screenshot upload returned no file ID")
        return file_id
    finally:
        if screenshot is not None:
            try:
                screenshot.unlink(missing_ok=True)
            except OSError:
                pass
