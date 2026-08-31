from __future__ import annotations

import json
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .daemon import earcons_enabled, load_api_key, load_config
from .levels import LevelThrottle, rms_level
from .sounds import play as play_sound

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
    def __init__(
        self,
        input_device_index: int | None = None,
        level_sink: Callable[[float], None] | None = None,
    ) -> None:
        self.input_device_index = input_device_index
        self.level_sink = level_sink
        self._audio: Any = None
        self._stream: Any = None
        self._frames: list[bytes] = []
        self._lock = threading.Lock()
        self._levels: LevelThrottle | None = None

    def set_level_sink(self, sink: Callable[[float], None]) -> None:
        self.level_sink = sink

    def start(self) -> None:
        if self._stream is not None:
            raise RuntimeError("dictation is already recording")
        try:
            import pyaudio
        except ImportError as error:
            raise RuntimeError("PyAudio is not installed. Run bin/omarvis-setup.") from error

        with self._lock:
            self._frames = []
        self._levels = LevelThrottle(
            lambda in_level, _out_level: self.level_sink(in_level)
            if self.level_sink is not None
            else None
        )
        self._audio = pyaudio.PyAudio()

        def capture(
            data: bytes, _frame_count: int, _time_info: Any, _status: int
        ) -> tuple[None, int]:
            with self._lock:
                self._frames.append(data)
            if self._levels is not None:
                self._levels.update_in(rms_level(data))
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
        self._levels = None
        return captured


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
        event_sink: Callable[[Mapping[str, Any]], None] | None = None,
        earcons_enabled: bool = False,
        sound_player: Callable[..., None] = play_sound,
        tap_discard_seconds: float = 1.0,
        max_recording_seconds: float = 90.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.recorder = recorder
        self.transcriber = transcriber
        self.injector = injector
        self.cleanup = cleanup
        self.event_sink = event_sink
        self.earcons_enabled = earcons_enabled
        self.sound_player = sound_player
        # A stop arriving within tap_discard_seconds of start is a tap on
        # the push-to-talk key, not a release after speech: discard the
        # recording quietly instead of transcribing a fraction of a second
        # of silence (hands-free is entered only via the Space chord). The
        # window is measured daemon-side, and each of start/stop rides a
        # separate keybind → omarchy-shell spawn → IPC → stdin hop, so it
        # must absorb that round-trip jitter on top of the physical tap —
        # hence a full second rather than a keyboard-repeat-scale
        # threshold. Deliberate push-to-talk holds are speech-length
        # (several seconds), so the wider window doesn't misread them.
        # max_recording_seconds caps a hands-free session so a forgotten
        # lock can't leave the microphone open indefinitely.
        self.tap_discard_seconds = tap_discard_seconds
        self.max_recording_seconds = max_recording_seconds
        self.clock = clock
        self.state = "idle"
        self.locked = False
        self._recording_started_at: float | None = None
        self._cap_timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    def _emit(self, state: str, **extra: Any) -> None:
        self.state = state
        if self.event_sink is not None:
            self.event_sink({"event": "dictation", "state": state, **extra})

    def _play(self, name: str) -> None:
        self.sound_player(name, enabled=self.earcons_enabled)

    def start(self) -> str:
        with self._lock:
            if self.state != "idle":
                return f"already-{self.state}"
            try:
                if hasattr(self.recorder, "set_level_sink"):
                    self.recorder.set_level_sink(self._recording_level)
                self.recorder.start()
            except Exception as error:
                self._emit("error", message=str(error))
                self._play("error")
                self._emit("idle")
                return "error"
            self.locked = False
            self._recording_started_at = self.clock()
            self._start_cap_timer()
            self._play("mic-open")
            self._emit("recording")
            return "recording"

    def _start_cap_timer(self) -> None:
        if self.max_recording_seconds <= 0:
            return
        self._cap_timer = threading.Timer(self.max_recording_seconds, self.stop)
        self._cap_timer.daemon = True
        self._cap_timer.start()

    def _cancel_cap_timer(self) -> None:
        if self._cap_timer is not None:
            self._cap_timer.cancel()
            self._cap_timer = None

    def _recording_level(self, level: float) -> None:
        if self.state == "recording":
            self._emit("recording", level=round(float(level), 3))

    def stop(self) -> str:
        with self._lock:
            if self.state != "recording":
                return "not-recording"
            if (
                not self.locked
                and self._recording_started_at is not None
                and self.clock() - self._recording_started_at < self.tap_discard_seconds
            ):
                return self._discard_locked()
            self._cancel_cap_timer()
            self.locked = False
            try:
                audio = self.recorder.stop()
            except Exception as error:
                self._emit("error", message=str(error))
                self._play("error")
                self._emit("idle")
                return "error"
            self._play("mic-close")
            self._emit("transcribing")
            self._worker = threading.Thread(
                target=self._finish, args=(audio,), daemon=True
            )
            self._worker.start()
            return "transcribing"

    def handsfree(self) -> str:
        """Lock the current recording open (Wispr-style Super+J+Space chord)."""
        with self._lock:
            if self.state != "recording":
                return "not-recording"
            if not self.locked:
                self.locked = True
                self._emit("recording", locked=True)
            return "locked"

    def cancel(self) -> str:
        """Discard the current recording without transcribing or typing."""
        with self._lock:
            if self.state != "recording":
                return "not-recording"
            return self._discard_locked()

    def _discard_locked(self) -> str:
        """Close the mic and drop the audio. Caller must hold self._lock."""
        self._cancel_cap_timer()
        self.locked = False
        try:
            self.recorder.stop()
        except Exception as error:
            self._emit("error", message=str(error))
            self._play("error")
            self._emit("idle")
            return "error"
        self._play("mic-close")
        self._emit("idle", canceled=True)
        return "canceled"

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
            with self._lock:
                self._emit("idle", text=transcript)
        except Exception as error:
            with self._lock:
                self._emit("error", message=str(error))
                self._play("error")
                self._emit("idle")

    def wait(self, timeout: float = 10.0) -> None:
        worker = self._worker
        if worker is not None:
            worker.join(timeout)

    def close(self) -> None:
        with self._lock:
            self._cancel_cap_timer()
            if self.state == "recording":
                self.recorder.stop()
                self._play("mic-close")
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
        earcons_enabled=earcons_enabled(config),
        tap_discard_seconds=float(dictation.get("tap_discard_ms", 1000)) / 1000.0,
        max_recording_seconds=float(dictation.get("max_recording_seconds", 90)),
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
        elif command == "handsfree":
            service.handsfree()
        elif command == "cancel":
            service.cancel()
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
