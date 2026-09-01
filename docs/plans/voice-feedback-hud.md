# Implementation plan: Voice session feedback HUD

Status: implemented, then partly superseded. Written 2026-08-31.

Superseded where it disagrees with the shipped HUD. The strip ended up
text-free, so the "text line" and "tool chip" sections below describe a design
that was not kept: conversation text lives in the bar tooltip and errors go to
the notification server. Ask mode, its `SUPER+SHIFT+J` binding, and the
`--text-only` / `--message` one-shot are gone entirely — the two-way call is
the only session type — so every reference to them below is historical.

## 1. Problem and background

Omarvis users cannot tell when they are being heard. Today the only feedback is a small
bar-widget glyph (opacity pulse while starting/recording) and desktop notifications that
fire *after* each finalized transcript. There is no signal at all while the user is
actually speaking.

Design research (Alexa design guides, NN/g, ChatGPT/Gemini/Siri, Wispr Flow,
superwhisper, HeyClicky, hyprwhspr) converged on these rules, which this plan implements:

- The core trust cue is **input-coupled motion**: a visual that reacts to the user's mic
  amplitude while they speak. A flat meter while talking is itself the error message
  (mic-health proof).
- Users read **animation coupling, not color**: listening = reacts to the user; thinking =
  self-animating; speaking = pulses with the agent's audio output.
- **Two surfaces**: a tiny persistent anchor (existing bar widget) plus a transient HUD
  that exists only while a session/dictation is active. Idle shows nothing.
