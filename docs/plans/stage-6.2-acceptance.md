# Stage 6.2 — client acceptance checklist

Run this on the Omarchy desktop after the changed checkout is installed as the
active `io.github.eliasstravik.omarvis` plugin and `omarchy-shell` is running.

Client direction after Stage 6.2 removed Omarvis-owned last-dictation display
and state. Successful transcripts still copy to Wayland, but Omarchy's
clipboard manager is now the sole dictation-history interface.

## 1. Confirm the service is using the changed checkout

```bash
omarchy-shell omarvis status | jq '{dictationState}'
```

Expected: valid JSON with `"dictationState": "idle"` before testing.

## 2. Normal dictation

1. Focus a disposable text field.
2. Hold `SUPER+J` for longer than one second, say a unique phrase, and release.
3. Open Omarchy's clipboard manager and locate the same phrase.

Then run:

```bash
printf 'clipboard: '; wl-paste --no-newline; printf '\n'
omarchy-shell omarvis status | jq '{dictationState}'
```

Expected:

- The phrase was typed into the focused field.
- The clipboard command prints the same phrase.
- The phrase appears in the clipboard manager history.
- `dictationState` is `idle`.

## 3. Force `wtype` to fail after a good transcript

First verify the live dictation daemon searches `/usr/local/bin` before
`/usr/bin`:

```bash
python - <<'PY'
import pathlib
import subprocess

pid = subprocess.check_output(
    ["pgrep", "-n", "-f", "/bin/omarvis-dictate"], text=True
).strip()
raw = pathlib.Path(f"/proc/{pid}/environ").read_bytes()
env = dict(
    field.split(b"=", 1) for field in raw.split(b"\0") if b"=" in field
)
paths = env[b"PATH"].decode().split(":")
assert paths.index("/usr/local/bin") < paths.index("/usr/bin"), paths
print("PASS: /usr/local/bin shadows /usr/bin for omarvis-dictate")
PY
```

Expected: the single `PASS` line. If the assertion fails, stop and return the
printed path rather than replacing any binary.

Create a temporary shadow command. The first `test` deliberately aborts if a
real local override already exists:

```bash
test ! -e /usr/local/bin/wtype
printf '#!/bin/sh\nexit 1\n' >/tmp/omarvis-wtype-fail
sudo install -m 755 /tmp/omarvis-wtype-fail /usr/local/bin/wtype
```

Now focus a disposable text field, dictate a new unique phrase, and release.
Nothing should be typed. Immediately remove the shadow command:

```bash
sudo rm /usr/local/bin/wtype
rm /tmp/omarvis-wtype-fail
printf 'clipboard: '; wl-paste --no-newline; printf '\n'
omarchy-shell omarvis status | jq '{dictationState}'
```

Expected:

- Nothing was typed into the focused field.
- The clipboard command prints the failed-injection phrase, and the phrase is
  present in clipboard history.
- One error earcon plays.
- A notification shows the `wtype` failure and says the dictation was copied
  to the clipboard.
- `dictationState` is `idle` and the HUD is gone.

If the test is interrupted, run the two removal commands before trying normal
dictation again.

## 4. No speech

Hold `SUPER+J` for longer than one second without speaking, then release.

```bash
omarchy-shell omarvis status | jq '{dictationState}'
```

Expected:

- Behavior matches the pre-change flow: no text is typed and the existing
  no-speech error notification/earcon appears.
- `dictationState` returns to `idle`; the HUD is not stuck.

Return the command output and whether each visual/audio bullet passed. Stage 1
does not begin until these results are accepted.
