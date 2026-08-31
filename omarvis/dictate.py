from __future__ import annotations

import json
import re
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .daemon import load_api_key, load_config

SAMPLE_RATE = 16_000
CHANNELS = 1
FRAMES_PER_BUFFER = 1024


def clean_transcript(text: str, *, cleanup: bool) -> str:
    """Prepare Scribe text for direct typing without ever adding Enter."""
    without_trailing_newline = text.rstrip("\r\n")
    if not cleanup:
        return without_trailing_newline
    return re.sub(r"\s+", " ", without_trailing_newline).strip()


def text_chunks(text: str, *, chunk_size: int = 500) -> list[str]:
    if chunk_size < 1:
        raise ValueError("dictation chunk_size must be positive")
    return [text[index : index + chunk_size] for index in range(0, len(text), chunk_size)]


def inject_text(
    text: str,
    *,
    chunk_size: int = 500,
    runner: Callable[..., Any] = subprocess.run,
) -> None:
    for chunk in text_chunks(text, chunk_size=chunk_size):
        runner(
            ["wtype", "--", chunk],
            check=True,
            timeout=10,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


class AudioRecorder:
    def __init__(self, input_device_index: int | None = None) -> None:
        self.input_device_index = input_device_index
        self._audio: Any = None
        self._stream: Any = None
        self._frames: list[bytes] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._stream is not None:
            raise RuntimeError("dictation is already recording")
        try:
            import pyaudio
        except ImportError as error:
            raise RuntimeError("PyAudio is not installed. Run bin/omarvis-setup.") from error

        with self._lock:
            self._frames = []
        self._audio = pyaudio.PyAudio()

        def capture(
            data: bytes, _frame_count: int, _time_info: Any, _status: int
        ) -> tuple[None, int]:
            with self._lock:
                self._frames.append(data)
            return None, pyaudio.paContinue

        try:
            self._stream = self._audio.open(
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                input=True,
                input_device_index=self.input_device_index,
                frames_per_buffer=FRAMES_PER_BUFFER,
                stream_callback=capture,
                start=True,
            )
        except Exception:
            self._audio.terminate()
            self._audio = None
            raise

    def stop(self) -> bytes:
        if self._stream is None:
            return b""
        stream = self._stream
        audio = self._audio
        self._stream = None
        self._audio = None
        try:
            stream.stop_stream()
            stream.close()
        finally:
            if audio is not None:
                audio.terminate()
        with self._lock:
            captured = b"".join(self._frames)
            self._frames = []
        return captured


class DictationNotifier:
    def __init__(self, runner: Callable[..., Any] = subprocess.run) -> None:
        self.runner = runner
        self.notification_id = ""

    def start(self) -> None:
        try:
            completed = self.runner(
                [
                    "omarchy-notification-send",
                    "-p",
                    "-g",
                    chr(0xF130),
                    "Omarvis",
                    "● Dictating…",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if completed.returncode == 0:
                self.notification_id = completed.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass

    def update(self, text: str) -> None:
        command = ["omarchy-notification-send"]
        if self.notification_id:
            command.extend(("-r", self.notification_id))
        command.extend(("Omarvis Dictation", text[:240]))
        try:
            self.runner(
                command,
                timeout=2,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            pass


def scribe_transcriber(
    api_key: str, config: Mapping[str, Any]
) -> Callable[[bytes], str]:
    from elevenlabs import ElevenLabs

    client = ElevenLabs(api_key=api_key)
    model_id = str(config.get("model_id") or "scribe_v2")
    language = str(config.get("language") or "").strip()

    def transcribe(audio: bytes) -> str:
        options: dict[str, Any] = {
            "model_id": model_id,
            "file": audio,
            "file_format": "pcm_s16le_16",
            "tag_audio_events": False,
            "diarize": False,
        }
        if language:
            options["language_code"] = language
        response = client.speech_to_text.convert(**options)
        return str(getattr(response, "text", ""))

    return transcribe


class DictationService:
    def __init__(
        self,
        *,
        recorder: Any,
        transcriber: Callable[[bytes], str],
        injector: Callable[[str], None],
        cleanup: bool = True,
        notifier: Any | None = None,
        event_sink: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.recorder = recorder
        self.transcriber = transcriber
        self.injector = injector
        self.cleanup = cleanup
        self.notifier = notifier or DictationNotifier()
        self.event_sink = event_sink
        self.state = "idle"
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    def _emit(self, state: str, **extra: Any) -> None:
        self.state = state
        if self.event_sink is not None:
            self.event_sink({"event": "dictation", "state": state, **extra})

    def start(self) -> str:
        with self._lock:
            if self.state != "idle":
                return f"already-{self.state}"
            try:
                self.recorder.start()
            except Exception as error:
                self._emit("error", message=str(error))
                self._emit("idle")
                return "error"
            self.notifier.start()
            self._emit("recording")
            return "recording"

    def stop(self) -> str:
        with self._lock:
            if self.state != "recording":
                return "not-recording"
            try:
                audio = self.recorder.stop()
            except Exception as error:
                self.notifier.update(f"Dictation failed: {error}")
                self._emit("error", message=str(error))
                self._emit("idle")
                return "error"
            self._emit("transcribing")
            self._worker = threading.Thread(
                target=self._finish, args=(audio,), daemon=True
            )
            self._worker.start()
            return "transcribing"

    def _finish(self, audio: bytes) -> None:
        try:
            if not audio:
                raise RuntimeError("No audio was captured")
            transcript = clean_transcript(
                self.transcriber(audio), cleanup=self.cleanup
            )
            if not transcript:
                raise RuntimeError("No speech was detected")
            self.injector(transcript)
            self.notifier.update(transcript)
            with self._lock:
                self._emit("idle", text=transcript)
        except Exception as error:
            self.notifier.update(f"Dictation failed: {error}")
            with self._lock:
                self._emit("error", message=str(error))
                self._emit("idle")

    def wait(self, timeout: float = 10.0) -> None:
        worker = self._worker
        if worker is not None:
            worker.join(timeout)

    def close(self) -> None:
        with self._lock:
            if self.state == "recording":
                self.recorder.stop()
            self._emit("idle")


def emit_event(event: Mapping[str, Any]) -> None:
    print(json.dumps(dict(event), ensure_ascii=False), flush=True)


def main(_argv: Sequence[str] | None = None) -> int:
    config = load_config()
    dictation = dict(config.get("dictation") or {})
    api_key = load_api_key()

    def missing_key(_audio: bytes) -> str:
        raise RuntimeError(
            "ELEVENLABS_API_KEY is missing. Run bin/omarvis-setup."
        )

    transcriber = scribe_transcriber(api_key, dictation) if api_key else missing_key
    chunk_size = int(dictation.get("chunk_size", 500))
    service = DictationService(
        recorder=AudioRecorder(config.get("input_device_index")),
        transcriber=transcriber,
        injector=lambda text: inject_text(text, chunk_size=chunk_size),
        cleanup=bool(dictation.get("cleanup", True)),
        event_sink=emit_event,
    )

    def terminate(_signum: int, _frame: Any) -> None:
        service.close()
        raise SystemExit(0)

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, terminate)

    emit_event({"event": "dictation", "state": "idle", "ready": True})
    for raw_command in sys.stdin:
        command = raw_command.strip().lower()
        if command == "start":
            service.start()
        elif command == "stop":
            service.stop()
        elif command in {"quit", "exit"}:
            service.close()
            break
        elif command:
            emit_event(
                {
                    "event": "dictation",
                    "state": service.state,
                    "message": f"Unknown dictation command: {command}",
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
