import QtQuick
import Quickshell
import Quickshell.Wayland
import qs.Commons
import qs.Ui

// Text-free voice strip, a sibling of the volume OSD: one state glyph and
// one amplitude bar carry the whole story — who holds the floor (mic vs
// speaker glyph, foreground vs accent fill), whether the mic is hot (the
// bar.active attention color), and hands-free mode (the border goes to the
// attention color, the same border-carries-state idiom the lock screen and
// polkit use). Conversation and command text are deliberately absent: audio
// is the channel and errors go to the notification server.
PanelWindow {
  id: hud

  property var service: null
  property var shell: null
  property string hudPosition: "top-center"
  property bool speakingFallback: false

  readonly property bool dictating: service && service.dictationState !== "idle"
  // Session errors live in a desktop notification, not on the strip.
  readonly property bool hudVisible: service
    && ((service.sessionState !== "idle" && service.sessionState !== "error")
      || service.dictationState !== "idle")
  readonly property real userLevel: !service ? 0.0 : (dictating ? service.dictationLevel : service.inLevel)
  readonly property string visualState: {
    if (!service) return "idle"
    if (service.dictationState !== "idle") return service.dictationState
    if (service.sessionState === "speaking" && speakingFallback) return "listening"
    return service.sessionState
  }
  readonly property bool errorState: visualState === "error"
  readonly property bool handsFree: service && service.dictationLocked === true
  // "Working": the agent has your words and is doing something — thinking
  // or running a command — but has not started speaking yet. One generic
  // hourglass, never per-tool glyphs or success flashes. A finished call
  // simply disappears — no goodbye beat.
  readonly property bool working: visualState === "thinking"
    || (service && service.runningCommand.length > 0 && visualState !== "speaking")
  // The websocket handshake can take a dozen seconds, and during it the
  // microphone is emphatically not open. Waiting is its own state with its
  // own glyph and its own beat: pulse means wait, a solid mic means talk.
  readonly property bool waitingToConnect: visualState === "starting"

  // Three glyphs, same as the bar and the phone: hourglass for any waiting
  // or busywork (connecting, transcribing, working), microphone when the
  // floor is yours, speaker for the agent's voice — plus the alert mark for
  // failure. Nothing else, and a finished call simply disappears.
  readonly property string stateGlyph: {
    if (visualState === "error") return String.fromCodePoint(0xF0026)
    if (visualState === "transcribing") return String.fromCodePoint(0xF051F)
    // The hourglass, never the microphone: the mic glyph appears only once
    // the session is live and the floor is actually yours.
    if (waitingToConnect) return String.fromCodePoint(0xF051F)
    if (visualState === "speaking") return String.fromCodePoint(0xF057E)
    if (working) return String.fromCodePoint(0xF051F)
    return String.fromCodePoint(0xF036C)
  }
  readonly property color stateGlyphColor: {
    if (errorState) return Color.urgent
    // Agent-side activity is accent: its voice and its working hourglass.
    // Dictation wears the accent too — the theme's primary — while the
    // bar.active attention color is reserved for the agent call's hot mic.
    if (visualState === "speaking" || visualState === "recording" || working) return Color.accent
    if (visualState === "listening") return Color.bar.active
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
    }
    function onOutLevelChanged() {
      if (hud.service && hud.service.outLevel >= 0.02) hud.speakingFallback = false
    }
  }

  Timer {
    id: speakingSilenceTimer
    interval: 1500
    repeat: false
    running: hud.visible && hud.service && hud.service.sessionState === "speaking" && hud.service.outLevel < 0.02
    onTriggered: hud.speakingFallback = true
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
    // Border carries mode: urgent on error; a full accent frame while the
    // mic is locked open hands-free; Omarchy's own unfocused-window border
    // during hold-to-talk (same 2px geometry — "momentary" reads exactly
    // like "unfocused" does on every window); the theme popups border for
    // agent calls.
    borderSpec: hud.errorState
      ? Border.flat(Color.urgent, Math.max(1, Style.space(2)))
      : (hud.handsFree
        ? Border.flat(Color.accent, Math.max(1, Style.space(2)))
        : (hud.dictating
          ? Border.flat(hud.service ? hud.service.inactiveBorderColor : Util.alpha(Color.popups.text, 0.35), Math.max(1, Style.space(2)))
          : Border.surfaceSpec("popups", "border", Color.popups.border, Math.max(1, Style.space(2)))))

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
          font.family: Style.font.family
          font.pixelSize: Style.font.iconLarge

          // The shell's wait pulse: 950ms InOutSine, alwaysRunToEnd so the
          // glyph lands back at full opacity the moment the call connects.
          SequentialAnimation on opacity {
            running: hud.waitingToConnect && hud.visible
            loops: Animation.Infinite
            alwaysRunToEnd: true
            NumberAnimation { from: 1.0; to: 0.35; duration: 950; easing.type: Easing.InOutSine }
            NumberAnimation { from: 0.35; to: 1.0; duration: 950; easing.type: Easing.InOutSine }
          }
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
          objectName: "omarvisLevelFill"
          // A dead zero-length meter would read as a dead microphone while
          // the call is still connecting, so the amplitude fill yields to the
          // indeterminate sweep until there is real audio to show.
          visible: !hud.waitingToConnect
          height: parent.height
          width: parent.width * hud.meterLevel
          color: hud.visualState === "speaking" ? Color.accent : Color.popups.text

          Behavior on width {
            enabled: hud.visible
            NumberAnimation { duration: 140; easing.type: Easing.OutCubic }
          }
        }

        Rectangle {
          id: meterSweep
          objectName: "omarvisMeterSweep"
          visible: hud.waitingToConnect
          height: parent.height
          width: Math.round(parent.width / 3)
          color: Color.accent

          SequentialAnimation on x {
            running: meterSweep.visible && hud.visible
            loops: Animation.Infinite
            alwaysRunToEnd: true
            NumberAnimation { from: 0; to: meter.width - meterSweep.width; duration: 950; easing.type: Easing.InOutSine }
            NumberAnimation { from: meter.width - meterSweep.width; to: 0; duration: 950; easing.type: Easing.InOutSine }
          }
        }
      }
    }
  }
}
