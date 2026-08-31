# Omarvis

Omarvis is an Omarchy shell plugin for voice questions, policy-guarded desktop control, hold-to-talk dictation, and typed commands. It can run allowlisted Omarchy and Hyprland commands, control Herdr, and drive Chromium through `agent-browser`.

The plugin exposes one client tool named `run`. Python policy code parses every command with `shlex`, checks the route and flags, and executes the resulting argument list with `shell=False`. The model cannot run a shell command outside that policy.

## Requirements

- Omarchy with the current plugin-based `omarchy-shell`
- An ElevenLabs account and API key
- Chromium (or Chrome) with an existing profile for browser control with your logins
- Herdr for terminal and coding-agent control
- `wtype` for direct Wayland dictation input

The setup script installs PortAudio, PyAudio, the ElevenLabs Python SDK, and agent-browser 0.34. It creates runtime files under `~/.config/omarchy/omarvis/` and `~/.local/share/omarvis/`. It does not edit this plugin checkout.

## Install

Install and enable the plugin on the Omarchy machine:

```bash
omarchy plugin add https://github.com/eliasstravik/Omarvis.git --enable --yes
~/.config/omarchy/plugins/omarvis.voice/bin/omarvis-setup
```

You can provide the API key in `ELEVENLABS_API_KEY`. If it is absent, setup asks for it and writes `~/.config/omarchy/omarvis/api_key` with mode 600.

Setup prints the J-key family and can append any missing bindings after making a timestamped backup:

```lua
o.bind("SUPER + CTRL + J", "Omarvis", "omarchy-shell omarvis toggle")
o.bind("SUPER + SHIFT + J", "Omarvis Ask", "omarchy-shell omarvis toggleMode ask")
hl.unbind("SUPER + J") -- replaces Omarchy's Toggle window split binding where present
o.bind("SUPER + J", "Omarvis Dictate", "omarchy-shell omarvis dictate start")
o.bind("SUPER + J", "Omarvis Dictate Stop", "omarchy-shell omarvis dictate stop", { release = true })
o.bind("SUPER + ALT + J", "Omarvis Text", "<plugin-dir>/bin/omarvis-text")
```

`SUPER + CTRL + J` starts the full Agent scope. `SUPER + SHIFT + J` starts the strictly read-only Ask scope. Hold `SUPER + J` to dictate your own words into the focused window, or use `SUPER + ALT + J` for a typed one-shot Agent instruction. Clicking the bar microphone toggles Agent mode.

## Session HUD

While a voice session or dictation is active, Omarvis shows a passive pill at the top of the screen. The left dot follows microphone level, the right dot animates while the agent thinks and follows playback level while it speaks, and validated tool execution gets a separate spinner/check indicator. The HUD disappears when both session and dictation are idle; errors remain visible until the next state change. Short earcons mark microphone open, microphone close, and failures.

The defaults can be overridden in `~/.config/omarchy/omarvis/config.json`:

```json
{
  "ui": {
    "earcons": true,
    "hud_position": "top-center"
  }
}
```

`hud_position` accepts `top-center` or `top-right`. Set `earcons` to `false` to silence all three cues. For development, `bin/omarvis-run --simulate` emits the complete HUD event timeline without an API key, audio hardware, network access, or ElevenLabs usage.

## What it can do

Omarvis can use documented, non-hidden `omarchy` routes and a curated set of Hyprland dispatchers. Common examples include switching workspaces, launching or focusing apps, closing the focused window, changing themes, adjusting volume and brightness, taking screenshots, setting reminders, and locking the screen.

Herdr support includes reading agent and workspace state, focusing agents and panes, submitting prompts, splitting and moving panes, sending plain keys, and showing notifications. Running arbitrary terminal commands, sending control-key combinations, or closing Herdr resources requires confirmation.

Browser support includes navigation, tab management, snapshots, clicking, filling fields, keyboard input, page titles, screenshots, downloads, and uploads. In `own-browser` mode Omarvis opens its own tab for the first navigation. Its isolated browser modes suppress Chromium's automatic startup tab, open one controlled tab, and create another only when you explicitly ask. It only changes to one of your other tabs when you ask it to switch.

Ask mode is enforced in Python policy: dispatchers, navigation, clicks, launches, and every other mutation are refused even if the model requests one. `omarvis see` is intercepted in-process, captures the current desktop, uploads it into the active ElevenLabs conversation, and follows the tool result with a native multimodal image turn. The old local OCR route is policy-blocked.

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

Test a text-only session without microphone billing or PyAudio:

```bash
bin/omarvis-run --text-only --message "switch to workspace three"
```

Print the generated prompt context:

```bash
python -m omarvis.catalog --print
```

In `own-browser` mode, if the browser says approval is pending, click Allow in Chromium and repeat the request; if attachment is rejected, check the remote-debugging toggle and Chromium version. In `real-profile` mode, log in to sites in your normal browser first — the snapshot picks up whatever your real profile is logged into at daemon start.

Removing the Omarvis widget from the bar removes the plugin's only entry from `shell.json`. Omarchy then unmounts the service, so the hotkey stops working. Restore it with:

```bash
omarchy plugin enable omarvis.voice
```

The QML, microphone, and full desktop flows require an Omarchy machine. Run `omarchy plugin validate .`, the setup self-check, and the end-to-end checklist there before treating a release as verified.
