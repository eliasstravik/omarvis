from __future__ import annotations

import base64
import json
import os
import subprocess
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def vision_configuration_error(config: Mapping[str, Any]) -> str | None:
    if not config.get("enabled", False):
        return "Vision is not configured. Enable the vision block in config.json."
    if str(config.get("provider") or "") != "anthropic":
        return "Vision provider is unsupported; configure provider as anthropic."
    if not str(config.get("model") or "").strip():
        return "Vision is not configured: set vision.model in config.json."
    key_path = Path(
        os.path.expanduser(str(config.get("api_key_path") or ""))
    )
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip() and not key_path.is_file():
        return "Vision is not configured: add the Anthropic API key at vision.api_key_path."
    return None


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


def anthropic_description(image_path: Path, config: Mapping[str, Any]) -> str:
    key_path = Path(
        os.path.expanduser(str(config.get("api_key_path") or ""))
    )
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        api_key = key_path.read_text().strip()
    payload = {
        "model": str(config["model"]),
        "max_tokens": int(config.get("max_tokens", 500)),
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64.b64encode(image_path.read_bytes()).decode(
                                "ascii"
                            ),
                        },
                    },
                    {
                        "type": "text",
                        "text": str(
                            config.get("prompt")
                            or "Describe this desktop screenshot concisely, including visible apps, layout, text, and anything relevant to the user's question."
                        ),
                    },
                ],
            }
        ],
    }
    request = urllib.request.Request(
        str(config.get("endpoint") or "https://api.anthropic.com/v1/messages"),
        data=json.dumps(payload).encode(),
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read())
    content = result.get("content", []) if isinstance(result, Mapping) else []
    text = "\n".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "text"
    ).strip()
    if not text:
        raise RuntimeError("Vision provider returned no text description")
    return text
