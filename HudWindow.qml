import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Wayland

PanelWindow {
  id: hud

  property var service: null
  property var shell: null
  property string hudPosition: "top-center"
  property color foregroundColor: "#f2f4f5"
  property color backgroundColor: "#e6101315"
  property color accentColor: "#7dcfff"
  property color urgentColor: "#ff6b6b"
  property bool speakingFallback: false
  property bool toolSucceeded: false
  property string toolText: ""

  readonly property bool dictating: service && service.dictationState !== "idle"
  readonly property bool hudVisible: service && (service.sessionState !== "idle" || service.dictationState !== "idle")
  readonly property real userLevel: !service ? 0.0 : (dictating ? service.dictationLevel : service.inLevel)
  readonly property string visualState: {
    if (!service) return "idle"
    if (service.dictationState !== "idle") return service.dictationState
    if (service.sessionState === "speaking" && speakingFallback) return "listening"
    return service.sessionState
  }
  readonly property bool errorState: visualState === "error"
  readonly property string prefix: service && service.currentMode === "ask" && service.sessionState !== "idle" ? "Ask · " : ""
  readonly property string stateLabel: {
    if (visualState === "recording") return "● Dictating…"
    if (visualState === "transcribing") return "Transcribing…"
    if (visualState === "starting") return "Connecting…"
    if (visualState === "thinking") return "Thinking…"
    if (visualState === "speaking") return "Speaking…"
    if (visualState === "error") return "Voice session failed"
    return "Listening…"
  }
  readonly property string displayText: {
    if (!service) return stateLabel
    if (service.lastError) return prefix + service.lastError
    if (service.streamingAgent) return prefix + service.streamingAgent
    if (service.lastAgent) return prefix + service.lastAgent
    if (service.lastUser) return prefix + "You: " + service.lastUser
    return prefix + stateLabel
  }

  function capture(path) {
    pill.grabToImage(function(result) { result.saveToFile(path) })
  }

  visible: hudVisible
  anchors { top: true; bottom: true; left: true; right: true }
  color: "transparent"
  WlrLayershell.namespace: "omarvis-hud"
  WlrLayershell.layer: WlrLayer.Overlay
  WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
  exclusionMode: ExclusionMode.Ignore
  mask: Region {}

  Connections {
    target: hud.service
    function onSessionStateChanged() {
      hud.speakingFallback = false
    }
    function onOutLevelChanged() {
      if (hud.service && hud.service.outLevel >= 0.02) hud.speakingFallback = false
    }
    function onRunningCommandChanged() {
      if (hud.service && hud.service.runningCommand) {
        hud.toolText = hud.service.runningCommand
        hud.toolSucceeded = false
        toolDoneTimer.stop()
      }
    }
    function onCommandRan(command) {
      hud.toolText = String(command || hud.toolText)
      hud.toolSucceeded = true
      toolDoneTimer.restart()
    }
  }

  Timer {
    id: speakingSilenceTimer
    interval: 1500
    repeat: false
    running: hud.visible && hud.service && hud.service.sessionState === "speaking" && hud.service.outLevel < 0.02
    onTriggered: hud.speakingFallback = true
  }

  Timer {
    id: toolDoneTimer
    interval: 1000
    repeat: false
    onTriggered: {
      hud.toolSucceeded = false
      hud.toolText = ""
    }
  }

  Rectangle {
    id: pill
    objectName: "omarvisHudPill"
    width: Math.min(420, Math.max(300, parent.width - 32))
    height: 54
    x: hud.hudPosition === "top-right" ? parent.width - width - 18 : Math.round((parent.width - width) / 2)
    y: 18
    radius: 27
    color: hud.backgroundColor
    border.width: hud.errorState ? 2 : 1
    border.color: hud.errorState ? hud.urgentColor : Qt.rgba(hud.accentColor.r, hud.accentColor.g, hud.accentColor.b, 0.55)

    RowLayout {
      anchors.fill: parent
      anchors.leftMargin: 16
      anchors.rightMargin: 16
      spacing: 12

      Item {
        Layout.preferredWidth: 22
        Layout.preferredHeight: 22
        Rectangle {
          id: userDot
          objectName: "omarvisUserDot"
          width: 16
          height: 16
          anchors.centerIn: parent
          radius: 8
          color: hud.foregroundColor
          opacity: hud.userLevel < 0.02 ? 0.35 : 0.95
          scale: 0.72 + hud.userLevel * 0.78
          Behavior on scale { NumberAnimation { duration: 120; easing.type: Easing.OutQuad } }
          Behavior on opacity { NumberAnimation { duration: 120 } }
          Rectangle {
            anchors.centerIn: parent
            width: parent.width + 8
            height: width
            radius: width / 2
            color: "transparent"
            border.width: 2
            border.color: Qt.rgba(hud.foregroundColor.r, hud.foregroundColor.g, hud.foregroundColor.b, 0.18 + hud.userLevel * 0.45)
          }
        }
      }

      Item {
        Layout.fillWidth: true
        Layout.minimumWidth: 70
        Layout.preferredHeight: 24
        Text {
          id: transcript
          objectName: "omarvisHudText"
          anchors.fill: parent
          text: hud.displayText
          textFormat: Text.PlainText
          elide: Text.ElideRight
          maximumLineCount: 1
          color: hud.errorState ? hud.urgentColor : hud.foregroundColor
          font.pixelSize: 14
          verticalAlignment: Text.AlignVCenter
        }
      }

      Item {
        id: agentDot
        objectName: "omarvisAgentDot"
        Layout.preferredWidth: 22
        Layout.preferredHeight: 22
        scale: hud.visualState === "speaking" && hud.service
          ? 0.72 + hud.service.outLevel * 0.78 : 1.0
        opacity: hud.visualState === "listening" ? 0.4 : 1.0
        Behavior on scale { NumberAnimation { duration: 120; easing.type: Easing.OutQuad } }

        Rectangle {
          width: 16
          height: 16
          anchors.centerIn: parent
          radius: 8
          color: hud.visualState === "thinking" ? "transparent" : (hud.errorState ? hud.urgentColor : hud.accentColor)
          border.width: hud.visualState === "thinking" ? 2 : 0
          border.color: hud.accentColor
        }
        Rectangle {
          visible: hud.visualState === "thinking"
          width: 5
          height: 5
          radius: 3
          x: 8
          y: 0
          color: hud.accentColor
        }
        RotationAnimation on rotation {
          running: hud.visible && hud.visualState === "thinking"
          from: 0
          to: 360
          duration: 1200
          loops: Animation.Infinite
        }
        SequentialAnimation on opacity {
          running: hud.visible && hud.visualState === "thinking"
          loops: Animation.Infinite
          NumberAnimation { from: 1.0; to: 0.45; duration: 600 }
          NumberAnimation { from: 0.45; to: 1.0; duration: 600 }
        }
      }

      Rectangle {
        id: toolChip
        objectName: "omarvisToolChip"
        visible: (hud.service && hud.service.runningCommand.length > 0) || hud.toolSucceeded
        Layout.preferredWidth: visible ? Math.min(145, toolRow.implicitWidth + 16) : 0
        Layout.preferredHeight: 30
        radius: 15
        color: Qt.rgba(hud.foregroundColor.r, hud.foregroundColor.g, hud.foregroundColor.b, 0.12)

        Row {
          id: toolRow
          anchors.centerIn: parent
          spacing: 6
          Text {
            id: toolGlyph
            text: hud.toolSucceeded ? "✓" : "◌"
            color: hud.foregroundColor
            font.pixelSize: 14
            RotationAnimation on rotation {
              running: hud.visible && !hud.toolSucceeded && toolChip.visible
              from: 0
              to: 360
              duration: 850
              loops: Animation.Infinite
            }
          }
          Text {
            width: Math.min(105, implicitWidth)
            text: hud.toolText.slice(0, 40)
            color: hud.foregroundColor
            textFormat: Text.PlainText
            elide: Text.ElideRight
            font.pixelSize: 11
          }
        }
      }
    }
  }
}
