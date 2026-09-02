# Omarvis reference

Omarvis is an Omarchy shell plugin for voice questions, policy-guarded desktop control, hold-to-talk dictation, and typed commands. It can run allowlisted Omarchy and Hyprland commands, control Herdr, and drive Chromium through `agent-browser`.

The plugin exposes one client tool named `run`. Python policy code parses every command with `shlex`, checks the route and flags, and executes the resulting argument list with `shell=False`. The model cannot run a shell command outside that policy.

## Requirements

- Omarchy with the current plugin-based `omarchy-shell`
- An ElevenLabs account and API key
- Chromium (or Chrome) with an existing profile for browser control with your logins
- Herdr for terminal and coding-agent control
- `wtype` for direct Wayland dictation input
- Tailscale on the computer and phone for optional remote control

The setup script installs PortAudio, PyAudio, the ElevenLabs Python SDK, QR matrix support, and agent-browser 0.34.0. It creates runtime files under `~/.config/omarchy/omarvis/` and `~/.local/share/omarvis/`. It does not edit this plugin checkout.

### Reproducible install

Every artifact setup fetches is pinned in this repository, so the plugin commit under review fully determines the code that runs:

- **Python runtime** — `requirements.lock` is a complete transitive lock (pip-compile, every package with sha256 wheel hashes). Setup installs it into `~/.local/share/omarvis/venv` with `pip install --require-hashes --ignore-installed`, which refuses any artifact whose hash is not in the lock. Top-level pins live in `requirements.in`.
- **agent-browser** — `npm/package.json` plus `npm/package-lock.json` pin agent-browser 0.34.0 exactly, with its registry integrity hash. Setup runs `npm ci` from that lockfile into `~/.local/share/omarvis/agent-browser/` and uses only that copy; nothing is installed globally and a global agent-browser, if present, is never used or modified.
- **ElevenLabs browser client** — the phone page's SDK (`@elevenlabs/client` 1.23.0 `dist/lib.iife.js`) is downloaded from the npm registry and verified against the sha256 hardcoded in `bin/omarvis-setup` before it is installed; on mismatch nothing is installed.
- **System packages** — `portaudio`, `python-pyaudio`, and optionally `wtype` come from the distribution's signed repositories via `omarchy pkg add`; their versions follow the distribution, not this plugin.

## Install

Install and enable the plugin on the Omarchy machine:

```bash
omarchy plugin add https://github.com/eliasstravik/Omarvis.git --enable --yes
~/.config/omarchy/plugins/io.github.eliasstravik.omarvis/bin/omarvis-setup
```

You can provide the API key in `ELEVENLABS_API_KEY`. If it is absent, setup asks for it and writes `~/.config/omarchy/omarvis/api_key` with mode 600.

Setup prints the J-key family and can append any missing bindings after making a timestamped backup:

```lua
o.bind("SUPER + CTRL + J", "Omarvis", "omarchy-shell omarvis toggle")
hl.unbind("SUPER + J") -- replaces Omarchy's Toggle window split binding where present
o.bind("SUPER + J", "Omarvis Dictate", "omarchy-shell omarvis dictate start")
hl.bind("SUPER + J", hl.dsp.submap("omarvis-dictate"))
o.bind("SUPER + J", "Omarvis Dictate Stop", "omarchy-shell omarvis dictate stop", { release = true, submap_universal = true })
hl.bind("SUPER + J", hl.dsp.submap("reset"), { release = true, submap_universal = true })
hl.define_submap("omarvis-dictate", function()
  o.bind("SUPER + SPACE", "Omarvis Hands-free", "omarchy-shell omarvis dictate handsfree")
  o.bind("ESCAPE", "Omarvis Cancel", "omarchy-shell omarvis dictate cancel")
  hl.bind("ESCAPE", hl.dsp.submap("reset"))
end)
o.bind("SUPER + ALT + J", "Omarvis Panel", "omarchy-shell omarvis panel")
o.bind("SUPER + SHIFT + J", "Omarvis Remote", "omarchy-shell omarvis toggleRemote")
```

