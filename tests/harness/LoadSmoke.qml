import QtQuick
import Quickshell
import Omarvis

// Load-only smoke harness: instantiates every QML surface the plugin ships —
// the service, the HUD, and the bar panel — without starting a session, so
// nothing is drawn on screen. Any binding loop, missing property, or typo in
// a state expression shows up as a QML error in the log. Run it with
// `tests/harness/load-smoke`.
ShellRoot {
  id: harness

  property int ticks: 0

  Service {
    id: service
    dictationDaemonEnabled: false
  }

  // Walk the whole state vocabulary so every glyph, color and status branch
  // in the HUD and the panel is actually evaluated at least once.
  readonly property var sessionStates: [
    "starting", "listening", "thinking", "speaking", "stopping", "error", "idle"
  ]
  readonly property var dictationStates: ["recording", "transcribing", "idle"]
  readonly property var remoteStates: [
    "serving", "needs-tailscale", "needs-operator", "serve-failed", "off"
  ]

  // Loaded by URL rather than as a registered type: Panel.qml derives from
  // the shell's own qs.Ui Panel, and registering a second `Panel` in this
  // directory would shadow it.
  Loader {
    id: barPanel
    source: "file://" + Quickshell.env("OMARVIS_REPO") + "/Panel.qml"
    onLoaded: item.svc = service
    onStatusChanged: if (status === Loader.Error) console.log("ERROR: panel failed to load")
  }

  Timer {
    interval: 120
    repeat: true
    running: true
    onTriggered: {
      var index = harness.ticks++
      service.sessionState = harness.sessionStates[index % harness.sessionStates.length]
      service.dictationState = harness.dictationStates[index % harness.dictationStates.length]
      service.dictationLocked = index % 2 === 0
      service.remoteState = harness.remoteStates[index % harness.remoteStates.length]
      service.remoteEnabled = index % 2 === 0
      service.phoneSessionActive = index % 3 === 0
      service.runningCommand = index % 4 === 0 ? "omarchy theme set tokyo-night" : ""
      service.inLevel = (index % 10) / 10
      service.outLevel = ((index + 3) % 10) / 10
      console.log("omarvis smoke:", service.sessionState,
        service.dictationState, service.remoteState,
        "| panel:", barPanel.item ? barPanel.item.displayState : "?",
        barPanel.item ? barPanel.item.glyphText.codePointAt(0).toString(16) : "?",
        "| hud:", service.hud.stateGlyph.codePointAt(0).toString(16),
        service.hud.waitingToConnect)
      if (harness.ticks > harness.sessionStates.length * harness.remoteStates.length) {
        console.log("omarvis smoke: done")
        Qt.quit()
      }
    }
  }
}
