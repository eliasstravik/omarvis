import QtQuick
import QtQuick.Controls
import Quickshell
import qs.Ui
import qs.Commons

Panel {
  id: root
  moduleName: "omarvis.voice"
  manageIpc: false

  property var svc: null
  property int statusTick: 0

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color accent: Color.accent
  readonly property color dim: Qt.darker(foreground, 1.45)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property string sessionState: svc ? svc.sessionState : "idle"
  readonly property string currentMode: svc ? svc.currentMode : "agent"
  readonly property string dictationState: svc ? svc.dictationState : "idle"
  readonly property bool dictationLocked: svc ? svc.dictationLocked : false
  readonly property string runningCommand: svc ? svc.runningCommand : ""
  readonly property bool phoneSessionActive: svc ? svc.phoneSessionActive : false
  readonly property string phoneRunningCommand: svc ? svc.phoneRunningCommand : ""
  readonly property string remoteState: svc ? svc.remoteState : "off"
  readonly property string remoteUrl: svc ? svc.remoteUrl : ""
  readonly property var qrMatrix: svc ? svc.qrMatrix : []

  readonly property bool hasLocalError: sessionState === "error" || dictationState === "error"
  readonly property bool localSessionLive: sessionState !== "idle" && sessionState !== "error"
  readonly property bool localActivity: hasLocalError || runningCommand !== ""
    || dictationState !== "idle" || localSessionLive
  readonly property string displayState: {
    if (hasLocalError) return "error"
    if (runningCommand) return "running"
    if (dictationState !== "idle") return dictationState
    if (localSessionLive) return sessionState
    if (phoneRunningCommand) return "phone command"
    if (phoneSessionActive) return "phone live"
    return "idle"
  }
  readonly property string glyphText: {
    // Local state always wins. Phone activity must never masquerade as a
    // command or hot microphone at the desk.
    if (hasLocalError) return String.fromCodePoint(0xF0026)
    if (runningCommand) return String.fromCodePoint(0xF05B7)
    if (dictationState === "recording") return String.fromCodePoint(0xF036C)
    if (dictationState === "transcribing") return String.fromCodePoint(0xF06D7)
    if (sessionState === "thinking") return String.fromCodePoint(0xF051F)
    if (sessionState === "speaking") return String.fromCodePoint(0xF057E)
    if (currentMode === "ask" && localSessionLive) return String.fromCodePoint(0xF02D6)
    if (localSessionLive) return String.fromCodePoint(0xF036C)
    if (phoneRunningCommand) return String.fromCodePoint(0xF0951)
    if (phoneSessionActive) return String.fromCodePoint(0xF036D)
    return String.fromCodePoint(0xF036D)
  }
  readonly property color glyphColor: hasLocalError || dictationState === "recording"
    ? urgent : foreground
  readonly property bool glyphDimmed: !localActivity && !phoneSessionActive && phoneRunningCommand === ""

  implicitWidth: barRow.implicitWidth
  implicitHeight: button.implicitHeight

  function resolveService() {
    if (!root.svc && root.bar && root.bar.shell && typeof root.bar.shell.serviceFor === "function")
      root.svc = root.bar.shell.serviceFor(root.moduleName)
    return root.svc
  }

  function syncPanelOpen() {
    var service = root.resolveService()
    if (service) service.panelOpen = root.opened
  }

  function animatedStatusPhrase() {
    var base = "Idle"
    if (root.dictationState === "recording") base = root.dictationLocked ? "Dictating hands-free" : "Dictating"
    else if (root.dictationState === "transcribing") base = "Transcribing"
    else if (root.sessionState === "starting") base = "Starting"
    else if (root.sessionState === "listening") base = "Listening"
    else if (root.sessionState === "thinking") base = "Thinking"
    else if (root.sessionState === "speaking") base = "Speaking"
    else if (root.sessionState === "stopping") base = "Ending"
    else if (root.hasLocalError) base = "Needs attention"
    else if (root.phoneSessionActive) base = "Live on phone"
    if (base === "Idle" || base === "Needs attention" || base === "Live on phone") return base
    return base + Array((root.statusTick % 3) + 2).join(".")
  }

  function tooltipText() {
    var text = root.dictationState !== "idle"
      ? "dictation: " + root.dictationState + (root.dictationLocked ? " (hands-free)" : "")
      : root.currentMode + ": " + root.sessionState
    if (root.remoteState !== "off") text += "\nremote: " + root.remoteState
    return text
  }

  function startMode(mode) {
    var service = root.resolveService()
    if (service) service.start(mode)
  }

  function endLocalSession() {
    var service = root.resolveService()
    if (service) service.stop()
  }

  function copyText(text) {
    var value = String(text || "")
    if (value !== "") Quickshell.execDetached(["wl-copy", "--", value])
  }

  function remoteStatusText() {
    if (root.remoteState === "serving") return "Serving on your tailnet"
    if (root.remoteState === "needs-tailscale") return "Connect Tailscale on this computer"
    if (root.remoteState === "needs-operator") return "Tailscale operator access is required"
    if (root.remoteState.indexOf("serve-failed") === 0) return "Tailscale Serve failed"
    return "Remote access is off"
  }

  onOpenedChanged: {
    syncPanelOpen()
    if (opened) {
      statusTick = 0
      if (panelFlick) panelFlick.contentY = 0
    }
  }
  onSvcChanged: syncPanelOpen()
  onQrMatrixChanged: qrCanvas.requestPaint()
  Component.onCompleted: resolveService()
  Component.onDestruction: if (svc) svc.panelOpen = false

  Timer {
    interval: 1000
    repeat: true
    running: !root.svc
    onTriggered: root.resolveService()
  }

  Timer {
    interval: 650
    repeat: true
    running: root.opened && root.localActivity && !root.hasLocalError
    onTriggered: root.statusTick++
  }

  Connections {
    target: root.svc
    function onPanelRequested() { root.open() }
  }

  Row {
    id: barRow
    height: button.implicitHeight
    spacing: Style.space(6)

    BarIconButton {
      id: button
      bar: root.bar
      text: root.glyphText
      foreground: root.glyphColor
      dimmed: root.glyphDimmed
      tooltipText: root.tooltipText()
      onPressed: function(buttonCode) {
        // D5: only left click owns panel behavior. Right and middle click are
        // intentionally inert.
        if (buttonCode === Qt.LeftButton) root.toggle()
      }
    }

    Text {
      id: stateLabel
      visible: root.setting("showLabel", false)
      anchors.verticalCenter: parent.verticalCenter
      text: root.displayState
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall

      MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton | Qt.RightButton | Qt.MiddleButton
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: function(mouse) { if (mouse.button === Qt.LeftButton) root.toggle() }
        onEntered: if (root.bar) root.bar.showTooltip(root, root.tooltipText())
        onExited: if (root.bar) root.bar.hideTooltip(root)
      }
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(390))
    contentHeight: panel.fittedContentHeight(panelColumn.implicitHeight, Style.space(640))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: panelColumn.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: panelColumn
          width: panelFlick.width
          spacing: Style.space(12)

          PanelHero {
            width: parent.width
            title: root.currentMode === "ask" ? "Omarvis Ask" : "Omarvis Agent"
            meta: root.animatedStatusPhrase()
            detail: root.displayState
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconOpacity: root.glyphDimmed ? 0.45 : 1.0
            iconComponent: Component {
              Text {
                text: root.glyphText
                color: root.glyphColor
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }
          }

          Row {
            visible: !root.localSessionLive
            width: parent.width
            spacing: Style.space(8)

            Button {
              text: "Start Agent"
              iconText: String.fromCodePoint(0xF036C)
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.startMode("agent")
            }

            Button {
              text: "Start Ask"
              iconText: String.fromCodePoint(0xF02D6)
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.startMode("ask")
            }
          }

          Button {
            visible: root.localSessionLive
            text: "End session"
            iconText: String.fromCodePoint(0xF04DB)
            foreground: root.urgent
            fontFamily: root.fontFamily
            onClicked: root.endLocalSession()
          }

          PanelSeparator {
            visible: exchangeSection.visible
            foreground: root.foreground
          }

          Column {
            id: exchangeSection
            visible: !!root.svc && (root.svc.lastUser !== "" || root.svc.lastAgent !== "" || root.svc.streamingAgent !== "")
            width: parent.width
            spacing: Style.space(8)

            PanelSectionHeader {
              text: "LAST EXCHANGE"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            ExchangeText {
              visible: !!root.svc && root.svc.lastUser !== ""
              speaker: "YOU"
              value: root.svc ? root.svc.lastUser : ""
            }

            ExchangeText {
              visible: !!root.svc && (root.svc.streamingAgent !== "" || root.svc.lastAgent !== "")
              speaker: "OMARVIS"
              value: root.svc ? (root.svc.streamingAgent || root.svc.lastAgent) : ""
            }
          }

          PanelSeparator { foreground: root.foreground }

          Row {
            width: parent.width
            spacing: Style.space(8)

            PanelSectionHeader {
              anchors.verticalCenter: parent.verticalCenter
              text: "REMOTE"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Item {
              width: Math.max(0, parent.width - parent.children[0].implicitWidth - remoteStateBadge.implicitWidth - parent.spacing * 2)
              height: 1
            }

            Row {
              id: remoteStateBadge
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(5)

              Rectangle {
                anchors.verticalCenter: parent.verticalCenter
                width: Style.space(7)
                height: width
                radius: width / 2
                color: root.svc && root.svc.remoteEnabled ? root.accent : root.dim
              }

              Text {
                text: root.svc && root.svc.remoteEnabled ? "ON" : "OFF"
                color: root.svc && root.svc.remoteEnabled ? root.accent : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
              }
            }
          }

          Toggle {
            id: remoteToggle
            width: parent.width
            label: "Remote access"
            description: root.remoteStatusText()
            checked: root.svc ? root.svc.remoteEnabled : false
            foreground: checked ? root.accent : root.foreground
            accent: root.accent
            fontFamily: root.fontFamily
            onClicked: if (root.svc) root.svc.setRemoteEnabled(!root.svc.remoteEnabled)
          }

          Text {
            width: parent.width
            visible: root.remoteState !== "off"
            text: root.remoteStatusText()
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          Text {
            visible: root.remoteState === "off"
            width: parent.width
            text: "Turn on Remote access to serve Omarvis only inside your Tailscale network."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          CopyRow {
            visible: root.remoteUrl !== ""
            value: root.remoteUrl
            onActivated: root.copyText(value)
          }

          Text {
            visible: root.remoteUrl !== ""
            width: parent.width
            text: "Scanning the QR is preferred. Clicking the URL copies its secret into persistent clipboard history. The phone must be on your tailnet."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Item {
            visible: root.qrMatrix && root.qrMatrix.length > 0
            width: Math.min(parent.width, Style.space(240))
            height: width
            anchors.horizontalCenter: parent.horizontalCenter

            Canvas {
              id: qrCanvas
              anchors.fill: parent
              onPaint: {
                var ctx = getContext("2d")
                ctx.clearRect(0, 0, width, height)
                ctx.fillStyle = Color.popups.background
                ctx.fillRect(0, 0, width, height)
                var matrix = root.qrMatrix || []
                if (matrix.length === 0) return
                var rows = matrix.length
                var columns = matrix[0].length
                var quiet = 4
                var cell = Math.max(1, Math.floor(Math.min(width / (columns + quiet * 2), height / (rows + quiet * 2))))
                var offsetX = Math.floor((width - cell * columns) / 2)
                var offsetY = Math.floor((height - cell * rows) / 2)
                ctx.fillStyle = root.foreground
                for (var y = 0; y < rows; y++)
                  for (var x = 0; x < columns; x++)
                    if (matrix[y][x]) ctx.fillRect(offsetX + x * cell, offsetY + y * cell, cell, cell)
              }
            }
          }

          Row {
            visible: root.phoneSessionActive
            width: parent.width
            spacing: Style.space(8)

            Text {
              anchors.verticalCenter: parent.verticalCenter
              text: "Phone session live"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
            }

            Item { width: Math.max(0, parent.width - parent.children[0].implicitWidth - endPhoneButton.implicitWidth - parent.spacing * 2); height: 1 }

            Button {
              id: endPhoneButton
              text: "End phone session"
              foreground: root.urgent
              fontFamily: root.fontFamily
              onClicked: if (root.svc) root.svc.endPhoneSession()
            }
          }

          Button {
            visible: root.remoteState === "needs-tailscale"
            width: parent.width
            leftAlign: true
            text: "Open the Tailscale panel"
            foreground: root.foreground
            fontFamily: root.fontFamily
            onClicked: {
              root.close()
              Quickshell.execDetached(["omarchy-shell", "omarchy.tailscale", "open"])
            }
          }

          Text {
            visible: root.remoteState === "needs-operator"
            width: parent.width
            text: "Run in a terminal: sudo tailscale set --operator=" + Quickshell.env("USER")
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WrapAnywhere
          }

          Text {
            visible: root.remoteState.indexOf("serve-failed") === 0
            width: parent.width
            text: root.svc && root.svc.remoteError ? root.svc.remoteError : "Tailscale Serve failed"
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WrapAnywhere
          }
        }
      }
    }
  }

  component ExchangeText: Column {
    property string speaker: ""
    property string value: ""
    width: parent ? parent.width : implicitWidth
    spacing: Style.space(2)

    Text {
      text: parent.speaker
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
    }

    Text {
      width: parent.width
      text: parent.value
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
      wrapMode: Text.WordWrap
      maximumLineCount: 2
      elide: Text.ElideRight
    }
  }

  component CopyRow: CursorSurface {
    id: copyRow
    property string value: ""
    signal activated()
    width: parent ? parent.width : implicitWidth
    implicitHeight: copyValue.implicitHeight + Style.space(18)
    foreground: root.foreground
    bordered: true

    Text {
      id: copyValue
      anchors.left: parent.left
      anchors.right: copyIcon.left
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
      text: copyRow.value
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
      wrapMode: Text.WordWrap
      maximumLineCount: 2
      elide: Text.ElideRight
    }

    Text {
      id: copyIcon
      anchors.right: parent.right
      anchors.rightMargin: Style.space(10)
      anchors.verticalCenter: parent.verticalCenter
      text: String.fromCodePoint(0xF018F)
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.icon
    }

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: copyRow.activated()
    }
  }
}
