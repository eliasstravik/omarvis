import QtQuick
import Quickshell
import Omarvis

ShellRoot {
  id: harness
  readonly property string captureDir: Quickshell.env("OMARVIS_HUD_CAPTURE_DIR")
  property bool capturedListening: false
  property bool capturedThinking: false
  property bool capturedSpeaking: false
  property bool capturedTool: false
  property bool capturedError: false

  function capture(name) {
    if (captureDir && service.hud) service.hud.capture(captureDir + "/" + name + ".png")
  }

  Service {
    id: service
    dictationDaemonEnabled: false
    Component.onCompleted: service.start("agent")
  }

  Connections {
    target: service
    function onSessionStateChanged() {
      console.log("omarvis HUD harness state:", service.sessionState)
      if (service.sessionState === "listening" && !harness.capturedListening) listeningCapture.restart()
      else if (service.sessionState === "thinking" && !harness.capturedThinking) thinkingCapture.restart()
      else if (service.sessionState === "speaking" && !harness.capturedSpeaking) speakingCapture.restart()
      else if (service.sessionState === "error" && !harness.capturedError) errorCapture.restart()
    }
    function onDictationStateChanged() { console.log("omarvis HUD harness dictation:", service.dictationState) }
    function onRunningCommandChanged() {
      if (service.runningCommand && !harness.capturedTool) {
        console.log("omarvis HUD harness tool text:", service.runningCommand)
        toolCapture.restart()
      }
    }
  }

  Timer {
    id: listeningCapture
    interval: 500
    onTriggered: { harness.capturedListening = true; harness.capture("listening") }
  }
  Timer {
    id: thinkingCapture
    interval: 300
    onTriggered: { harness.capturedThinking = true; harness.capture("thinking") }
  }
  Timer {
    id: speakingCapture
    interval: 800
    onTriggered: { harness.capturedSpeaking = true; harness.capture("speaking") }
  }
  Timer {
    id: toolCapture
    interval: 500
    onTriggered: { harness.capturedTool = true; harness.capture("tool-running") }
  }
  Timer {
    id: errorCapture
    interval: 200
    onTriggered: { harness.capturedError = true; harness.capture("error") }
  }
}
