pragma ComponentBehavior: Bound
import QtQuick
import Quickshell
import qs.Ui
import qs.Commons

// Bar row plus dropdown, built on the stock power-panel anatomy: a hero, one
// action, a separator, and one settings section. There is no mode choice —
// the agent call is the only session type — and no scrolling, so the panel
// never grows past a glance.
Panel {
  id: root
  moduleName: "io.github.eliasstravik.omarvis"
  manageIpc: false

  property var svc: null

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color accent: Color.accent
  readonly property color dim: Qt.darker(foreground, 1.4)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property string sessionState: svc ? svc.sessionState : "idle"
  readonly property string dictationState: svc ? svc.dictationState : "idle"
  readonly property bool dictationLocked: svc ? svc.dictationLocked : false
  readonly property string runningCommand: svc ? svc.runningCommand : ""
  readonly property bool phoneSessionActive: svc ? svc.phoneSessionActive : false
  readonly property string phoneRunningCommand: svc ? svc.phoneRunningCommand : ""
  readonly property bool remoteEnabled: svc ? svc.remoteEnabled : false
  readonly property string remoteState: svc ? svc.remoteState : "off"
  readonly property string remoteUrl: svc ? svc.remoteUrl : ""
  readonly property var qrMatrix: svc ? svc.qrMatrix : []

  readonly property bool hasLocalError: sessionState === "error" || dictationState === "error"
  readonly property bool localSessionLive: sessionState !== "idle" && sessionState !== "error"
  // A call that has not finished connecting yet: the pulse states.
  readonly property bool connecting: sessionState === "starting" || sessionState === "stopping"
  readonly property bool localActivity: hasLocalError || runningCommand !== ""
    || dictationState !== "idle" || localSessionLive

  // One state vocabulary across the bar glyph, this panel, the HUD, and the
  // phone page: pulse means wait, a solid microphone means talk, a static
  // alert means act.
  readonly property string displayState: {
    if (hasLocalError) return "Needs attention"
    if (runningCommand) return "Running"
    if (dictationState === "recording") return dictationLocked ? "Dictating hands-free" : "Dictating"
    if (dictationState !== "idle") return dictationState
    if (sessionState === "starting") return "Connecting"
    if (sessionState === "stopping") return "Ending"
    if (localSessionLive) return sessionState
    if (phoneRunningCommand) return "Phone command"
    if (phoneSessionActive) return "Phone"
    return "Idle"
  }
  // Four glyphs only — hourglass for any waiting or busywork, microphone
  // when the floor is yours, speaker for the agent's voice, alert for
  // failure — because a strip that changes pictures mid-conversation reads
  // as glitchy, not informative. Color carries the rest: attention color for
  // a hot local mic, accent for agent-side or phone-side activity.
  readonly property string glyphText: {
    if (hasLocalError) return String.fromCodePoint(0xF0026)
    if (dictationState === "recording") return String.fromCodePoint(0xF036C)
    if (dictationState === "transcribing") return String.fromCodePoint(0xF051F)
    if (sessionState === "starting" || sessionState === "stopping")
      return String.fromCodePoint(0xF051F)
    if (sessionState === "speaking") return String.fromCodePoint(0xF057E)
    // Working — thinking or a running command, before it speaks — is one
    // generic hourglass, never per-tool glyphs.
    if (sessionState === "thinking" || runningCommand) return String.fromCodePoint(0xF051F)
    if (localSessionLive) return String.fromCodePoint(0xF036C)
    if (phoneRunningCommand) return String.fromCodePoint(0xF051F)
    // A live phone call gets its own mark — the in-talk handset — so the
    // desk glyph can never be mistaken for local listening or speaking.
    if (phoneSessionActive) return String.fromCodePoint(0xF03F6)
    return String.fromCodePoint(0xF036C)
  }
  readonly property color glyphColor: {
    if (hasLocalError) return urgent
    // Dictation wears the theme accent; the attention color is reserved for
    // a hot agent-call microphone and for a live phone session.
    if (dictationState === "recording") return accent
    if (sessionState === "listening") return Color.bar.active
    if (phoneSessionActive || phoneRunningCommand !== "") return Color.bar.active
    if (sessionState === "speaking" || sessionState === "thinking" || runningCommand !== "") return accent
    return foreground
  }

  // The one short line the panel is allowed to show for a broken remote
  // service. The full text goes to the notification server via Service.qml.
  readonly property string remoteProblem: {
    if (remoteState === "needs-tailscale") return "Tailscale is not connected"
    if (remoteState === "needs-operator") return "Tailscale operator access required"
    if (remoteState.indexOf("serve-failed") === 0) return "Tailscale Serve failed"
    return ""
  }

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

  function toggleSession() {
    var service = root.resolveService()
    if (!service) return
    if (root.localSessionLive) service.stop()
    else service.start()
  }

  function copyText(text) {
    var value = String(text || "")
    if (value !== "") Quickshell.execDetached(["wl-copy", "--", value])
  }

  onOpenedChanged: syncPanelOpen()
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

  Connections {
    target: root.svc
    function onPanelRequested() { root.open() }
  }

  // The bar row is the glyph. The state word is an off-by-default setting for
  // people who want it, never chrome the button carries on its own.
  Row {
    id: barRow
    height: button.implicitHeight
    spacing: Style.space(6)

    BarIconButton {
      id: button
      bar: root.bar
      text: root.glyphText
      foreground: root.glyphColor
      // No hover card: the glyph is the whole story, and the panel is one
      // click away for anything more.
      // The waiting glyph pulses in the bar exactly as it does on the HUD and
      // the phone, so all three read as the same beat.
      opacity: root.connecting ? barPulse.value : 1.0
      onPressed: function(buttonCode) {
        // D5: only left click owns panel behavior. Right and middle click are
        // intentionally inert.
        if (buttonCode === Qt.LeftButton) root.toggle()
      }
    }

    Text {
      visible: root.setting("showLabel", false)
      anchors.verticalCenter: parent.verticalCenter
      text: root.displayState
      textFormat: Text.PlainText
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall

      MouseArea {
        anchors.fill: parent
        cursorShape: Qt.PointingHandCursor
        onClicked: root.toggle()
      }
    }
  }

  // Shared 950ms InOutSine wait pulse — the Omarchy charging-bar idiom.
  QtObject {
    id: barPulse
    property real value: 1.0
  }

  SequentialAnimation {
    running: root.connecting
    loops: Animation.Infinite
    alwaysRunToEnd: true
    onStopped: barPulse.value = 1.0
    NumberAnimation { target: barPulse; property: "value"; from: 1.0; to: 0.45; duration: 950; easing.type: Easing.InOutSine }
    NumberAnimation { target: barPulse; property: "value"; from: 0.45; to: 1.0; duration: 950; easing.type: Easing.InOutSine }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(panelColumn.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: panelColumn
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(14)

        // ---------- Hero: state glyph · Omarvis · status caption ----------
        PanelHero {
          width: parent.width
          title: "Omarvis"
          meta: root.displayState
          foreground: root.foreground
          fontFamily: root.fontFamily
          iconOpacity: root.connecting ? barPulse.value : 1.0
          iconComponent: Component {
            Text {
              text: root.glyphText
              color: root.glyphColor
              font.family: root.fontFamily
              font.pixelSize: Style.font.display
            }
          }
          // The single action rides the hero line itself. "Talk" matches the
          // phone page's button, so both surfaces speak the same pair.
          trailingControl: Component {
            Button {
              bordered: true
              text: root.localSessionLive ? "End" : "Talk"
              iconText: root.localSessionLive
                ? String.fromCodePoint(0xF04DB)
                : String.fromCodePoint(0xF036C)
              foreground: root.localSessionLive ? root.urgent : root.foreground
              fontFamily: root.fontFamily
              onClicked: root.toggleSession()
            }
          }
        }

        PanelSeparator { foreground: root.foreground }

        // ---------- Keybindings: the user's real mappings, live ----------
        Column {
          width: parent.width
          spacing: Style.space(8)
          visible: keybindingRows.count > 0

          PanelSectionHeader {
            text: "KEYBINDINGS"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Repeater {
            id: keybindingRows
            model: root.svc ? root.svc.keybindings : []

            Row {
              id: keybindingRow
              required property var modelData
              width: parent.width

              Text {
                text: keybindingRow.modelData.label
                textFormat: Text.PlainText
                color: root.foreground
                opacity: 0.6
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }

              Item {
                width: Math.max(0, parent.width - parent.children[0].implicitWidth - parent.children[2].implicitWidth)
                height: 1
              }

              Text {
                text: keybindingRow.modelData.keys
                textFormat: Text.PlainText
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }
            }
          }
        }

        PanelSeparator { foreground: root.foreground }

        // ---------- Remote phone access ----------
        Column {
          width: parent.width
          spacing: Style.space(10)

          PanelSectionHeader {
            text: "REMOTE"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          // A plain label with the bare switch as the only button: no row
          // fill, no row border, no row click. The switch alone carries the
          // on-state in accent.
          Row {
            width: parent.width

            Text {
              anchors.verticalCenter: parent.verticalCenter
              text: "Remote access"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.subtitle
              font.bold: true
            }

            Item {
              width: Math.max(0, parent.width - parent.children[0].implicitWidth - remoteSwitch.implicitWidth)
              height: 1
            }

            ToggleSwitch {
              id: remoteSwitch
              anchors.verticalCenter: parent.verticalCenter
              checked: root.remoteEnabled
              foreground: root.foreground
              accent: root.accent
              onToggled: if (root.svc) root.svc.setRemoteEnabled(!root.remoteEnabled)
            }
          }

          // Pairing is a QR to scan, never a URL to read: the secret is
          // terminal access and copied URLs persist in clipboard history.
          // The code takes the panel's full width; the copy affordance is a
          // plain labeled button below it.
          Column {
            visible: root.remoteEnabled && !root.phoneSessionActive
              && root.qrMatrix && root.qrMatrix.length > 0
            width: parent.width
            spacing: Style.space(10)

            Item {
              id: qrSlot
              width: parent.width
              height: width

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

            Button {
              id: copyButton
              // Standard confirmation beat: the label itself says "Copied"
              // for a moment, then reverts. No extra chrome.
              property bool copied: false
              width: parent.width
              bordered: true
              text: copied ? "Copied" : "Copy link"
              iconText: String.fromCodePoint(copied ? 0xF012C : 0xF018F)
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: {
                root.copyText(root.remoteUrl)
                copied = true
                copiedTimer.restart()
              }

              Timer {
                id: copiedTimer
                interval: 1500
                onTriggered: copyButton.copied = false
              }
            }
          }

          Row {
            visible: root.phoneSessionActive
            width: parent.width
            spacing: Style.space(8)

            PanelSectionHeader {
              anchors.verticalCenter: parent.verticalCenter
              text: "PHONE CONNECTED"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Item {
              width: Math.max(0, parent.width - parent.children[0].implicitWidth - endPhoneButton.implicitWidth - parent.spacing * 2)
              height: 1
            }

            Button {
              id: endPhoneButton
              anchors.verticalCenter: parent.verticalCenter
              bordered: true
              text: "End"
              iconText: String.fromCodePoint(0xF04DB)
              foreground: root.urgent
              fontFamily: root.fontFamily
              onClicked: if (root.svc) root.svc.endPhoneSession()
            }
          }

          // One line only. The repair instructions live in the notification
          // Service.qml raises, where long text can actually be read.
          Text {
            visible: root.remoteProblem !== ""
            width: parent.width
            text: root.remoteProblem
            textFormat: Text.PlainText
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            elide: Text.ElideRight
          }
        }
      }
    }
  }
}
