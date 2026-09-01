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
  readonly property bool dictationLocked: svc ? svc.dictationLocked : false
  readonly property string runningCommand: svc ? svc.runningCommand : ""
  readonly property string displayState: runningCommand ? "running" : (dictationState !== "idle" ? dictationState : sessionState)
  readonly property string glyphText: {
    if (sessionState === "error" || dictationState === "error") return String.fromCodePoint(0xF0026)
    if (runningCommand) return String.fromCodePoint(0xF05B7)
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
    // Hot microphone gets the bar's attention color — especially important
    // in hands-free (tap-locked) dictation, where no held key reminds you
    // the mic is open.
    if (dictationState === "recording") return bar.urgent
    return bar.foreground
  }
  // Idle reads as a concealed indicator: same 0.45 dim the stock bar
  // indicators use, instead of a darkened color.
  readonly property real glyphOpacity: sessionState === "idle" && dictationState === "idle" ? 0.45 : 1.0

  function resolveService() {
    if (!root.svc && root.bar && root.bar.shell && typeof root.bar.shell.serviceFor === "function")
      root.svc = root.bar.shell.serviceFor(root.moduleName)
    return root.svc
  }

  function tooltipText() {
    var text = root.dictationState !== "idle"
      ? "dictation: " + root.dictationState + (root.dictationLocked ? " (hands-free)" : "")
      : root.currentMode + ": " + root.sessionState
    if (root.svc && root.svc.lastUser) text += "\nYou: " + root.svc.lastUser
    if (root.svc && root.svc.lastAgent) text += "\nOmarvis: " + root.svc.lastAgent
    if (root.svc && root.svc.runningCommand) text += "\nRunning: " + root.svc.runningCommand
    else if (root.svc && root.svc.lastCommand) text += "\nRan: " + root.svc.lastCommand
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
      opacity: root.glyphOpacity
      font.family: root.bar.fontFamily
      font.pixelSize: Style.bar.iconFont

      Behavior on opacity { NumberAnimation { duration: 100 } }
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