`SUPER + CTRL + J` starts or ends the two-way voice call — the only kind of session Omarvis has. Hold `SUPER + J` to dictate your own words into the focused window; release to type them. While holding, press `SPACE` to go hands-free: the recording stays open after you let go, a fresh `SUPER + J` tap types it, and `ESCAPE` discards it. Holding `SUPER + J` puts Hyprland into a small submap, so the chord never reaches the global `SUPER + SPACE` menu binding, and the submap ends with the release. Left-click the bar microphone, or press `SUPER + ALT + J`, to open the Omarvis panel. Right-click is intentionally inert. Setup deletes any `SUPER + SHIFT + J` Ask binding or `SUPER + ALT + J` text binding an older install left behind, and rewrites a pre-submap dictation pair.

## Panel

The native Omarchy panel is deliberately small: a hero with the state glyph and a one-word status, one Start/End button, and the REMOTE section. There is no mode choice, no transcript, and no scrolling. Conversation text lives in the bar tooltip, dictation history lives in Omarchy's clipboard manager, and anything that needs real reading — a failed voice session, a remote service that cannot serve — goes to the notification server. The REMOTE section shows the pairing QR while remote access is on and no phone is connected; when a phone joins, the QR is replaced by a PHONE CONNECTED row and an end-session button. The copy glyph beside the QR puts the pairing URL on the clipboard for the cases a camera cannot reach.

The plugin id is `io.github.eliasstravik.omarvis`. Installs from before 0.2.0 used the id `omarvis.voice`; upgrading across that rename means removing the old plugin and adding this one again, after which the bar widget must be re-added to `shell.json`. Opening another stock panel closes Omarvis through Omarchy's normal one-popup coordinator.

## Remote control from your phone

Remote access uses a path-scoped `tailscale serve` mount at `/omarvis`; Omarvis never enables Funnel or changes Tailscale login/network settings. Turn it on in the panel, then scan the QR from a phone signed into the same tailnet account. The authenticated phone page matches the active Omarchy theme and wallpaper. Tap **Talk** to start a phone WebRTC conversation; this ends any live computer conversation, while starting again on the computer ends the phone session.

**Whoever holds this URL, or photographs the QR, can install software and run terminal commands on this machine.** The remote transcript stream cannot provide a physical proof of who spoke, so confirmation-gated actions—including `herdr pane run`, package or plugin installation, power actions, deletes, browser uploads, and browser downloads—remain reachable to the authenticated remote caller.

The URL is available only inside the tailnet, never through Funnel or the public internet. Exposure therefore includes other enrolled devices, machines shared into the tailnet, processes running on any of them, and anyone who sees the QR. On ordinary user-owned Tailscale nodes, Omarvis also requires the identity header supplied by Tailscale Serve to match the computer's tailnet login. This narrows access but does not make the URL safe to share.

The pairing URL is stable and deliberately remains in the phone browser's address bar. The credential may enter browser history and, on iOS or Chrome, may sync to a cloud account and other devices. It is stored in `~/.local/share/omarvis/web-secret` with mode 600 and does not rotate when Remote access is toggled off. Delete that file while Remote access is off to revoke it and generate a new QR on the next enable. Clicking the URL row copies the full credential into Omarchy's persistent, browsable clipboard history—the same history used for dictations—so scanning the QR is preferred.

Remote access persists across reboots and re-arms at login. A stale Tailscale mount after a hard kill or power loss normally points at nothing, but it could proxy to an unrelated process if that process later binds Omarvis's port. Omarvis removes its own stale mount at shell startup and never removes other Serve configuration.

