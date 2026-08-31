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

    service = DictationService(
        recorder=Recorder(),
        transcriber=lambda audio: "  dictated   words\n",
        injector=injected.append,
        cleanup=True,
        event_sink=events.append,
        tap_discard_seconds=0.0,
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
        event_sink=events.append,
        tap_discard_seconds=0.0,
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
        earcons_enabled=True,
        sound_player=lambda name, *, enabled: sounds.append((name, enabled)),
        tap_discard_seconds=0.0,
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
        earcons_enabled=True,
        sound_player=lambda name, *, enabled: sounds.append((name, enabled)),
    )

    assert service.start() == "error"
    assert sounds == [("error", True)]


class _PcmRecorder:
    def start(self):
        pass

    def stop(self):
        return b"pcm"


def _tap_service(events, clock, **overrides):
    options = {
        "recorder": _PcmRecorder(),
        "transcriber": lambda _audio: "captured",
        "injector": lambda _text: None,
        "event_sink": events.append,
        "tap_discard_seconds": 0.3,
        "clock": lambda: clock.now,
    }
    options.update(overrides)
    return DictationService(**options)


def test_default_tap_window_absorbs_keybind_ipc_round_trip():
    # start/stop each arrive through a keybind → omarchy-shell → IPC → stdin
    # chain, so the default window must be far wider than a raw key tap.
    events = []
    clock = SimpleNamespace(now=0.0)
    service = DictationService(
        recorder=_PcmRecorder(),
        transcriber=lambda _audio: "captured",
        injector=lambda _text: None,
        event_sink=events.append,
        clock=lambda: clock.now,
    )

    assert service.tap_discard_seconds == 1.0

    assert service.start() == "recording"
    clock.now = 0.7
    assert service.stop() == "canceled"


def test_tap_release_inside_window_discards_quietly():
    # A bare tap must NOT enter hands-free (that's the Space chord's job)
    # and must not transcribe a fraction of a second of silence — it just
    # closes the mic and drops the audio.
    events = []
    transcribed = []
    injected = []
    clock = SimpleNamespace(now=0.0)
    service = _tap_service(
        events,
        clock,
        transcriber=lambda audio: transcribed.append(audio) or "captured",
        injector=injected.append,
    )

    assert service.start() == "recording"
    clock.now = 0.1
    assert service.stop() == "canceled"

    assert service.state == "idle"
    assert service.locked is False
    assert transcribed == []
    assert injected == []
    assert {"event": "dictation", "state": "idle", "canceled": True} in events


def test_hold_release_past_lock_window_stops_like_push_to_talk():
    events = []
    clock = SimpleNamespace(now=0.0)
    service = _tap_service(events, clock)

    assert service.start() == "recording"
    clock.now = 0.5
    assert service.stop() == "transcribing"
    service.wait()

    assert service.state == "idle"
    assert service.locked is False
    assert not any(event.get("locked") for event in events)


def test_next_recording_after_hands_free_session_starts_unlocked():
    events = []
    clock = SimpleNamespace(now=0.0)
    service = _tap_service(events, clock)

    service.start()
    assert service.handsfree() == "locked"
    clock.now = 5.0
    service.stop()
    service.wait()

    clock.now = 10.0
    assert service.start() == "recording"
    assert service.locked is False
    clock.now = 11.0
    assert service.stop() == "transcribing"
    service.wait()


def test_handsfree_command_locks_current_recording_and_is_idempotent():
    events = []
    clock = SimpleNamespace(now=0.0)
    service = _tap_service(events, clock)

    assert service.handsfree() == "not-recording"

    service.start()
    clock.now = 12.0  # deep into a hold — well past any tap window
    assert service.handsfree() == "locked"
    assert service.locked is True
    assert {"event": "dictation", "state": "recording", "locked": True} in events

    assert service.handsfree() == "locked"
    assert (
        sum(1 for event in events if event.get("locked")) == 1
    )  # idempotent: no duplicate locked emissions

    clock.now = 20.0
    assert service.stop() == "transcribing"
    service.wait()


def test_release_after_handsfree_chord_does_not_stop_recording():
    # Wispr flow: hold Super+J, press Space (→ handsfree), then release the
    # keys. The release still sends a stop; locked mode must survive only if
    # the release lands inside the tap window... it doesn't — locked mode
    # ignores the tap window entirely, so the release must be swallowed by
    # neither: locked recordings stop on the NEXT stop. Guard the actual
    # contract: after handsfree, the very next stop finishes the recording.
    events = []
    clock = SimpleNamespace(now=0.0)
    service = _tap_service(events, clock)

    service.start()
    clock.now = 5.0
    service.handsfree()
    clock.now = 5.2
    assert service.stop() == "transcribing"
    service.wait()
    assert service.state == "idle"


def test_cancel_discards_recording_without_transcribing_or_typing():
    events = []
    injected = []
    transcribed = []
    clock = SimpleNamespace(now=0.0)
    service = _tap_service(
        events,
        clock,
        transcriber=lambda audio: transcribed.append(audio) or "captured",
        injector=injected.append,
    )

    assert service.cancel() == "not-recording"

    service.start()
    clock.now = 5.0
    service.handsfree()
    clock.now = 10.0
    assert service.cancel() == "canceled"
    service.wait()

    assert service.state == "idle"
    assert service.locked is False
    assert transcribed == []
    assert injected == []
    assert {"event": "dictation", "state": "idle", "canceled": True} in events


def test_handsfree_command_locks_current_recording_via_space_chord():
    events = []
    clock = SimpleNamespace(now=0.0)
    service = _tap_service(events, clock)

    assert service.handsfree() == "not-recording"

    assert service.start() == "recording"
    clock.now = 15.0  # realized mid-hold that hands-free is wanted
    assert service.handsfree() == "locked"
    assert service.locked is True
    assert {"event": "dictation", "state": "recording", "locked": True} in events
    assert service.handsfree() == "locked"  # idempotent, no duplicate emit
    assert (
        sum(1 for event in events if event.get("locked")) == 1
    )

    clock.now = 20.0
    assert service.stop() == "transcribing"
    service.wait()
    assert service.state == "idle"


def test_cancel_discards_recording_without_transcribing_or_typing():
    events = []
    transcribed = []
    injected = []
    clock = SimpleNamespace(now=0.0)
    service = _tap_service(
        events,
        clock,
        transcriber=lambda audio: transcribed.append(audio) or "captured",
        injector=injected.append,
    )

    assert service.cancel() == "not-recording"

    service.start()
    assert service.handsfree() == "locked"
    clock.now = 5.0
    assert service.cancel() == "canceled"

    assert service.state == "idle"
    assert service.locked is False
    assert transcribed == []
    assert injected == []
    assert {"event": "dictation", "state": "idle", "canceled": True} in events

    # A fresh recording still works after a cancel.
    clock.now = 10.0
    assert service.start() == "recording"
    clock.now = 12.0
    assert service.stop() == "transcribing"
    service.wait()
    assert injected == ["captured"]


def test_hands_free_recording_auto_stops_at_safety_cap():
    import time

    events = []
    service = DictationService(
        recorder=_PcmRecorder(),
        transcriber=lambda _audio: "captured",
        injector=lambda _text: None,
        event_sink=events.append,
        tap_discard_seconds=0.0,
        max_recording_seconds=0.05,
    )

    assert service.start() == "recording"
    deadline = time.time() + 2.0
    while service.state == "recording" and time.time() < deadline:
        time.sleep(0.01)
    service.wait()

    assert service.state == "idle"
    assert {"event": "dictation", "state": "transcribing"} in events


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
