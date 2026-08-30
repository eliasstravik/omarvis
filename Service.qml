import QtQuick
import Quickshell.Io

Item {
  id: root

  property var shell: null
  property string sessionState: "idle"
  property string lastUser: ""
  property string lastAgent: ""
  property string lastError: ""
  readonly property string pluginDir: String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "").replace(/\/$/, "")

  function handleEvent(line) {
    var event
    try {
      event = JSON.parse(line)
    } catch (error) {
      console.warn("omarvis: invalid daemon event:", line)
      return
    }
    if (event.event === "state") root.sessionState = String(event.state || "idle")
    else if (event.event === "user") root.lastUser = String(event.text || "")
    else if (event.event === "agent") root.lastAgent = String(event.text || "")
    else if (event.event === "error") {
      root.lastError = String(event.message || "Unknown error")
      root.sessionState = "error"
    }
  }

  function start(): string {
    if (daemon.running) return "already-running"
    killTimer.stop()
    root.lastError = ""
    root.sessionState = "starting"
    daemon.running = true
    return "starting"
  }

  function stop(): string {
    if (!daemon.running) return "not-running"
    daemon.signal(15)
    killTimer.restart()
    return "stopping"
  }

  function toggle(): string {
    return daemon.running ? root.stop() : root.start()
  }

  Process {
    id: daemon
    command: [root.pluginDir + "/bin/omarvis-run"]
    stdout: SplitParser {
      onRead: data => root.handleEvent(String(data))
    }
    stderr: SplitParser {
      onRead: data => console.log("omarvis:", data)
    }
    onExited: function(exitCode, exitStatus) {
      killTimer.stop()
      root.sessionState = exitCode === 0 ? "idle" : "error"
      if (exitCode !== 0 && !root.lastError)
        root.lastError = "Daemon exited with code " + exitCode
    }
  }

  Timer {
    id: killTimer
    interval: 5000
    repeat: false
    onTriggered: if (daemon.running) daemon.signal(9)
  }

  IpcHandler {
    target: "omarvis"

    function toggle(): string { return root.toggle() }
    function start(): string { return root.start() }
    function stop(): string { return root.stop() }
    function status(): string {
      return JSON.stringify({
        sessionState: root.sessionState,
        lastUser: root.lastUser,
        lastAgent: root.lastAgent,
        lastError: root.lastError,
        running: daemon.running
      })
    }
  }
}
