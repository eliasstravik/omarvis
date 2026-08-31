from types import SimpleNamespace

import pytest

from omarvis.screenshot import capture_and_upload_screenshot


def test_native_screenshot_upload_returns_file_id_and_deletes_local_file(tmp_path):
    screenshot = tmp_path / "screenshot.png"
    calls = []

    def capture(config):
        calls.append(("capture", config["cache_dir"]))
        screenshot.write_bytes(b"png")
        return screenshot

    class Files:
        @staticmethod
        def create(*, conversation_id, file):
            calls.append(("upload", conversation_id, file.read()))
            return SimpleNamespace(file_id="file-current-desktop")

    client = SimpleNamespace(
        conversational_ai=SimpleNamespace(
            conversations=SimpleNamespace(files=Files())
        )
    )

    file_id = capture_and_upload_screenshot(
        client,
        "conversation-1",
        {"cache_dir": str(tmp_path)},
        capture=capture,
    )

    assert file_id == "file-current-desktop"
    assert calls == [
        ("capture", str(tmp_path)),
        ("upload", "conversation-1", b"png"),
    ]
    assert not screenshot.exists()


def test_native_screenshot_upload_deletes_local_file_when_upload_fails(tmp_path):
    screenshot = tmp_path / "screenshot.png"

    def capture(_config):
        screenshot.write_bytes(b"png")
        return screenshot

    class Files:
        @staticmethod
        def create(**_arguments):
            raise RuntimeError("upload failed")

    client = SimpleNamespace(
        conversational_ai=SimpleNamespace(
            conversations=SimpleNamespace(files=Files())
        )
    )

    with pytest.raises(RuntimeError, match="upload failed"):
        capture_and_upload_screenshot(
            client,
            "conversation-1",
            {},
            capture=capture,
        )

    assert not screenshot.exists()


def test_native_screenshot_upload_requires_live_conversation_id():
    with pytest.raises(RuntimeError, match="conversation ID"):
        capture_and_upload_screenshot(SimpleNamespace(), "", {})
