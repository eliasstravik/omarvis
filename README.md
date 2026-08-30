# Omarvis

Omarvis is an Omarchy shell plugin that starts an ElevenLabs voice session from a hotkey or bar widget. It can run allowlisted Omarchy and Hyprland commands, control Herdr, and drive Chromium through `agent-browser`.

The plugin exposes one client tool named `run`. Python policy code parses every command with `shlex`, checks the route and flags, and executes the resulting argument list with `shell=False`. The model cannot run a shell command outside that policy.

## Requirements

- Omarchy with the current plugin-based `omarchy-shell`
- An ElevenLabs account and API key
- Chromium 146 or newer for control of your existing browser profile
- Herdr for terminal and coding-agent control

The setup script installs PortAudio, PyAudio, the ElevenLabs Python SDK, and agent-browser 0.34. It creates runtime files under `~/.config/omarchy/omarvis/` and `~/.local/share/omarvis/`. It does not edit this plugin checkout.

## Install

Install and enable the plugin on the Omarchy machine:

```bash
omarchy plugin add https://github.com/eliasstravik/Omarvis.git --enable --yes
~/.config/omarchy/plugins/omarvis.voice/bin/omarvis-setup
```

You can provide the API key in `ELEVENLABS_API_KEY`. If it is absent, setup asks for it and writes `~/.config/omarchy/omarvis/api_key` with mode 600.

Setup prints this optional Hyprland binding and can append it after making a timestamped backup:

```lua
o.bind("SUPER + CTRL + J", "Omarvis", "omarchy-shell omarvis toggle")
```

Press `SUPER + CTRL + J` or click the bar microphone to start or stop one session. Sessions also end when you ask Omarvis to stop, after about 30 seconds of silence, or after the five-minute limit.

## What it can do

Omarvis can use documented, non-hidden `omarchy` routes and a curated set of Hyprland dispatchers. Common examples include switching workspaces, launching or focusing apps, closing the focused window, changing themes, adjusting volume and brightness, taking screenshots, setting reminders, and locking the screen.

Herdr support includes reading agent and workspace state, focusing agents and panes, submitting prompts, splitting and moving panes, sending plain keys, and showing notifications. Running arbitrary terminal commands, sending control-key combinations, or closing Herdr resources requires confirmation.

Browser support includes navigation, tab management, snapshots, clicking, filling fields, keyboard input, page titles, screenshots, downloads, and uploads. Omarvis opens its own tab for the first navigation. It only changes to one of your other tabs when you ask it to switch.

Omarvis does not provide a wake word, always-on listening, dictation, general shell access, arbitrary JavaScript evaluation, tmux control, or keystroke injection into ordinary desktop windows. Multi-page checkout and form workflows are outside v1.

## Confirmation

Commands that shut down or leave the session, install or remove software, activate plugin code, change the bar layout, run terminal commands, close Herdr resources, upload or download files, or close the browser attachment require a spoken confirmation.

The daemon records the exact parsed argument list, waits for a later user transcript, and accepts the confirmation for 30 seconds. A first tool call with `confirmed: true`, a changed command, an expired request, or a response generated before the user speaks cannot bypass this check.

## Browser setup

To control your existing Chromium profile, open `chrome://inspect/#remote-debugging` and enable remote debugging. Chromium shows an Allow prompt on the first attach and displays an automation banner while attached. If the prompt is still open, Omarvis asks you to click Allow and try again. It does not silently switch to another browser.

Existing-profile mode can access any logged-in tab you ask Omarvis to use, including its cookies and account session. Omarvis pins its browser session to one tab so a closed tab produces a `tab_gone` error instead of falling through to another page.

If Chromium is older than 146 or existing-profile access is unavailable during setup, Omarvis uses a separate headed browser profile under `~/.local/share/omarvis/browser-profile`. That profile has separate logins.

## Privacy

During a session, your microphone audio, the Omarchy, Herdr, and browser command lists, your workspace number, the class and title of your open windows, your Herdr workspace and agent names and their working-directory paths, and your open browser tab titles and hosts are sent to ElevenLabs. Any page snapshot or text Omarvis requests is also sent to ElevenLabs. Nothing is sent while Omarvis is idle.

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

If the browser says approval is pending, click Allow in Chromium and repeat the request. If attachment is rejected, check the remote-debugging toggle and Chromium version.

Removing the Omarvis widget from the bar removes the plugin's only entry from `shell.json`. Omarchy then unmounts the service, so the hotkey stops working. Restore it with:

```bash
omarchy plugin enable omarvis.voice
```

The QML, microphone, and full desktop flows require an Omarchy machine. Run `omarchy plugin validate .`, the setup self-check, and the end-to-end checklist there before treating a release as verified.
