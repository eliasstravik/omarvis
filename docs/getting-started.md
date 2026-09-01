# Talk to your Omarchy desktop in five minutes

By the end of this guide you press one hotkey, say "switch to workspace 2", and watch your desktop do it.

## Prerequisites

- Omarchy with the current plugin-based `omarchy-shell`
- An ElevenLabs account and API key
- Optional: Chromium (or Chrome) with an existing profile, for browser control with your logins
- Optional: Herdr, for terminal and coding-agent control
- Optional: `wtype`, for hold-to-talk dictation
- Optional: Tailscale on the computer and phone, for remote control

## 1. Install the plugin

On the Omarchy machine:

```bash
omarchy plugin add https://github.com/eliasstravik/omarvis.git --enable --yes
~/.config/omarchy/plugins/omarvis.voice/bin/omarvis-setup
```

Setup installs PortAudio, PyAudio, the ElevenLabs SDK, and agent-browser, and creates its runtime files under `~/.config/omarchy/omarvis/` and `~/.local/share/omarvis/`.

## 2. Paste your ElevenLabs key

If `ELEVENLABS_API_KEY` is not set in your environment, setup asks for the key and writes it to `~/.config/omarchy/omarvis/api_key` with mode 600. That is the only thing you type.

Setup then offers the J-key bindings and appends any missing ones after making a timestamped backup:

- `Super+Ctrl+J` starts or ends the voice call
- Hold `Super+J` to dictate into the focused window
- `Super+Ctrl+Alt+J` (or a left click on the bar microphone) opens the panel

## 3. Have your first conversation

Press `Super+Ctrl+J` and say:

> "Switch to workspace 2."

The workspace switches, and a small strip under the bar shows the session state: an hourglass while it works, a microphone when the floor is yours, a speaker while the agent talks. Press `Super+Ctrl+J` again to hang up. Sessions cap themselves at five minutes.

## Where to look when something happens

- Conversation text lives in the bar tooltip.
- Dictation history lives in Omarchy's clipboard manager.
- Failures go to desktop notifications.
- `omarchy-shell omarvis status` prints the service state, and `bin/omarvis-run` in a terminal streams the daemon's JSON events.

## Next steps

- Try a riskier ask, like installing a package. Omarvis reads the exact command back and waits for your spoken yes.
- Ask about something on your screen. Omarvis uploads one screenshot and answers out loud.
- Turn on Remote access in the panel and scan the QR with a phone on your tailnet. Read the [remote control security notes](reference.md#remote-control-from-your-phone) first.
- The full [reference](reference.md) covers the panel, browser modes, microphone selection, privacy, and troubleshooting.

Stuck? [Open an issue](https://github.com/eliasstravik/omarvis/issues/new).