Every helper Omarvis runs (agent commands, `hyprctl`, `herdr`, `tailscale`, screenshots) starts in its own process group with output streamed through fixed caps: a command that floods stdout is killed, a command that times out has its whole group terminated, and whatever is still running when a session ends or is taken over is terminated with it. Commands that intentionally keep running, like `omarchy launch`, are reported as started after their window; their later output is drained and discarded, and Omarvis tracks only a handful of them at once. Programs are resolved along `PATH` but only from directories whose whole chain is root- or user-owned and not writable by others, and the resolved file is re-checked at exec time. Output past a command's limit is never parsed as a whole, and the command-catalog caches under `~/.cache/omarvis` are rewritten atomically and regenerated when empty or corrupt. The config, API key, and phone secret are opened through their directory descriptor without following symlinks and must be plain, user-owned, single-link files under a size cap; Omarvis refuses to start rather than read anything else, and rewrites them by renaming a fresh 0600 file into place.

The backend listens only on `127.0.0.1`. Change its port with `"web_port": 4763` in `~/.config/omarchy/omarvis/config.json` if the default is occupied. `bin/omarvis-web --simulate` provides a fake URL, real QR matrix, theme/page rendering, a no-op policy executor, and a short SSE timeline without touching Tailscale. Simulation does not mint an ElevenLabs token and does not simulate phone audio, browser client tools, transcripts, or screenshots.

Phone conversation audio flows directly between the phone and ElevenLabs over WebRTC; the API key remains on the computer. The computer receives only final transcript text and relayed tool calls. Remote sessions always use Agent scope. They can drive `real-profile` browser windows containing snapshots of your live logins, and an explicit remote `omarvis see` uploads a current desktop screenshot to ElevenLabs. Sessions are capped at five minutes, and a locked or suspended phone normally loses its lifeline and is ended by the computer after about 15 seconds.

## Session HUD

While a voice session or dictation is active, Omarvis shows a text-free strip under the bar: one state glyph and one amplitude meter. The vocabulary is three glyphs — an hourglass for any waiting or busywork (connecting, transcribing, thinking, running a command), a microphone when the floor is yours, and a speaker while the agent talks — plus an alert mark for failures, which are otherwise routed to desktop notifications. A finished call simply disappears. The strip disappears when both session and dictation are idle. Omarvis plays no sounds.

The defaults can be overridden in `~/.config/omarchy/omarvis/config.json`:

```json
{
  "ui": {
    "hud_position": "top-center"
  }
}
```

`hud_position` accepts `top-center` or `top-right`. For development, `bin/omarvis-run --simulate` emits the complete HUD event timeline without an API key, audio hardware, network access, or ElevenLabs usage.

## What it can do

Omarvis can use documented, non-hidden `omarchy` routes and a curated set of Hyprland dispatchers. Common examples include switching workspaces, launching or focusing apps, closing the focused window, changing themes, adjusting volume and brightness, taking screenshots, setting reminders, and locking the screen.

Herdr support includes reading agent and workspace state, focusing agents and panes, submitting prompts, splitting and moving panes, sending plain keys, and showing notifications. Running arbitrary terminal commands, sending control-key combinations, or closing Herdr resources requires confirmation.

Browser support includes navigation, tab management, snapshots, clicking, filling fields, keyboard input, page titles, screenshots, downloads, and uploads. In `own-browser` mode Omarvis opens its own tab for the first navigation. Its isolated browser modes suppress Chromium's automatic startup tab, open one controlled tab, and create another only when you explicitly ask. It only changes to one of your other tabs when you ask it to switch.

`omarvis see` is intercepted in-process, captures the current desktop, uploads it into the active ElevenLabs conversation, and follows the tool result with a native multimodal image turn. The old local OCR route is policy-blocked.

Omarvis does not provide a wake word, always-on listening, general shell access, arbitrary JavaScript evaluation, tmux control, or agent-selected keystroke injection into ordinary desktop windows. Dictation is the sole injection path and types only the user's direct Scribe transcript.

## Confirmation

Commands that shut down or leave the session, install or remove software, activate plugin code, change the bar layout, run terminal commands, close Herdr resources, upload or download files, or close the browser attachment require a spoken confirmation.

