import QtQuick
import Quickshell
import Quickshell.Wayland
import qs.Commons
import qs.Ui

// Text-free voice strip, a sibling of the volume OSD: one state glyph and
// one amplitude bar carry the whole story — who holds the floor (mic vs
// speaker glyph, foreground vs accent fill), whether the mic is hot (the
// bar.active attention color), a running command (cog in the glyph slot,
// then a ✓ flash), and hands-free mode (the border goes to the attention
// color, the same border-carries-state idiom the lock screen and polkit
// use). Conversation and command text are deliberately absent: audio is the
// channel, errors go to the notification server, and the bar widget's
// tooltip carries the text on demand.
PanelWindow {
  id: hud

  property var service: null
  property var shell: null
  property string hudPosition: "top-center"
  property bool speakingFallback: false
  property bool toolSucceeded: false
  // A call that just ended gets a short goodbye beat: the strip lingers
  // with a waving hand before disappearing.
  property bool waving: false
  property string lastSessionState: "idle"

  readonly property bool dictating: service && service.dictationState !== "idle"
  // Session errors live in a desktop notification, not on the strip.
  readonly property bool hudVisible: waving || (service
    && ((service.sessionState !== "idle" && service.sessionState !== "error")
      || service.dictationState !== "idle"))
  readonly property real userLevel: !service ? 0.0 : (dictating ? service.dictationLevel : service.inLevel)
  readonly property string visualState: {
    if (!service) return "idle"
    if (service.dictationState !== "idle") return service.dictationState
    if (service.sessionState === "speaking" && speakingFallback) return "listening"
    return service.sessionState
  }
  readonly property bool errorState: visualState === "error"
  readonly property bool toolRunning: service && service.runningCommand.length > 0
  readonly property bool handsFree: service && service.dictationLocked === true

  // Same glyph vocabulary as BarWidget (and the stock voxtype indicator for
  // the dictation states), so bar and HUD read as one module. A running
  // command is just another state, so it lives in the same slot: cog while
  // executing, a short ✓ flash when done.
  // Iconic actions get their own glyph while running; everything else is
  // the wrench. Deliberately tiny — a glyph vocabulary only stays readable
  // if it is small.
  function toolGlyphFor(command) {
    var cmd = String(command || "")
    if (cmd.indexOf("screenshot") !== -1 || cmd.indexOf("screenrecord") !== -1
      || cmd.indexOf("omarchy capture") !== -1 || cmd.indexOf("omarchy-capture") !== -1)
      return String.fromCodePoint(0xF0100)
    if (cmd.indexOf("theme") !== -1) return String.fromCodePoint(0xF03D8)
    if (cmd.indexOf("omarchy system") !== -1 || cmd.indexOf("omarchy-system") !== -1)
      return String.fromCodePoint(0xF0425)
    return String.fromCodePoint(0xF05B7)
  }

  readonly property string stateGlyph: {
    if (visualState === "error") return String.fromCodePoint(0xF0026)
    if (waving && visualState === "idle") return String.fromCodePoint(0xF1821)
    if (toolSucceeded) return "✓"
    if (toolRunning) return toolGlyphFor(service.runningCommand)
    if (visualState === "transcribing") return String.fromCodePoint(0xF06D7)
    if (visualState === "thinking") return String.fromCodePoint(0xF051F)
    if (visualState === "speaking") return String.fromCodePoint(0xF057E)
    return String.fromCodePoint(0xF036C)
  }
  readonly property color stateGlyphColor: {
    if (errorState) return Color.urgent
    if (waving && visualState === "idle") return Color.accent
    // Agent-side activity is accent: its voice, its commands.
    if (toolSucceeded || toolRunning || visualState === "speaking") return Color.accent
    // Hot microphone: the bar.active attention color, per its shell.toml
    // charter ("recording, voxtype, alerts, updates").
    if (visualState === "listening" || visualState === "recording") return Color.bar.active
    return Color.popups.text
  }

  // The bar borrows the OSD volume-bar idiom: fill tracks live amplitude —
  // yours in foreground, the agent's in accent — so motion only ever means
  // sound. Mapped through dBFS (-60dB empty to -20dB full) because raw
  // speech RMS is a few percent of full scale and would barely register;
  // the -20dB ceiling keeps ordinary speaking volume in the upper half of
  // the bar rather than reserving it for shouting.
  function meterFill(level) {
    if (level <= 0) return 0
    var db = 20 * Math.log(level) / Math.LN10
    return Math.max(0, Math.min(1, (db + 60) / 40))
  }
  readonly property real meterLevel: meterFill(visualState === "speaking" && service ? service.outLevel : userLevel)

  // Clear the bar edge exactly like the notification popups do, falling back
  // to the default bar size when shell.bar isn't reachable (test harness).
  readonly property string barPosition: shell && shell.barConfig ? String(shell.barConfig.position || "top") : "top"
  readonly property bool barVertical: barPosition === "left" || barPosition === "right"
  readonly property int defaultBarSize: barVertical ? Style.bar.sizeVertical : Style.bar.sizeHorizontal
  readonly property int liveBarSize: shell && shell.bar && !shell.bar.barHidden ? Math.max(0, shell.bar.barSize) : defaultBarSize
  readonly property int topClearance: (barPosition === "top" ? liveBarSize : 0) + Style.gapsOut
  readonly property int rightClearance: (barPosition === "right" ? liveBarSize : 0) + Style.gapsOut

  function capture(path) {
    card.grabToImage(function(result) { result.saveToFile(path) })
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
      var state = hud.service.sessionState
      if (state === "idle"
        && (hud.lastSessionState === "listening"
          || hud.lastSessionState === "speaking"
          || hud.lastSessionState === "thinking")) {
        hud.waving = true
        waveTimer.restart()
      }
      hud.lastSessionState = state
    }
    function onOutLevelChanged() {
      if (hud.service && hud.service.outLevel >= 0.02) hud.speakingFallback = false
    }
    function onRunningCommandChanged() {
      if (hud.service && hud.service.runningCommand) {
        hud.toolSucceeded = false
        toolDoneTimer.stop()
      }
    }
    function onCommandRan(command) {
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
    onTriggered: hud.toolSucceeded = false
  }

  Timer {
    id: waveTimer
    interval: 900
    repeat: false
    onTriggered: hud.waving = false
  }

  BorderSurface {
    id: card
    objectName: "omarvisHudCard"

    readonly property int padX: Style.space(16)
    readonly property int padY: Style.space(10)

    // Fixed geometry across every state: one glyph slot, one bar. The strip
    // never lurches — the only thing that ever moves is the amplitude fill.
    width: card.borderLeft + card.padX
      + glyphSlot.width + Style.space(12) + meter.width
      + card.padX + card.borderRight
    height: card.borderTop + card.padY + Style.space(22) + card.padY + card.borderBottom
    x: hud.hudPosition === "top-right"
      ? parent.width - width - hud.rightClearance
      : Math.round((parent.width - width) / 2)
    y: hud.topClearance
    radius: Style.cornerRadius
    color: Util.alpha(Color.popups.background, 0.97)
    // Border carries mode: urgent on error, the attention color while the
    // mic is locked open hands-free, the theme popups border otherwise.
    borderSpec: hud.errorState
      ? Border.flat(Color.urgent, Math.max(1, Style.space(2)))
      : (hud.handsFree
        ? Border.flat(Color.bar.active, Math.max(1, Style.space(2)))
        : Border.surfaceSpec("popups", "border", Color.popups.border, Math.max(1, Style.space(2))))

    Behavior on color { ColorAnimation { duration: 420; easing.type: Easing.InOutCubic } }

    Row {
      anchors.fill: parent
      anchors.topMargin: card.borderTop + card.padY
      anchors.rightMargin: card.borderRight + card.padX
      anchors.bottomMargin: card.borderBottom + card.padY
      anchors.leftMargin: card.borderLeft + card.padX
      spacing: Style.space(12)

      Item {
        id: glyphSlot
        width: Style.space(22)
        height: parent.height

        Text {
          id: stateGlyphText
          objectName: "omarvisStateGlyph"
          anchors.centerIn: parent
          text: hud.stateGlyph
          color: hud.stateGlyphColor
          opacity: hud.visualState === "starting" ? 0.45 : 1.0
          font.family: Style.font.family
          font.pixelSize: Style.font.iconLarge
        }
      }

      Rectangle {
        id: meter
        objectName: "omarvisLevelMeter"
        width: Style.space(142)
        height: Math.max(Style.space(6), Style.spacing.sm)
        anchors.verticalCenter: parent.verticalCenter
        color: Util.alpha(Color.popups.text, 0.45)

        Rectangle {
          height: parent.height
          width: parent.width * hud.meterLevel
          color: hud.visualState === "speaking" ? Color.accent : Color.popups.text

          Behavior on width {
            enabled: hud.visible
            NumberAnimation { duration: 140; easing.type: Easing.OutCubic }
          }
        }
      }
    }
  }
}
