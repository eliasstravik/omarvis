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
~/.config/omarchy/plugins/io.github.eliasstravik.omarvis/bin/omarvis-setup
```

Setup asks all its questions first, then runs unattended: it installs PortAudio, PyAudio, the ElevenLabs SDK, and the agent-browser native binary (from its pinned, digest-verified tarball), and creates its runtime files under `~/.config/omarchy/omarvis/` and `~/.local/share/omarvis/`. The terminal shows one line per phase; full subprocess output goes to `~/.local/share/omarvis/setup.log`. For automation, `omarvis-setup --yes` answers every prompt with yes (it then requires `ELEVENLABS_API_KEY` in the environment on a first run).

## 2. Paste your ElevenLabs key

If no key is stored and `ELEVENLABS_API_KEY` is not set, setup explains how to create one and prompts for it:

1. Sign in at [elevenlabs.io](https://elevenlabs.io) — any plan works; agent calls and dictation consume credits from your plan's quota.
2. Click your profile (bottom-left) → **API Keys** → **Create API Key**.
3. Leave it unrestricted, or restrict it to **Agents Platform / Conversational AI** (the voice agent) and **Speech to Text** (dictation). A monthly credit cap on the key is a good idea.
4. Copy the key right away — ElevenLabs shows it only once.

Setup validates the key against the ElevenLabs API at the prompt — a mistyped key, or one scoped without agents access, fails immediately with instructions instead of surfacing minutes later during provisioning. The key is stored in `~/.config/omarchy/omarvis/api_key` with mode 600 and sent only to `api.elevenlabs.io`. That is the only thing you type.

Setup then offers the J-key bindings and appends any missing ones after making a timestamped backup:

- `Super+Ctrl+J` starts or ends the voice call
- Hold `Super+J` to dictate into the focused window; press `Space` while holding to go hands-free, then tap `Super+J` to type it or `Escape` to discard
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

## Uninstall

Remove the plugin and everything it created:

```bash
omarchy plugin remove io.github.eliasstravik.omarvis
rm -rf ~/.config/omarchy/omarvis ~/.local/share/omarvis
```

If you enabled Remote access at some point, also clear the Tailscale mount:

```bash
tailscale serve --set-path /omarvis off
```

Setup only ever appended the J-key bindings to `~/.config/hypr/bindings.lua` after asking you and making a timestamped backup. Delete the `omarvis` lines there (or restore the backup) to finish.

Stuck? [Open an issue](https://github.com/eliasstravik/omarvis/issues/new).