- **Earcons** at mic-open and mic-close (eyes are on the user's work, not the HUD).
- Distinct, loud **error** state; never look armed while dead; avoid stale "thinking".
- Agent **tool activity gets its own indicator** (spinner → checkmark), separate from
  speech states.

Hard constraint: the ElevenLabs Python SDK delivers only **final** user transcripts — no
partial transcripts, no VAD events (unknown websocket message types are silently
dropped). Live word-by-word display of the user's speech is impossible with this
provider. The substitute is local amplitude metering: we own the PyAudio input and
output streams in both the session daemon and the dictation daemon.

Out of scope for this plan (do not implement):
- Tap-vs-hold / locked ("hands-free") dictation. Separately decided, not yet scheduled.
  The HUD must simply not preclude a future "locked" dictation state.
- Any change of STT/agent provider; live partial user transcripts.
- Changes to policy, confirmation, or command execution behavior.

## 2. Architecture overview

```
 PyAudio in/out streams                 ElevenLabs SDK callbacks
        │ RMS metering                        │ finals + deltas
        ▼                                     ▼
  omarvis-run daemon ──── JSON events on stdout ────► Service.qml
  omarvis-dictate daemon ─ JSON events on stdout ───► Service.qml
                                                        │ properties
                                          ┌─────────────┴─────────────┐
                                          ▼                           ▼
                                    BarWidget.qml (anchor)      HudWindow.qml (new,
                                                                layer-shell overlay)
```

All new UI state flows through the existing stdout JSON event pipe — no new IPC.

## 3. Event protocol additions (daemon stdout)

New event types, all backward compatible (Service.qml already ignores unknown fields;
unknown *events* must be ignored, verify `handleEvent` does not error on them):

| Event | Shape | Source | Cadence |
|---|---|---|---|
| `level` | `{"event":"level","in":0.42,"out":0.0}` | omarvis-run audio interface | throttled, max 10 Hz; values 0.0–1.0, 3 decimals |
| `agent_part` | `{"event":"agent_part","text":"...","type":"delta"}` | SDK `callback_agent_chat_response_part` | as delivered |
| `running` | `{"event":"running","command":"omarchy-theme-set ..."}` | RunToolHandler, immediately before execution | per tool call |
| `state` gains value `"thinking"` | `{"event":"state","state":"thinking","mode":...}` | daemon | on final user transcript |
| dictation events gain `level` | `{"event":"dictation","state":"recording","level":0.4}` | omarvis-dictate | max 10 Hz while recording |

State machine changes in `run_session` (omarvis/daemon.py):
- `on_user` (final user transcript): emit `state: thinking` (currently emits
  `listening`). The mic is technically still open (full duplex barge-in) — the HUD's
  user-level meter stays live regardless, so the coarse state drives only the label and
  the agent-dot animation.
- First `agent_part` delta or `on_agent`: emit `state: speaking` (already done for
  `on_agent`).
- Return to `listening`: QML-side fallback — while `sessionState === "speaking"`, if
  `out` level stays below a small epsilon for 1.5 s, HUD displays listening again. Do
  not add daemon-side playback-end detection in this iteration.

## 4. Python changes

### 4.1 `omarvis/levels.py` (new module)

Pure, unit-testable helpers:

```python
def rms_level(chunk: bytes) -> float:
    """RMS of int16 mono PCM, normalized to 0.0–1.0."""

class LevelThrottle:
    """Hold latest in/out values; decide when to emit (>=100ms since last emit,
    or a zero-crossing edge so silence onset is never missed). Also force-emit
    in=0.0,out=0.0 once when a session ends so the HUD can't stick on a stale level."""
```

Use `audioop`-free pure-Python or `struct`/`array` math (audioop is removed in 3.13+;
this venv is Python 3.14). RMS over a 250 ms chunk is ~4000 samples — fine in pure
Python at 4 Hz, but use `array('h', chunk)` not per-byte struct unpacking.

### 4.2 `omarvis/daemon.py` — metered audio interface

Replace `_selected_audio_interface` with one class used for **all** voice sessions
(device-index override or not):

```python
def _metered_audio_interface(input_device_index, level_sink):
    class MeteredAudioInterface(DefaultAudioInterface):
        # override start() as the existing SelectedAudioInterface does, passing
        # input_device_index=... when set (keep current behavior when None: plain open)
        def _in_callback(self, in_data, frame_count, time_info, status):
            level_sink.update_in(rms_level(in_data))   # 4 Hz (250ms buffers)
            return super()._in_callback(in_data, frame_count, time_info, status)
        def _output_thread(self):
            # copy of SDK loop (queue.get timeout 0.25) with two additions:
            #   level_sink.update_out(rms_level(audio)) before out_stream.write
            #   level_sink.update_out(0.0) on queue.Empty
    return MeteredAudioInterface()
```

`level_sink` wraps `LevelThrottle` and calls `emit_event({"event":"level",...})`.
Keep the SDK's buffer sizes (250 ms in / 62.5 ms out) — do NOT change network chunking.
4 Hz input updates are acceptable; the QML side smooths with animation (Section 5).
Note the SDK's `_output_thread` is a private method copied here; pin/verify the
installed `elevenlabs` package version in a comment and in `omarvis-setup`'s pin.

On session teardown (the `finally` block in `run_session`), emit a final
`{"event":"level","in":0.0,"out":0.0}`.

### 4.3 `omarvis/daemon.py` — streaming agent text

Wire `callback_agent_chat_response_part=on_agent_part` into the `Conversation(...)`
construction:

```python
def on_agent_part(text: str, part_type) -> None:
    if first delta of a turn: emit_state("speaking")
    emit_event({"event": "agent_part", "text": text, "type": str(getattr(part_type, "value", part_type))})
```

`on_agent` (final) stays as is and remains the notification source. Do not send
`agent_part` text to `Notifier` (too chatty).

### 4.4 `omarvis/daemon.py` — `running` event

In `RunToolHandler`, at the point where a validated command is about to execute (the
same code paths that later emit `"ran"` — both the sync path and the background/`exit:
None` path), first emit `{"event": "running", "command": <same display string used by
"ran">}` via the existing `event_sink`. No behavior change to execution or policy.

### 4.5 `omarvis/dictate.py` — recording levels

In `AudioRecorder`, accept an optional `level_sink` callback; compute `rms_level` per
capture buffer (1024 frames = 64 ms) inside the existing `capture` callback and throttle
to 10 Hz. `DictationService.start` passes a sink that emits
`{"event":"dictation","state":"recording","level":x}` through the existing
`event_sink`. Service.qml already tolerates repeated same-state dictation events
(verify; `handleDictationEvent` just re-assigns state).

### 4.6 Earcons

- New assets: `assets/sounds/mic-open.wav`, `mic-close.wav`, `error.wav`. Generate them
  in-repo with a small script (`bin/omarvis-make-sounds`, python, stdlib `wave`): short
  chirps ≤150 ms, ~-12 dBFS — up-chirp for open, down-chirp for close, low buzz for
  error. Committing the tiny wav files is fine; the script documents provenance.
- New helper `omarvis/sounds.py`: `play(name, *, enabled)` →
  `subprocess.Popen(["pw-play", path], stdout/err=DEVNULL)`, swallow all errors
  (pw-play ships with PipeWire on Omarchy).
- Trigger points: session `starting`→connected (`emit_state("listening")` after
  `wait_for_conversation_connection`) plays mic-open; session teardown plays mic-close;
  daemon fatal error path plays error. Dictation start → mic-open, stop → mic-close,
  dictation error → error.
- Config: `"ui": {"earcons": true}` in `~/.config/omarchy/omarvis/config.json`,
  read via existing `load_config`. Default true when absent.

### 4.7 `--simulate` mode (test/dev harness, required)

`bin/omarvis-run --simulate` (flag in `omarvis/daemon.py` argparse; also honored when
env `OMARVIS_SIMULATE=1` so Service.qml's fixed command line can trigger it without QML
changes). Instead of connecting to ElevenLabs/PyAudio, it plays a scripted timeline of
the full event vocabulary to stdout in real time (~20 s, then exits 0):

starting → listening → level sweeps (in: 0→0.8→0 sine) → user transcript → thinking →
agent_part deltas → speaking + out-level sweeps → running + ran → another user turn →
error demo (only when `OMARVIS_SIMULATE_ERROR=1`) → idle.

Timeline lives in `omarvis/simulate.py` as data (list of `(delay, event)` tuples) so
tests can run it with delay-scale 0. No API key, no audio hardware, no network.

## 5. QML changes

### 5.1 `Service.qml`

- New properties: `inLevel`, `outLevel` (real), `streamingAgent` (string),
  `runningCommand` (string), `dictationLevel` (real).
- `handleEvent` additions:
  - `level` → set `inLevel`/`outLevel`.
  - `agent_part` → append to `streamingAgent` (reset it on each `user` event and on
    final `agent` event, which also sets `lastAgent` as today).
  - `running` → set `runningCommand`; `ran` → clear it.
  - unknown events remain ignored.
- `handleDictationEvent`: pick up optional `level`.
- On daemon `onExited`: zero `inLevel`/`outLevel`, clear `streamingAgent` and
  `runningCommand` (stale-state hygiene).

### 5.2 `HudWindow.qml` (new file, instantiated from `Service.qml`)

A Quickshell `PanelWindow` (WlrLayershell overlay layer, no exclusive zone, no keyboard
focus), anchored top-center by default; config-overridable position via bar-widget
setting or config key `ui.hud_position` in `{"top-center","top-right"}` — implementer's
choice of plumbing, but the position must be user-changeable. **Visible only when**
`sessionState !== "idle" || dictationState !== "idle"`.

Layout (one horizontal pill, ~420 px max width, theme colors from the shell's palette
the same way BarWidget uses `bar.foreground`/`bar.urgent` — resolve what's available to
a service-instantiated window; if bar palette is unreachable, use
`Quickshell`-provided theme or fall back to a translucent dark pill with light text):

```
[ you-dot ]  [ state label / transcript line ]  [ omarvis-dot ]  [ tool chip? ]
```

- **you-dot**: circle whose scale/glow tracks `inLevel` (Behavior on scale, ~120 ms
  OutQuad — this smooths the 4 Hz updates). Dim when level ~0. During dictation it
  tracks `dictationLevel` instead.
- **omarvis-dot**: while `thinking` → self-animating (slow rotation/opacity swirl,
  `SequentialAnimation`); while `speaking` → scale tracks `outLevel`; while `speaking`
  with `outLevel < 0.02` for >1.5 s (Timer) → render as listening. Pause all HUD
  animations when window not visible (CPU hygiene — HeyClicky shipped a fix for
  exactly this).
- **text line** (single line, elided): most recent of — `lastError` (red, sticky until
  next state change) → `streamingAgent` (while streaming) → `lastAgent` → "You: " +
  `lastUser` → state label ("Listening…", "Thinking…", "Connecting…",
  "● Dictating…", "Transcribing…"). Ask-mode sessions prefix "Ask · ".
- **tool chip**: visible while `runningCommand` non-empty — spinner + command (elided,
  first ~40 chars); on `ran`, flash a checkmark ~1 s then hide (Timer).
- **error styling**: pill border/glyph in urgent color; do not auto-hide while
  `sessionState === "error"`.

Register nothing new in `manifest.json` unless omarchy-shell requires windows to be
declared; `Service.qml` has `keepLoaded: true` and runs inside the shell's QML engine,
so a child `PanelWindow` should work — **verify against omarchy-shell's plugin docs or
another plugin that opens a window; if services may not create windows, add the
appropriate manifest kind instead.** This is the one open integration risk; resolve it
first (task ordering below).

### 5.3 `BarWidget.qml`

- Distinct glyph/color per state including new `thinking` (e.g. hourglass/ellipsis
  glyph) and `runningCommand` (gear glyph while non-empty).
- Error keeps `bar.urgent`. No other behavior changes; the widget remains the anchor
  and click target.

## 6. Tests

Follow the existing patterns: pure-Python pytest for logic; string-assertion tests over
QML files (see `tests/test_service.py`) for UI wiring.

New/updated tests:

1. `tests/test_levels.py` — `rms_level` on silence, full-scale square wave, half-scale
   sine (≈0.35); `LevelThrottle` emits at most 1 per 100 ms, always emits the
   transition to zero, forced final zero emit.
2. `tests/test_daemon.py` (extend) — `running` event emitted before `ran` with the same
   command string (both sync and background paths); `agent_part` events emitted and
   `thinking` state on user transcript; final `level 0/0` on teardown (drive
   `run_session` collaborators directly as existing tests do — do not hit the network).
3. `tests/test_simulate.py` — with delay-scale 0, the simulate timeline: is valid JSON
   per line; contains every event type in Section 3; starts with `state: starting` and
   ends with `state: idle`; error event only present with `OMARVIS_SIMULATE_ERROR=1`.
4. `tests/test_dictate.py` (extend) — recording emits throttled `level` fields;
   transcription/injection behavior unchanged.
5. `tests/test_sounds.py` — `play` is a no-op when disabled; builds the right pw-play
   command; missing binary/file does not raise. `bin/omarvis-make-sounds` output files
   exist in `assets/sounds/` and are <100 KB total.
6. `tests/test_service.py` / new `tests/test_hud.py` — string asserts: Service.qml
   handles `level`, `agent_part`, `running`; zeroes levels in `onExited`. HudWindow.qml
   binds `inLevel`/`outLevel`, hides when both states idle, contains the
   speaking→listening fallback Timer, animations gated on visibility.

## 7. End-to-end verification (agent-runnable)

Run all of these; all must pass before handing back:

1. `~/.local/share/omarvis/venv/bin/python -m pytest tests/ -q` — green.
2. `omarchy plugin validate .` — passes.
3. `bin/omarvis-run --simulate | head -50` — well-formed event stream in real time;
   exits 0.
4. **Visual harness** (no live shell needed): `tests/harness/HudHarness.qml` — a
   standalone quickshell config that instantiates Service.qml + HudWindow.qml with the
   daemon command overridden to the simulate stream (simplest: harness sets
   `OMARVIS_SIMULATE=1` in its environment before launching quickshell). Run
   `quickshell -p tests/harness/HudHarness.qml`, let the timeline play, and capture
   screenshots (`grim` or the shell screenshot tool) at listening / thinking /
   speaking / tool-running / error moments. Check each state is visually distinct and
   the pill hides afterward. Save screenshots to the scratchpad and include paths in
   the handback report.
5. Earcons: `pw-play assets/sounds/mic-open.wav` etc. exit 0 (audibility can't be
   asserted; file playability can).
6. Full-stack smoke (uses real API key + billing, keep it short):
   `bin/omarvis-run --text-only --message "what workspace am I on"` — confirm the
   event stream now includes `thinking` and `agent_part` events alongside the existing
   ones, and the session ends cleanly. Skip only if no API key is configured, and say
   so in the report.

Do NOT restart or relink the user's live omarchy-shell session; the harness covers
visual verification. Leave live-shell testing to the human.

## 8. Human verification checklist (after agent handback)

- [ ] Reload shell / re-link plugin; start Agent session (`SUPER+CTRL+J`): mic-open
      chirp plays, HUD appears, you-dot moves while speaking, thinking swirl during the
      gap, agent text streams into the pill, omarvis-dot pulses during TTS.
- [ ] Ask a command ("switch to workspace 2"): tool chip spinner → checkmark.
- [ ] Ask mode (`SUPER+SHIFT+J`): "Ask ·" prefix visible.
- [ ] Hold `SUPER+J` and dictate: HUD shows waveform-reactive dot + "Dictating…", then
      "Transcribing…", then hides; text lands in the focused window.
- [ ] Unplug/mute mic mid-session: you-dot goes flat while you speak (the failure is
      visible), no crash.
- [ ] Kill the daemon (`pkill -f omarvis.daemon`): HUD clears, no stale level/state.
- [ ] `"ui": {"earcons": false}` silences chirps; `hud_position` moves the pill.

## 9. Suggested task order

1. `levels.py` + tests (pure logic, no integration risk).
2. Simulate mode + tests (unblocks all UI work).
3. Resolve the one open integration question: can a service plugin create a
   `PanelWindow`? (Read omarchy-shell plugin docs/source or a precedent plugin.)
4. Daemon changes (metered interface, agent_part, running, thinking, teardown zeroing)
   + tests.
5. Dictation levels + tests.
6. HudWindow + Service.qml wiring + harness + screenshots.
7. BarWidget polish, earcons, config keys, README section ("Session HUD") update.
8. Full verification pass (Section 7).
