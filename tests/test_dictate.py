from types import SimpleNamespace

import pytest

from omarvis.dictate import (
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
