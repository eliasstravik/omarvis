import QtQuick
import qs.Ui
import qs.Commons

BarWidget {
  id: root
  moduleName: "omarvis.voice"

  property var svc: null
  readonly property string sessionState: svc ? svc.sessionState : "idle"
  readonly property string currentMode: svc ? svc.currentMode : "agent"
  readonly property string dictationState: svc ? svc.dictationState : "idle"
  readonly property string runningCommand: svc ? svc.runningCommand : ""
  readonly property string displayState: runningCommand ? "running" : (dictationState !== "idle" ? dictationState : sessionState)
  readonly property string glyphText: {
    if (sessionState === "error" || dictationState === "error") return String.fromCodePoint(0xF0026)
    if (runningCommand) return String.fromCodePoint(0xF0493)
    if (dictationState === "recording") return String.fromCodePoint(0xF036C)
    if (dictationState === "transcribing") return String.fromCodePoint(0xF06D7)
    if (sessionState === "thinking") return String.fromCodePoint(0xF051F)
    if (sessionState === "speaking") return String.fromCodePoint(0xF057E)
    if (currentMode === "ask" && sessionState !== "idle") return String.fromCodePoint(0xF02D6)
    if (sessionState === "idle") return String.fromCodePoint(0xF036D)
    return String.fromCodePoint(0xF036C)
  }
  readonly property color glyphColor: {
    if (sessionState === "error" || dictationState === "error") return bar.urgent
    if (sessionState === "idle") return Qt.darker(bar.foreground, 1.5)
    return bar.foreground
  }

  function resolveService() {
    if (!root.svc && root.bar && root.bar.shell && typeof root.bar.shell.serviceFor === "function")
      root.svc = root.bar.shell.serviceFor(root.moduleName)
    return root.svc
  }

  function tooltipText() {
    var text = root.dictationState !== "idle"
      ? "dictation: " + root.dictationState
      : root.currentMode + ": " + root.sessionState
    if (root.svc && root.svc.lastDictation) text += "\nDictated: " + root.svc.lastDictation
    if (root.svc && root.svc.lastUser) text += "\nYou: " + root.svc.lastUser
    if (root.svc && root.svc.lastAgent) text += "\nOmarvis: " + root.svc.lastAgent
    if (root.svc && root.svc.lastError) text += "\n" + root.svc.lastError
    return text
  }

  implicitWidth: row.implicitWidth + Style.space(14)
  implicitHeight: barSize

  Component.onCompleted: resolveService()

  Timer {
    interval: 1000
    repeat: true
    running: !root.svc
    onTriggered: root.resolveService()
  }

  Row {
    id: row
    anchors.centerIn: parent
    spacing: Style.space(6)

    Text {
      id: glyph
      anchors.verticalCenter: parent.verticalCenter
      textFormat: Text.PlainText
      text: root.glyphText
      color: root.glyphColor
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.body

      SequentialAnimation on opacity {
        running: root.sessionState === "starting" || root.dictationState === "recording" || root.dictationState === "transcribing"
        loops: Animation.Infinite
        NumberAnimation { from: 1.0; to: 0.35; duration: 450 }
        NumberAnimation { from: 0.35; to: 1.0; duration: 450 }
      }
    }

    Text {
      anchors.verticalCenter: parent.verticalCenter
      visible: root.setting("showLabel", false)
      textFormat: Text.PlainText
      text: root.displayState
      color: root.bar.foreground
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.bodySmall
    }
  }

  MouseArea {
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    onClicked: function() {
      var service = root.resolveService()
      if (service) service.toggle()
    }
    onEntered: if (root.bar) root.bar.showTooltip(root, root.tooltipText())
    onExited: if (root.bar) root.bar.hideTooltip(root)
  }
}
