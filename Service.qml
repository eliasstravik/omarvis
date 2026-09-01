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
  property string currentMode: "agent"
  property string pendingMode: ""
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
      if (event.mode) root.currentMode = root.normalizeMode(event.mode)
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

  function normalizeMode(mode): string {
    return String(mode || "agent") === "ask" ? "ask" : "agent"
  }

  function start(mode = "agent"): string {
    var requestedMode = root.normalizeMode(mode)
    if (webDaemon.running) webDaemon.write("end-session\n")
    if (daemon.running) {
      if (requestedMode === root.currentMode) return "already-running"
      root.pendingMode = requestedMode
      root.stopRequested = true
      daemon.signal(15)
      killTimer.restart()
      return "restarting"
    }
    killTimer.stop()
    root.stopRequested = false
    root.lastError = ""
    root.currentMode = requestedMode
    root.sessionState = "starting"
    daemon.running = true
    return "starting"
  }

  function stop(): string {
    if (!daemon.running) return "not-running"
    root.pendingMode = ""
    root.stopRequested = true
    daemon.signal(15)
    killTimer.restart()
    return "stopping"
  }

  function toggle(mode = "agent"): string {
    var requestedMode = root.normalizeMode(mode)
    if (!daemon.running) return root.start(requestedMode)
    if (requestedMode === root.currentMode) return root.stop()
    return root.start(requestedMode)
  }

  function dictate(action): string {
    var command = String(action || "").toLowerCase()
    var replies = { start: "recording", stop: "transcribing", handsfree: "locked", cancel: "canceled" }
    if (!replies[command]) return "expected-start-stop-handsfree-or-cancel"
    if (!dictationDaemon.running) return "dictation-daemon-not-running"
    dictationDaemon.write(command + "\n")
    return replies[command]
  }

  // While Omarvis is live, expose that to keybindings. Two separate
  // lifetimes: the marker file bin/omarvis-space tests (SUPER+SPACE →
  // hands-free instead of the menu) exists only during a dictation
  // recording, so the menu keeps working during voice sessions; the dynamic
  // plain-Escape bind exists whenever ANYTHING is live and ends it —
  // cancels a dictation recording, hangs up a voice session, dismisses an
  // error. Both are absent when idle, so Escape and SUPER+SPACE behave
  // normally the rest of the time, and both are reset on startup in case a
  // crash left them behind.
  readonly property string dictatingMarker: Quickshell.env("XDG_RUNTIME_DIR") + "/omarvis-dictating"
  // Error state shows no HUD (the error went to a notification), so Escape
  // must not be silently intercepted there.
  readonly property bool escapeLive: dictationState === "recording"
    || (sessionState !== "idle" && sessionState !== "error")
  // A phone call is intentionally excluded: Escape at the desk must not end
  // a call happening in another room. While the native panel is open, its
  // key catcher owns Escape and closes the panel before the next press may
  // hang up a local session.
  readonly property bool escapeBindWanted: escapeLive && !panelOpen

  onDictationStateChanged: updateDictationMarker()
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
  Component.onCompleted: {
    updateDictationMarker()
    updateEscapeBind()
  }

  function updateDictationMarker() {
    if (root.dictationState === "recording")
      Quickshell.execDetached(["touch", root.dictatingMarker])
    else
      Quickshell.execDetached(["rm", "-f", root.dictatingMarker])
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
      root.remoteState = String(event.state || "off")
      root.remoteError = String(event.error || "")
      root.remoteUrl = String(event.url || "")
      root.qrMatrix = event.qr_matrix || []
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
      : [root.pluginDir + "/bin/omarvis-run", "--mode", root.currentMode]
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
      if (root.pendingMode) {
        var restartMode = root.pendingMode
        root.pendingMode = ""
        Qt.callLater(function() { root.start(restartMode) })
      }
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
        root.remoteState = "serve-failed"
        if (!root.remoteError) root.remoteError = "Port 4763 is already in use. Remote access was not restarted."
        return
      }
      root.remoteState = "serve-failed"
      if (!root.remoteError) root.remoteError = "Remote service exited with code " + exitCode
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

    // Quickshell only exports IPC parameters that have explicit QML types.
    // Keep the original no-argument Agent routes for existing installs, and
    // use separate typed routes when a mode is selected explicitly.
    function toggle(): string { return root.toggle("agent") }
    function toggleMode(mode: string): string { return root.toggle(mode) }
    function start(): string { return root.start("agent") }
    function startMode(mode: string): string { return root.start(mode) }
    function stop(): string { return root.stop() }
    function dictate(action: string): string { return root.dictate(action) }
    function panel(): string { root.panelRequested(); return "opening" }
    function setRemote(enabled: bool): string { return root.setRemoteEnabled(enabled) }
    // "escape" collides with the JS global, which QML rejects as a method
    // name, hence "esc".
    function esc(): string { return root.escapeAction() }
    function status(): string {
      return JSON.stringify({
        sessionState: root.sessionState,
        lastUser: root.lastUser,
        lastAgent: root.lastAgent,
        lastError: root.lastError,
        currentMode: root.currentMode,
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
