import json
import subprocess
import sys
from array import array
from types import SimpleNamespace

import pytest

from omarvis.dictate import (
    AudioRecorder,
    DictationService,
    clean_transcript,
    copy_to_clipboard,
    inject_text,
    scribe_transcriber,
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


def _paste_runner(calls, window_class):
    class Result:
        stdout = json.dumps({"class": window_class}).encode()

    def runner(argv, **options):
        calls.append((argv, options))
        return Result()

    return runner


def test_injection_is_a_single_ctrl_v_paste_not_typing():
    calls = []

    inject_text(
        "any transcript at all",
        runner=_paste_runner(calls, "chromium"),
        sleeper=lambda _s: None,
    )

    # One hyprctl probe, one paste chord: the text appears at once from the
    # clipboard (already set by copy_to_clipboard), never typed out.
    assert [call[0] for call in calls] == [
        ["hyprctl", "activewindow", "-j"],
        ["wtype", "-M", "ctrl", "-P", "v", "-p", "v", "-m", "ctrl"],
    ]
    assert all(call[1]["check"] is True for call in calls)


def test_injection_uses_ctrl_shift_v_in_terminals():
    calls = []

    inject_text(
        "ls -la",
        runner=_paste_runner(calls, "Alacritty"),
        sleeper=lambda _s: None,
    )

    assert calls[-1][0] == [
        "wtype", "-M", "ctrl", "-M", "shift", "-P", "v", "-p", "v",
        "-m", "shift", "-m", "ctrl",
    ]


def test_injection_falls_back_to_plain_paste_when_hyprctl_fails():
    calls = []

    def runner(argv, **options):
        calls.append(argv)
        if argv[0] == "hyprctl":
            raise RuntimeError("no compositor")

    inject_text("words", runner=runner, sleeper=lambda _s: None)

    assert calls[-1] == ["wtype", "-M", "ctrl", "-P", "v", "-p", "v", "-m", "ctrl"]


def test_injection_of_empty_text_does_nothing():
    calls = []

    inject_text("", runner=_paste_runner(calls, "chromium"), sleeper=lambda _s: None)

    assert calls == []


def test_clipboard_copy_uses_safe_argv_and_detaches_all_streams():
    calls = []

    def runner(argv, **options):
        calls.append((argv, options))

    copy_to_clipboard("--literal $HOME", runner=runner)

    assert calls == [
        (
            ["wl-copy", "--", "--literal $HOME"],
            {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "timeout": 5,
                "check": False,
            },
        )
    ]


def test_clipboard_failure_never_prevents_injection(monkeypatch):
    injected = []
    monkeypatch.setattr(
        "omarvis.dictate.subprocess.run",
        lambda *_args, **_options: (_ for _ in ()).throw(OSError("no clipboard")),
    )
    service = DictationService(
        recorder=_PcmRecorder(),
        transcriber=lambda _audio: "still type this",
        injector=injected.append,
        tap_discard_seconds=0.0,
    )

    service.start()
    service.stop()
    service.wait()

    assert injected == ["still type this"]
    assert service.state == "idle"


def test_injector_failure_keeps_transcript_and_emits_one_idle(monkeypatch):
    events = []
    clipboard = []
    monkeypatch.setattr(
        "omarvis.dictate.subprocess.run",
        lambda argv, **options: clipboard.append((argv, options)),
    )
    service = DictationService(
        recorder=_PcmRecorder(),
        transcriber=lambda _audio: "recoverable words",
        injector=lambda _text: (_ for _ in ()).throw(RuntimeError("wtype failed")),
        event_sink=events.append,
        tap_discard_seconds=0.0,
    )

    service.start()
    service.stop()
    service.wait()

    assert clipboard[0][0] == ["wl-copy", "--", "recoverable words"]
    assert {
        "event": "dictation",
        "state": "error",
        "message": "wtype failed",
        "text": "recoverable words",
    } in events
    assert sum(event.get("state") == "idle" for event in events) == 1
    assert service.state == "idle"


@pytest.mark.parametrize(
    "audio, transcription, message",
    [
        (b"", "unused", "No audio was captured"),
        (b"pcm", "", "No speech was detected"),
    ],
)
def test_pre_transcript_failures_do_not_touch_clipboard(
    monkeypatch, audio, transcription, message
):
    events = []
    clipboard = []
    monkeypatch.setattr(
        "omarvis.dictate.subprocess.run",
        lambda *args, **options: clipboard.append((args, options)),
    )

    class Recorder:
        def start(self):
            pass

        def stop(self):
            return audio

    service = DictationService(
        recorder=Recorder(),
        transcriber=lambda _audio: transcription,
        injector=lambda _text: None,
        event_sink=events.append,
        tap_discard_seconds=0.0,
    )

    service.start()
    service.stop()
    service.wait()

    assert clipboard == []
    assert any(event.get("message") == message for event in events)
    assert service.state == "idle"


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


def test_dictation_is_silent_by_design():
    # Earcons were removed outright: the service has no sound hooks at all,
    # so nothing can beep no matter what the config says.
    import omarvis.dictate as dictate

    source = open(dictate.__file__).read()
    for retired in ("sound_player", "earcons_enabled", "_play", "play_sound"):
        assert retired not in source


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
    assert service.stop() == "locked"  # the chord's own key release
    assert service.stop() == "transcribing"
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
    assert service.stop() == "locked"  # the chord's own key release
    assert service.stop() == "transcribing"
    service.wait()


def test_release_after_handsfree_chord_does_not_stop_recording():
    # Wispr flow: hold Super+J, press Space (-> handsfree), release the keys.
    # The release still sends one stop; it must be swallowed so the recording
    # stays open. The next Super+J press is the "send" gesture, and its own
    # release then finds nothing to stop.
    events = []
    clock = SimpleNamespace(now=0.0)
    service = _tap_service(events, clock)

    service.start()
    clock.now = 5.0
    assert service.handsfree() == "locked"
    clock.now = 5.2
    assert service.stop() == "locked"
    assert service.state == "recording"
    assert service.locked is True

    clock.now = 20.0
    assert service.start() == "transcribing"
    assert service.stop() == "not-recording"
    service.wait()
    assert service.state == "idle"


def test_only_one_release_is_swallowed_after_hands_free():
    events = []
    clock = SimpleNamespace(now=0.0)
    service = _tap_service(events, clock)

    service.start()
    clock.now = 5.0
    service.handsfree()
    assert service.stop() == "locked"
    clock.now = 9.0
    assert service.stop() == "transcribing"
    service.wait()
    assert service.state == "idle"


def test_safety_cap_ends_a_hands_free_recording_even_before_the_release():
    events = []
    clock = SimpleNamespace(now=0.0)
    service = _tap_service(events, clock)

    service.start()
    clock.now = 5.0
    service.handsfree()
    service._cap_reached()
    service.wait()
    assert service.state == "idle"
    assert service.locked is False


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
    assert service.stop() == "locked"  # the chord's own key release
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
