import sys
from array import array
from types import SimpleNamespace

import pytest

from omarvis.dictate import (
    AudioRecorder,
    DictationService,
    clean_transcript,
    inject_text,
    scribe_transcriber,
    text_chunks,
)


@pytest.mark.parametrize(
    "raw, cleanup, expected",
    [
        ("hello world\n", False, "hello world"),
        ("hello world\r\n\r\n", False, "hello world"),
        ("  hello\n  world \n", True, "hello world"),
        ("--no-shell-expansion $HOME\n", False, "--no-shell-expansion $HOME"),
    ],
)
def test_transcript_cleanup_always_strips_trailing_newlines(
    raw, cleanup, expected
):
    assert clean_transcript(raw, cleanup=cleanup) == expected


def test_long_transcripts_are_chunked_without_changing_text():
    text = "abcdefghijklmnopqrstuvwxyz"

    chunks = text_chunks(text, chunk_size=7)

    assert chunks == ["abcdefg", "hijklmn", "opqrstu", "vwxyz"]
    assert "".join(chunks) == text


def test_wtype_injection_is_direct_chunked_and_has_no_newline():
    calls = []

    def runner(argv, **options):
        calls.append((argv, options))

    inject_text("abcdefghij", chunk_size=4, runner=runner)

    assert [call[0] for call in calls] == [
        ["wtype", "--", "abcd"],
        ["wtype", "--", "efgh"],
        ["wtype", "--", "ij"],
    ]
    assert all("\n" not in call[0][-1] for call in calls)
    assert all(call[1]["check"] is True for call in calls)


def test_dictation_service_records_transcribes_cleans_and_injects():
    events = []
    injected = []

    class Recorder:
        def start(self):
            events.append("mic-start")

        def stop(self):
            events.append("mic-stop")
            return b"pcm"

    class Notifier:
        def start(self):
            events.append("notify-start")

        def update(self, text):
            events.append(("notify", text))

    service = DictationService(
        recorder=Recorder(),
        transcriber=lambda audio: "  dictated   words\n",
        injector=injected.append,
        cleanup=True,
        notifier=Notifier(),
        event_sink=events.append,
    )

    assert service.start() == "recording"
    assert service.stop() == "transcribing"
    service.wait()

    assert injected == ["dictated words"]
    assert service.state == "idle"
    assert {"event": "dictation", "state": "recording"} in events
    assert {"event": "dictation", "state": "transcribing"} in events
    assert {
        "event": "dictation",
        "state": "idle",
        "text": "dictated words",
    } in events


def test_audio_recorder_emits_throttled_recording_levels(monkeypatch):
    opened = {}

    class Stream:
        def stop_stream(self):
            pass

        def close(self):
            pass

    class PyAudio:
        def open(self, **options):
            opened.update(options)
            return Stream()

        def terminate(self):
            pass

    fake_pyaudio = SimpleNamespace(
        PyAudio=PyAudio,
        paInt16=8,
        paContinue=0,
    )
    monkeypatch.setitem(sys.modules, "pyaudio", fake_pyaudio)
    events = []
    recorder = AudioRecorder()
    service = DictationService(
        recorder=recorder,
        transcriber=lambda _audio: "captured",
        injector=lambda _text: None,
        notifier=SimpleNamespace(start=lambda: None, update=lambda _text: None),
        event_sink=events.append,
    )

    assert service.start() == "recording"
    callback = opened["stream_callback"]
    callback(array("h", [16000] * 1024).tobytes(), 1024, None, 0)
    callback(array("h", [8000] * 1024).tobytes(), 1024, None, 0)
    callback(array("h", [0] * 1024).tobytes(), 1024, None, 0)
    assert service.stop() == "transcribing"
    service.wait()

    recording_levels = [
        event["level"]
        for event in events
        if isinstance(event, dict) and "level" in event
    ]
    assert recording_levels == [pytest.approx(0.488, abs=0.001), 0.0]


def test_dictation_plays_open_and_close_earcons_when_enabled():
    sounds = []

    class Recorder:
        def start(self):
            pass

        def stop(self):
            return b"pcm"

    service = DictationService(
        recorder=Recorder(),
        transcriber=lambda _audio: "captured",
        injector=lambda _text: None,
        notifier=SimpleNamespace(start=lambda: None, update=lambda _text: None),
        earcons_enabled=True,
        sound_player=lambda name, *, enabled: sounds.append((name, enabled)),
    )

    assert service.start() == "recording"
    assert service.stop() == "transcribing"
    service.wait()

    assert sounds == [("mic-open", True), ("mic-close", True)]


def test_dictation_plays_error_earcon_on_capture_failure():
    sounds = []
    service = DictationService(
        recorder=SimpleNamespace(
            start=lambda: (_ for _ in ()).throw(RuntimeError("mic failed"))
        ),
        transcriber=lambda _audio: "",
        injector=lambda _text: None,
        notifier=SimpleNamespace(start=lambda: None, update=lambda _text: None),
        earcons_enabled=True,
        sound_player=lambda name, *, enabled: sounds.append((name, enabled)),
    )

    assert service.start() == "error"
    assert sounds == [("error", True)]


def test_scribe_uses_raw_16khz_pcm_and_language_hint(monkeypatch):
    calls = []
    client = SimpleNamespace(
        speech_to_text=SimpleNamespace(
            convert=lambda **options: calls.append(options)
            or SimpleNamespace(text="hello")
        )
    )
    monkeypatch.setattr("elevenlabs.ElevenLabs", lambda *, api_key: client)

    transcribe = scribe_transcriber(
        "key", {"model_id": "scribe_v2", "language": "sv"}
    )

    assert transcribe(b"pcm") == "hello"
    assert calls == [
        {
            "model_id": "scribe_v2",
            "file": b"pcm",
            "file_format": "pcm_s16le_16",
            "tag_audio_events": False,
            "diarize": False,
            "language_code": "sv",
        }
    ]