The daemon records the exact parsed argument list, waits for a later user transcript, and accepts the confirmation for 30 seconds. A first tool call with `confirmed: true`, a changed command, an expired request, or a response generated before the user speaks cannot bypass this check.

After one confirmed non-permanent-risk command, Omarvis may offer to stop asking for that category for the rest of the session. Category approvals disappear when the session ends. Deletes, closes, session/server stops, and system power actions always require fresh confirmation.

## Memory and screenshots

Setup seeds `~/.config/omarchy/omarvis/profile.md`; up to 2,000 characters are supplied to both agents. Both agents have ElevenLabs file input enabled. Screenshots used by `omarvis see` are uploaded only on an explicit screen-content question and deleted from the local cache immediately after upload. Omarvis has no Anthropic integration or vision API key.

## Browser setup

Omarvis picks a browser mode during setup and records it as `browser_mode` in `~/.config/omarchy/omarvis/config.json`:

- `real-profile` (default when a system Chromium/Chrome profile exists): Omarvis opens a separate headed browser window from a snapshot copy of your real profile, made fresh each time the agent-browser daemon starts. Your logins and cookies are available immediately, no remote-debugging consent prompt ever appears, and nothing Omarvis does is written back to your real profile. Set `browser_profile` to a profile name (default `Default`) to snapshot a different profile.
- `omarvis-browser`: a separate persistent headed profile under `~/.local/share/omarvis/browser-profile` with its own logins. Used when no system browser profile is found; set `browser_profile` to a directory path to relocate it.
- `own-browser` (manual opt-in): attaches to your running Chromium through `chrome://inspect/#remote-debugging`, which must be enabled there first. This drives your real live browser, but Chromium 146+ shows an Allow prompt on every attach by design — there is no way to suppress it — plus an automation banner while attached. If the prompt is still open, Omarvis asks you to click Allow and try again.

In `real-profile` and `own-browser` modes Omarvis can use your logged-in account sessions, so treat its browser actions accordingly. Omarvis pins its browser session to one tab so a closed tab produces a `tab_gone` error instead of falling through to another page.

## Privacy

During a session, your microphone audio, profile memory, the Omarchy, Herdr, and browser command lists, your workspace number, the class and title of your open windows, your Herdr workspace and agent names and their working-directory paths, and your open browser tab titles and hosts are sent to ElevenLabs. Any page snapshot or text Omarvis requests is also sent to ElevenLabs. A current desktop screenshot is uploaded to ElevenLabs only when `omarvis see` is invoked, and ElevenLabs bills each uploaded file. Dictation audio is sent to ElevenLabs Scribe on release. Nothing is sent while Omarvis is idle or while the dictation daemon is waiting.

ElevenLabs bills Agents sessions by conversation minute. LLM usage may be billed separately as pass-through usage. Omarvis starts sessions only when you press the hotkey or click the widget and caps each session at five minutes.

## Microphone selection

List input devices:

```bash
bin/omarvis-run --list-devices
```

Set the chosen numeric index as `input_device_index` in `~/.config/omarchy/omarvis/config.json`, then start a new session.

## Troubleshooting

Check the service state:

```bash
omarchy-shell omarvis status
```

Run the daemon in a terminal to see its JSON event stream and errors:

```bash
bin/omarvis-run
```

Print the generated prompt context:

```bash
python -m omarvis.catalog --print
```

In `own-browser` mode, if the browser says approval is pending, click Allow in Chromium and repeat the request; if attachment is rejected, check the remote-debugging toggle and Chromium version. In `real-profile` mode, log in to sites in your normal browser first — the snapshot picks up whatever your real profile is logged into at daemon start.

Removing the Omarvis widget from the bar removes the plugin's only entry from `shell.json`. Omarchy then unmounts the service, so the hotkey stops working. Restore it with:

```bash
omarchy plugin enable io.github.eliasstravik.omarvis
```

The QML, microphone, and full desktop flows require an Omarchy machine. Run `omarchy plugin validate .`, the setup self-check, and the end-to-end checklist there before treating a release as verified.
