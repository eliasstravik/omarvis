from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .privatefiles import PrivateFileError, open_private_file, private_dir
from .process import execute_process

MAX_SCREENSHOT_BYTES = 32 * 1024 * 1024


def capture_screenshot(config: Mapping[str, Any]) -> Path:
    cache_dir = Path(
        os.path.expanduser(str(config.get("cache_dir") or "~/.cache/omarvis"))
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    completed = execute_process(
        ["omarchy", "capture", "screenshot", "fullscreen", "save"],
        timeout=15.0,
        kill_on_timeout=True,
        stdout_limit=4000,
        extra_env={"OMARCHY_SCREENSHOT_DIR": str(cache_dir)},
    )
    if completed.timed_out:
        raise RuntimeError("Screenshot capture timed out")
    if completed.exit_code != 0:
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
        # The cache lives at a predictable pathname, so the file is opened
        # through its directory descriptor without following links and
        # validated by fstat before a byte of it is uploaded.
        with private_dir(screenshot.parent) as dir_fd:
            with open_private_file(
                dir_fd, screenshot.name, limit=MAX_SCREENSHOT_BYTES, private=False
            ) as image:
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
                with private_dir(screenshot.parent, create=False) as dir_fd:
                    os.unlink(screenshot.name, dir_fd=dir_fd)
            except (OSError, PrivateFileError):
                pass
