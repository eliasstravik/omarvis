import QtQuick
import Quickshell
import Quickshell.Io

Item {
  id: root

  property var shell: null
  property string sessionState: "idle"
  property string lastUser: ""
  property string lastAgent: ""
  property string lastError: ""
  property string dictationState: "idle"
  property bool dictationLocked: false
  property real inLevel: 0.0
  property real outLevel: 0.0
  property string streamingAgent: ""
  property string runningCommand: ""
  property string lastCommand: ""
  property real dictationLevel: 0.0
  property bool panelOpen: false
  property bool remoteEnabled: false
  property string remoteState: "off"
  property string remoteError: ""
  property string remoteUrl: ""
  property var qrMatrix: []
  property bool phoneSessionActive: false
  property string phoneRunningCommand: ""
  property bool remoteStartupReady: false
  property bool remoteMarkerSeeded: false
  property bool webBindFailed: false
  property bool pendingRemoteAck: false
  property string hudPosition: "top-center"
  property var daemonCommand: []
  property bool dictationDaemonEnabled: true
  property bool stopRequested: false
  readonly property string pluginDir: String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "").replace(/\/$/, "")
  property alias hud: hudWindow
  signal commandRan(string command)
  signal panelRequested()

  function handleEvent(line) {
    var event
    try {
      event = JSON.parse(line)
    } catch (error) {
      console.warn("omarvis: invalid daemon event:", line)
      return
    }
    if (event.event === "state") {
      if (String(event.state || "idle") !== root.sessionState && event.state !== "error")
        root.lastError = ""
      root.sessionState = String(event.state || "idle")
    }
    else if (event.event === "level") {
      root.inLevel = Math.max(0, Math.min(1, Number(event.in || 0)))
      root.outLevel = Math.max(0, Math.min(1, Number(event.out || 0)))
    }
    else if (event.event === "user") {
      root.lastUser = String(event.text || "")
      root.streamingAgent = ""
    }
    else if (event.event === "agent_part")
      root.streamingAgent += String(event.text || "")
    else if (event.event === "agent") {
      root.lastAgent = String(event.text || "")
      root.streamingAgent = ""
    }
    else if (event.event === "running")
      root.runningCommand = String(event.command || "")
    else if (event.event === "ran") {
      root.runningCommand = ""
      root.lastCommand = String(event.command || "")
      root.commandRan(String(event.command || ""))
    }
    else if (event.event === "dictation") {
      root.applyDictationEvent(event)
    }
    else if (event.event === "error") {
      root.lastError = String(event.message || "Unknown error")
      root.sessionState = "error"
    }
  }

  function handleDictationEvent(line) {
    var event
    try {
      event = JSON.parse(line)
    } catch (error) {
      console.warn("omarvis dictate: invalid event:", line)
      return
    }
    if (event.event !== "dictation") return
    root.applyDictationEvent(event)
  }

  function applyDictationEvent(event) {
    var nextState = String(event.state || "idle")
    if (nextState !== root.dictationState && nextState !== "error") root.lastError = ""
    root.dictationState = nextState
    if (event.locked !== undefined) root.dictationLocked = !!event.locked
    if (nextState !== "recording") root.dictationLocked = false
    if (event.level !== undefined) root.dictationLevel = Math.max(0, Math.min(1, Number(event.level || 0)))
    else if (nextState !== "recording") root.dictationLevel = 0.0
    if (event.message) {
      var message = String(event.message)
      root.lastError = nextState === "error" && event.text
        ? message + "\nDictation was copied to clipboard."
        : message
    }
  }

  function loadUiConfig(raw) {
    try {
      var config = JSON.parse(String(raw || "{}"))
      var position = config.ui ? String(config.ui.hud_position || "top-center") : "top-center"
      root.hudPosition = position === "top-right" ? "top-right" : "top-center"
    } catch (error) {
      root.hudPosition = "top-center"
    }
  }

  // Agent is the only session type, so start/stop/toggle take no arguments
  // and a running daemon is simply left alone.
  function start(): string {
    if (webDaemon.running) webDaemon.write("end-session\n")
    if (daemon.running) return "already-running"
    killTimer.stop()
    root.stopRequested = false
    root.lastError = ""
    root.sessionState = "starting"
    daemon.running = true
    return "starting"
  }

  function stop(): string {
    if (!daemon.running) return "not-running"
    root.stopRequested = true
    daemon.signal(15)
    killTimer.restart()
    return "stopping"
  }

  function toggle(): string {
    return daemon.running ? root.stop() : root.start()
  }

  function dictate(action): string {
    var command = String(action || "").toLowerCase()
    var replies = { start: "recording", stop: "transcribing", handsfree: "locked", cancel: "canceled" }
    if (!replies[command]) return "expected-start-stop-handsfree-or-cancel"
    if (!dictationDaemon.running) return "dictation-daemon-not-running"
    dictationDaemon.write(command + "\n")
    return replies[command]
  }

  // While Omarvis is live, expose that to keybindings. Holding SUPER+J
  // enters the "omarvis-dictate" Hyprland submap from bindings.lua, where
  // SPACE chords into hands-free and ESCAPE cancels without the global
  // SUPER+SPACE menu ever seeing the chord; the submap ends on release. The
  // dynamic plain-Escape bind below exists whenever ANYTHING is live and
  // ends it — cancels a hands-free recording, hangs up a voice session,
  // dismisses an error — and is reset on startup in case a crash left it.
  // Error state shows no HUD (the error went to a notification), so Escape
  // must not be silently intercepted there.
  readonly property bool escapeLive: dictationState === "recording"
    || (sessionState !== "idle" && sessionState !== "error")
  // A phone call is intentionally excluded: Escape at the desk must not end
  // a call happening in another room. While the native panel is open, its
  // key catcher owns Escape and closes the panel before the next press may
  // hang up a local session.
  readonly property bool escapeBindWanted: escapeLive && !panelOpen

  onEscapeBindWantedChanged: updateEscapeBind()

  // The HUD is deliberately text-free, so errors — the one kind of text that
  // must be read — go to the Omarchy notification server, which wraps and
  // persists them properly.
  onLastErrorChanged: if (lastError) notifyError(lastError)

  function notifyError(message) {
    Quickshell.execDetached(["omarchy-notification-send",
      "--app-name", "Omarvis",
      "-g", String.fromCodePoint(0xF0026),
      "-u", "normal",
      "Omarvis voice error", String(message)])
  }

  // Remote-service failures carry repair instructions far too long for the
  // panel, so they take the same route the voice errors do; the panel keeps
  // one short line. Notify once per distinct problem state, not per poll.
  property string notifiedRemoteState: ""

  function remoteProblemDetail(state): string {
    if (state === "needs-tailscale")
      return "Connect Tailscale on this computer, then try Remote access again."
    if (state === "needs-operator")
      return "Run in a terminal: sudo tailscale set --operator=" + Quickshell.env("USER")
    return root.remoteError || "Tailscale Serve failed to publish Omarvis."
  }

  onRemoteStateChanged: {
    if (remoteState === "off" || remoteState === "serving") {
      notifiedRemoteState = ""
      return
    }
    if (remoteState === notifiedRemoteState) return
    notifiedRemoteState = remoteState
    Quickshell.execDetached(["omarchy-notification-send",
      "--app-name", "Omarvis",
      "-g", String.fromCodePoint(0xF0026),
      "-u", "normal",
      "Omarvis remote access", remoteProblemDetail(remoteState)])
  }

  Component.onCompleted: {
    updateEscapeBind()
  }

  // Omarchy's Lua config parser rejects `hyprctl keyword bind`, so the
  // dynamic bind goes through the same `o.bind`/`hl.unbind` Lua API the
  // static config uses, via `hyprctl eval`.
  function updateEscapeBind() {
    if (root.escapeBindWanted)
      Quickshell.execDetached(["hyprctl", "eval",
        "o.bind(\"ESCAPE\", \"Omarvis escape\", \"omarchy-shell omarvis esc\")"])
    else
      Quickshell.execDetached(["hyprctl", "eval", "hl.unbind(\"ESCAPE\")"])
  }

  // Escape ends the most immediate live thing: dictation recording first,
  // then a running voice session, then a lingering error/stale state.
  function escapeAction(): string {
    if (root.dictationState === "recording") return root.dictate("cancel")
    if (daemon.running) return root.stop()
    if (root.sessionState !== "idle") {
      root.sessionState = "idle"
      root.lastError = ""
      return "cleared"
    }
    return "idle"
  }

  function endPhoneSession(): string {
    if (!root.phoneSessionActive) return "not-running"
    if (webDaemon.running) webDaemon.write("end-session\n")
    return "stopping"
  }

  function setRemoteEnabled(enabled): string {
    var next = !!enabled
    if (next === root.remoteEnabled) {
      if (next) {
        Quickshell.execDetached(["mkdir", "-p", Quickshell.env("HOME") + "/.local/share/omarvis"])
        Quickshell.execDetached(["touch", Quickshell.env("HOME") + "/.local/share/omarvis/remote-enabled"])
        if (root.webBindFailed) {
          root.webBindFailed = false
          root.remoteError = ""
          if (root.remoteStartupReady && !webDaemon.running) webDaemon.running = true
          return "retrying"
        }
      } else {
        Quickshell.execDetached(["rm", "-f", Quickshell.env("HOME") + "/.local/share/omarvis/remote-enabled"])
      }
      return next ? "already-enabled" : "already-disabled"
    }
    root.remoteEnabled = next
    if (next) {
      root.webBindFailed = false
      root.remoteError = ""
      Quickshell.execDetached(["mkdir", "-p", Quickshell.env("HOME") + "/.local/share/omarvis"])
      Quickshell.execDetached(["touch", Quickshell.env("HOME") + "/.local/share/omarvis/remote-enabled"])
      if (root.remoteStartupReady && !webDaemon.running) webDaemon.running = true
      return "enabling"
    }
    if (webDaemon.running) {
      webDaemon.write("end-session\n")
      webDaemon.signal(15)
    }
    Quickshell.execDetached(["rm", "-f", Quickshell.env("HOME") + "/.local/share/omarvis/remote-enabled"])
    Quickshell.execDetached([root.pluginDir + "/bin/omarvis-web", "--cleanup"])
    root.remoteState = "off"
    root.remoteError = ""
    root.remoteUrl = ""
    root.qrMatrix = []
    root.phoneSessionActive = false
    root.phoneRunningCommand = ""
    return "disabling"
  }

  function applyRemoteMarker(present) {
    if (root.remoteMarkerSeeded) return
    root.remoteMarkerSeeded = true
    var enabled = !!present
    if (enabled === root.remoteEnabled) return
    root.remoteEnabled = enabled
    if (enabled) {
      root.webBindFailed = false
      if (root.remoteStartupReady && !webDaemon.running) webDaemon.running = true
    } else {
      if (webDaemon.running) webDaemon.signal(15)
      root.remoteState = "off"
      root.remoteError = ""
      root.remoteUrl = ""
      root.qrMatrix = []
      root.phoneSessionActive = false
      root.phoneRunningCommand = ""
    }
  }

  function handleWebEvent(line) {
    var event
    try {
      event = JSON.parse(line)
    } catch (error) {
      console.warn("omarvis web: invalid event:", line)
      return
    }
    if (event.event === "remote") {
      // Error text first: onRemoteStateChanged reads it to build the
      // notification, so a stale message must never win the race.
      root.remoteError = String(event.error || "")
      root.remoteUrl = String(event.url || "")
      root.qrMatrix = event.qr_matrix || []
      root.remoteState = String(event.state || "off")
    } else if (event.event === "phone") {
      root.phoneSessionActive = !!event.active
      if (!root.phoneSessionActive) root.phoneRunningCommand = ""
    } else if (event.event === "running") {
      root.phoneRunningCommand = String(event.command || "")
    } else if (event.event === "ran") {
      root.phoneRunningCommand = ""
    } else if (event.event === "error") {
      root.remoteError = String(event.message || "Remote service error")
    } else if (event.event === "action" && event.action === "end-local") {
      if (root.stop() === "not-running") webDaemon.write("ack-local-ended\n")
      else root.pendingRemoteAck = true
    }
  }

  Process {
    id: daemon
    command: root.daemonCommand.length > 0
      ? root.daemonCommand
      : [root.pluginDir + "/bin/omarvis-run"]
    stdout: SplitParser {
      onRead: data => root.handleEvent(String(data))
    }
    stderr: SplitParser {
      onRead: data => console.log("omarvis:", data)
    }
    onExited: function(exitCode, exitStatus) {
      killTimer.stop()
      var expectedStop = root.stopRequested
      root.stopRequested = false
      root.sessionState = expectedStop || exitCode === 0 ? "idle" : "error"
      root.inLevel = 0.0
      root.outLevel = 0.0
      root.streamingAgent = ""
      root.runningCommand = ""
      if (root.pendingRemoteAck) {
        root.pendingRemoteAck = false
        if (webDaemon.running) webDaemon.write("ack-local-ended\n")
      }
      if (!expectedStop && exitCode !== 0 && !root.lastError)
        root.lastError = "Daemon exited with code " + exitCode
    }
  }

  Process {
    id: webCleanup
    command: [root.pluginDir + "/bin/omarvis-web", "--cleanup"]
    running: true
    stderr: SplitParser {
      onRead: data => console.log("omarvis web cleanup:", data)
    }
    onExited: function(exitCode, exitStatus) {
      root.remoteStartupReady = true
      if (root.remoteEnabled && !root.webBindFailed && !webDaemon.running)
        webDaemon.running = true
    }
  }

  Process {
    id: webDaemon
    command: [root.pluginDir + "/bin/omarvis-web"]
    stdinEnabled: true
    stdout: SplitParser {
      onRead: data => root.handleWebEvent(String(data))
    }
    stderr: SplitParser {
      onRead: data => console.log("omarvis web:", data)
    }
    onExited: function(exitCode, exitStatus) {
      root.pendingRemoteAck = false
      root.phoneSessionActive = false
      root.phoneRunningCommand = ""
      root.remoteUrl = ""
      root.qrMatrix = []
      Quickshell.execDetached([root.pluginDir + "/bin/omarvis-web", "--cleanup"])
      if (!root.remoteEnabled) {
        root.remoteState = "off"
        root.remoteError = ""
        return
      }
      if (exitCode === 3) {
        root.webBindFailed = true
        if (!root.remoteError) root.remoteError = "Port 4763 is already in use. Remote access was not restarted."
        root.remoteState = "serve-failed"
        return
      }
      if (!root.remoteError) root.remoteError = "Remote service exited with code " + exitCode
      root.remoteState = "serve-failed"
      webRestart.restart()
    }
  }

  Timer {
    id: webRestart
    interval: 2000
    repeat: false
    onTriggered: if (root.remoteEnabled && root.remoteStartupReady && !root.webBindFailed && !webDaemon.running)
      webDaemon.running = true
  }

  Process {
    id: dictationDaemon
    command: [root.pluginDir + "/bin/omarvis-dictate"]
    running: root.dictationDaemonEnabled
    stdinEnabled: true
    stdout: SplitParser {
      onRead: data => root.handleDictationEvent(String(data))
    }
    stderr: SplitParser {
      onRead: data => console.log("omarvis dictate:", data)
    }
    onExited: function(exitCode, exitStatus) {
      root.dictationState = "idle"
      root.dictationLevel = 0.0
      if (exitCode !== 0) root.lastError = "Dictation daemon exited with code " + exitCode
      if (root.dictationDaemonEnabled) dictationRestart.restart()
    }
  }

  Timer {
    id: dictationRestart
    interval: 2000
    repeat: false
    onTriggered: if (root.dictationDaemonEnabled && !dictationDaemon.running) dictationDaemon.running = true
  }

  // Live view of the user's actual Omarvis keybindings, parsed from the
  // Hyprland bindings file and re-parsed on every save, so the panel shows
  // real mappings — rerouted keys included — never just the defaults.
  property var keybindings: []

  function parseKeybindings(content) {
    var actions = [
      { match: 'omarchy-shell omarvis dictate start"', label: "Dictation (hold)" },
      { match: 'omarchy-shell omarvis dictate handsfree"', label: "Hands-free dictation" },
      { match: 'omarchy-shell omarvis toggle"', label: "Talk" },
      { match: 'omarchy-shell omarvis toggleRemote"', label: "Remote access" },
      { match: 'omarchy-shell omarvis panel"', label: "Panel" },
    ]
    var found = []
    var lines = String(content || "").split("\n")
    for (var a = 0; a < actions.length; a++) {
      for (var i = 0; i < lines.length; i++) {
        var line = lines[i]
        if (line.trim().indexOf("--") === 0) continue
        if (line.indexOf(actions[a].match) === -1) continue
        var keys = line.match(/bind\(\s*"([^"]+)"/)
        if (keys) {
          found.push({ label: actions[a].label, keys: keys[1] })
          break
        }
      }
    }
    // Hands-free is a chord, not a binding of its own: SPACE is pressed
    // while the dictation keys are still held, so it displays as the
    // dictation keys plus the chord binding's final key — composed from
    // both live bindings so rebinding either side keeps it truthful.
    for (var h = 0; h < found.length; h++) {
      if (found[h].label !== "Hands-free dictation") continue
      var chordKey = found[h].keys.split("+").pop().trim()
      for (var d = 0; d < found.length; d++) {
        if (found[d].label === "Dictation (hold)") {
          found[h].keys = found[d].keys + " + " + chordKey
          break
        }
      }
    }
    root.keybindings = found
  }

  // Omarchy's native unfocused-window border (Hyprland's
  // general:col.inactive_border, rgba(595959aa) in the stock looknfeel).
  // The HUD reuses it for the ephemeral hold-to-talk dictation frame, so
  // "momentary" reads exactly like "unfocused" does everywhere else.
  property color inactiveBorderColor: Qt.rgba(0x59 / 255, 0x59 / 255, 0x59 / 255, 0xAA / 255)

  function applyInactiveBorderJson(payload) {
    try {
      var gradient = String(JSON.parse(payload).gradient || "").trim().split(/\s+/)[0]
      if (/^[0-9a-fA-F]{8}$/.test(gradient))
        root.inactiveBorderColor = "#" + gradient
    } catch (error) {
      // hyprctl missing or Hyprland not running — keep the stock fallback.
    }
  }

  Process {
    running: true
    command: ["hyprctl", "-j", "getoption", "general:col.inactive_border"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyInactiveBorderJson(text)
    }
  }

  FileView {
    id: bindingsFile
    path: Quickshell.env("HOME") + "/.config/hypr/bindings.lua"
    watchChanges: true
    printErrors: false
    onLoaded: root.parseKeybindings(text())
    onLoadFailed: root.keybindings = []
    onFileChanged: reload()
  }

  FileView {
    id: uiConfig
    path: Quickshell.env("HOME") + "/.config/omarchy/omarvis/config.json"
    watchChanges: true
    printErrors: false
    onLoaded: root.loadUiConfig(text())
    onLoadFailed: root.loadUiConfig("{}")
    onFileChanged: reload()
  }

  FileView {
    id: remoteMarker
    path: Quickshell.env("HOME") + "/.local/share/omarvis/remote-enabled"
    watchChanges: true
    printErrors: false
    onLoaded: root.applyRemoteMarker(true)
    onLoadFailed: root.applyRemoteMarker(false)
    onFileChanged: reload()
  }

  // Colors, typography, and radius come from the qs.Commons theme singletons
  // inside HudWindow itself, so the HUD restyles live on omarchy theme set.
  HudWindow {
    id: hudWindow
    service: root
    shell: root.shell
    hudPosition: root.hudPosition
  }

  Timer {
    id: killTimer
    interval: 8000
    repeat: false
    onTriggered: if (daemon.running) daemon.signal(9)
  }

  IpcHandler {
    target: "omarvis"

    function toggle(): string { return root.toggle() }
    function start(): string { return root.start() }
    function stop(): string { return root.stop() }
    function dictate(action: string): string { return root.dictate(action) }
    function panel(): string { root.panelRequested(); return "opening" }
    function setRemote(enabled: bool): string { return root.setRemoteEnabled(enabled) }
    function toggleRemote(): string { return root.setRemoteEnabled(!root.remoteEnabled) }
    // "escape" collides with the JS global, which QML rejects as a method
    // name, hence "esc".
    function esc(): string { return root.escapeAction() }
    function status(): string {
      return JSON.stringify({
        sessionState: root.sessionState,
        lastUser: root.lastUser,
        lastAgent: root.lastAgent,
        lastError: root.lastError,
        dictationState: root.dictationState,
        dictationLocked: root.dictationLocked,
        inLevel: root.inLevel,
        outLevel: root.outLevel,
        streamingAgent: root.streamingAgent,
        runningCommand: root.runningCommand,
        lastCommand: root.lastCommand,
        dictationLevel: root.dictationLevel,
        panelOpen: root.panelOpen,
        remoteEnabled: root.remoteEnabled,
        remoteState: root.remoteState,
        remoteError: root.remoteError,
        remoteUrl: root.remoteUrl,
        phoneSessionActive: root.phoneSessionActive,
        phoneRunningCommand: root.phoneRunningCommand,
        running: daemon.running
      })
    }
  }
}
