import QtQuick
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
  property bool stopRequested: false
  readonly property string pluginDir: String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "").replace(/\/$/, "")

  function handleEvent(line) {
    var event
    try {
      event = JSON.parse(line)
    } catch (error) {
      console.warn("omarvis: invalid daemon event:", line)
      return
    }
    if (event.event === "state") {
      root.sessionState = String(event.state || "idle")
      if (event.mode) root.currentMode = root.normalizeMode(event.mode)
    }
    else if (event.event === "user") root.lastUser = String(event.text || "")
    else if (event.event === "agent") root.lastAgent = String(event.text || "")
    else if (event.event === "error") {
      root.lastError = String(event.message || "Unknown error")
      root.sessionState = "error"
    }
  }

  function normalizeMode(mode): string {
    return String(mode || "agent") === "ask" ? "ask" : "agent"
  }

  function start(mode = "agent"): string {
    var requestedMode = root.normalizeMode(mode)
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

  Process {
    id: daemon
    command: [root.pluginDir + "/bin/omarvis-run", "--mode", root.currentMode]
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
      if (!expectedStop && exitCode !== 0 && !root.lastError)
        root.lastError = "Daemon exited with code " + exitCode
      if (root.pendingMode) {
        var restartMode = root.pendingMode
        root.pendingMode = ""
        Qt.callLater(function() { root.start(restartMode) })
      }
    }
  }

  Timer {
    id: killTimer
    interval: 8000
    repeat: false
    onTriggered: if (daemon.running) daemon.signal(9)
  }

  IpcHandler {
    target: "omarvis"

    function toggle(mode = "agent"): string { return root.toggle(mode) }
    function start(mode = "agent"): string { return root.start(mode) }
    function stop(): string { return root.stop() }
    function status(): string {
      return JSON.stringify({
        sessionState: root.sessionState,
        lastUser: root.lastUser,
        lastAgent: root.lastAgent,
        lastError: root.lastError,
        currentMode: root.currentMode,
        running: daemon.running
      })
    }
  }
}
