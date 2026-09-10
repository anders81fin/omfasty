import QtQuick
import QtQuick.Controls
import Quickshell.Io
import qs.Ui
import qs.Commons

// Intermittent-fasting timer: bar pill shows elapsed/remaining time while a
// fast is running (or the eating-window countup once it ends), popup has
// start/stop, fasting:eating ratio presets, a progress bar, a playful
// physiology-stage commentary, and a short streak/history readout. State
// lives outside the shell in fasting-cli.py (~/.local/state/omarchy-fasting/),
// so it survives shell restarts and stays simple to inspect/edit by hand.
Panel {
  id: root
  moduleName: "anders81fin.omfasty"
  ipcTarget: "anders81fin.omfasty"

  // Fasting:eating ratio presets. All but the joke entry sum to 24h, so the
  // eating-window target is simply derived as 24 - targetHours.
  readonly property var presets: [
    { hours: 14, label: "14:10" },
    { hours: 16, label: "16:8" },
    { hours: 18, label: "18:6" },
    { hours: 20, label: "20:4" },
    { hours: 22, label: "22:2" },
    { hours: 24, label: "UMAD?" }
  ]

  // Font Awesome glyphs from the bar's nerd-font. Plain glyphs (not emoji)
  // so they paint in the theme foreground/accent like every other bar icon.
  readonly property string iconIdle: ""       // utensils
  readonly property string iconDone: ""       // check
  readonly property string iconHistoryPartial: "" // circle (dot)
  // Base glyph under the icon stack: eating-window utensils normally, the
  // checkmark once a fast hits its target. A diagonal strike draws on top of
  // this (never the checkmark) whenever a fast is actively running.
  readonly property string baseIcon: targetReached ? iconDone : iconIdle
  readonly property bool showBan: fasting && !targetReached

  property bool fasting: false
  property int startedAt: 0
  property real targetHours: 16
  property int streak: 0
  property var history: []
  property var longestHistory: []
  property int lastEnd: 0
  property int nowEpoch: Math.floor(Date.now() / 1000)

  // Highest whole-hour mark already notified for, so the hourly nudge
  // fires once per boundary instead of once per second. Reset whenever a
  // new fast or eating window starts, seeded to the current progress (not
  // 0) so resuming after a shell restart doesn't replay every hour missed
  // while nothing was watching.
  property int lastNotifiedFastHour: 0
  property int lastNotifiedEatingHour: 0

  readonly property real elapsedHours: fasting ? Math.max(0, (nowEpoch - startedAt) / 3600) : 0
  readonly property real progressFraction: targetHours > 0 ? Math.min(1, elapsedHours / targetHours) : 0
  readonly property bool targetReached: fasting && elapsedHours >= targetHours

  readonly property real eatingTargetHours: Math.max(0, 24 - targetHours)
  readonly property bool hasEatingHistory: !fasting && lastEnd > 0
  readonly property real eatingElapsedHours: hasEatingHistory ? Math.max(0, (nowEpoch - lastEnd) / 3600) : 0

  function formatHm(hours) {
    var totalMinutes = Math.max(0, Math.round(hours * 60))
    var h = Math.floor(totalMinutes / 60)
    var m = totalMinutes % 60
    return h + ":" + (m < 10 ? "0" : "") + m
  }

  // Tongue-in-cheek physiology timeline. Hour thresholds are the usual
  // rough IF folklore, not medical advice — this is a bar widget, not a lab.
  function fastingStage(hours) {
    if (hours < 4) return { title: "Fresh from the table", blurb: "blood sugar's still up from that last meal" }
    if (hours < 8) return { title: "Glycogen cruise control", blurb: "burning through the liver's sugar stash" }
    if (hours < 12) return { title: "Tank running low", blurb: "glycogen reserves are thinning out" }
    if (hours < 18) return { title: "Metabolic switch flipping", blurb: "body's starting to eye the fat reserves" }
    if (hours < 24) return { title: "Ketosis kicking in", blurb: "fat's becoming the fuel of choice" }
    if (hours < 36) return { title: "Deep ketosis", blurb: "you're basically a furnace now" }
    if (hours < 48) return { title: "Autophagy o'clock", blurb: "cells are grabbing their brooms" }
    return { title: "Legendary autophagy", blurb: "full cellular renovation crew, hard hats on" }
  }

  function eatingStage(hoursElapsed, targetHrs) {
    if (targetHrs <= 0) return { title: "Zero eating window", blurb: "UMAD picked the nuclear option, respect" }
    var remaining = targetHrs - hoursElapsed
    if (remaining > targetHrs * 0.5) return { title: "Fueling up", blurb: "eat well, the fast comes back around" }
    if (remaining > 0) return { title: "Window closing soon", blurb: "last orders, plan that final bite" }
    return { title: "Into overtime", blurb: "window's technically closed, no judgment" }
  }

  // Hourly desktop-notification flavor text — same tongue-in-cheek tone as
  // the stage commentary above, just short enough for a notification body.
  function fastHourMessages(hours) {
    var remaining = targetHours - hours
    return [
      "Hour " + hours + " down. Willpower: still fully charged.",
      hours + "h fasted — glycogen's quietly packing its bags.",
      "Still going at " + hours + "h. Future you says thanks.",
      hours + (hours === 1 ? " hour" : " hours") + " in. Hunger's just a suggestion at this point.",
      "Clocked " + hours + "h fasting. Cells are taking notes for the cleanup crew.",
      remaining > 0
        ? hours + "h down, " + remaining + "h to go. Onward."
        : hours + "h down, past the target and still going. Onward."
    ]
  }

  function eatingHourMessages(hours) {
    return [
      hours + "h into the eating window — get some food in before the fast's back on.",
      "Eating window: " + hours + "h used. The clock's ticking toward the next fast.",
      hours + "h in — refuel now, the fast doesn't wait around.",
      "Still got food on the table? " + hours + "h into the window already.",
      hours + "h eating so far. Don't let the window close on an empty plate."
    ]
  }

  function randomOf(list) {
    return list[Math.floor(Math.random() * list.length)]
  }

  // Routed through the CLI rather than calling notify-send directly. The bar
  // is instantiated once per monitor, so on a multi-monitor desktop every copy
  // of this widget reaches the same whole hour at the same moment; the CLI
  // claims the hour so only one of them actually sends anything.
  function notify(kind, hour, title, body) {
    notifyProc.command = [root.scriptPath(), "nudge", kind, String(hour), title, body]
    notifyProc.running = true
  }

  readonly property string heroTitle: fasting ? "Fasting" : (hasEatingHistory ? "Eating window" : "Ready when you are")

  readonly property string heroStats: fasting
    ? (formatHm(elapsedHours) + " / " + targetHours + "h")
    : (hasEatingHistory ? (formatHm(eatingElapsedHours) + " / " + formatHm(eatingTargetHours) + "h") : "")

  readonly property string heroBlurb: {
    if (fasting) {
      var fs = fastingStage(elapsedHours)
      return (targetReached ? "Target smashed! " : "") + fs.title + " — " + fs.blurb + "."
    }
    if (hasEatingHistory) {
      var es = eatingStage(eatingElapsedHours, eatingTargetHours)
      return es.title + " — " + es.blurb + (streak > 0 ? (". Streak: " + streak + ".") : ".")
    }
    return "Pick a ratio below and let's go."
  }

  function scriptPath() {
    return Qt.resolvedUrl("fasting-cli.py").toString().replace("file://", "")
  }

  function refresh() {
    if (!statusProc.running) statusProc.running = true
  }

  function applyStatus(json) {
    var data
    try { data = JSON.parse(json) } catch (e) { return }
    if (!data) return
    root.fasting = !!data.fasting
    root.startedAt = data.startedAt || 0
    root.targetHours = data.targetHours || 16
    root.streak = data.streak || 0
    root.lastEnd = data.lastEnd || 0
    root.history = data.history || []
    root.longestHistory = data.longestHistory || []
    root.nowEpoch = Math.floor(Date.now() / 1000)
  }

  function start(hours) {
    startProc.command = [root.scriptPath(), "start", String(hours)]
    startProc.running = true
  }

  function setTarget(hours) {
    startProc.command = [root.scriptPath(), "target", String(hours)]
    startProc.running = true
  }

  function stop() {
    startProc.command = [root.scriptPath(), "stop"]
    startProc.running = true
  }

  Component.onCompleted: refresh()

  onOpenedChanged: if (opened) refresh()

  // Seed the "already notified up to here" marks to the current progress
  // rather than 0 whenever a fresh fast/eating-window shows up in a status
  // reply (fast just started, fast just ended, or the shell just restarted
  // mid-fast) — otherwise the first tick after that would fire one nudge
  // per hour already elapsed.
  onStartedAtChanged: lastNotifiedFastHour = fasting ? Math.floor(elapsedHours) : 0
  onLastEndChanged: lastNotifiedEatingHour = hasEatingHistory ? Math.floor(eatingElapsedHours) : 0

  // Live 1s tick drives both the fasting countup and the eating-window
  // countup; the CLI round-trip only happens on open, on actions, and every
  // 60s in case another surface (e.g. a terminal) changed the state file
  // underneath the shell. Piggybacks the hourly notification check since
  // both need the same up-to-date clock.
  Timer {
    interval: 1000
    running: true
    repeat: true
    onTriggered: {
      root.nowEpoch = Math.floor(Date.now() / 1000)

      if (root.fasting) {
        var fh = Math.floor(root.elapsedHours)
        if (fh > root.lastNotifiedFastHour) {
          root.lastNotifiedFastHour = fh
          root.notify("fast", fh, "omfasty — " + fh + "h fasting", root.randomOf(root.fastHourMessages(fh)))
        }
      } else if (root.hasEatingHistory) {
        var eh = Math.floor(root.eatingElapsedHours)
        if (eh > root.lastNotifiedEatingHour) {
          root.lastNotifiedEatingHour = eh
          root.notify("eating", eh, "omfasty — " + eh + "h eating", root.randomOf(root.eatingHourMessages(eh)))
        }
      }
    }
  }

  Timer {
    interval: 60000
    running: true
    repeat: true
    onTriggered: root.refresh()
  }

  Process {
    id: statusProc
    command: [root.scriptPath(), "status"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyStatus(text)
    }
  }

  Process {
    id: startProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyStatus(text)
    }
  }

  // Fire-and-forget hourly nudge. Silently a no-op if notify-send (or a
  // notification daemon to receive it) isn't installed — never blocks the
  // timer or the rest of the widget.
  Process {
    id: notifyProc
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // Plain WidgetButton (not BarIconButton) because this pill's content is
  // variable-width (icon + elapsed time) and the icon itself is a two-layer
  // stack (utensils + prohibition ring while fasting), not a single glyph
  // in a fixed-slot font button. WidgetButton still owns hover/tooltip/click
  // handling and click-target registration; its own label is hidden and
  // fixedWidth mirrors the custom content's measured width instead, which is
  // what keeps this from overlapping the next bar widget as the elapsed time
  // grows.
  WidgetButton {
    id: button
    bar: root.bar
    labelVisible: false
    text: ""
    hasVisualContent: true
    fixedWidth: contentRow.implicitWidth + scaledHorizontalMargin * 2
    useActiveColor: true
    activeColor: Color.accent
    active: root.targetReached
    tooltipText: root.fasting
      ? (root.formatHm(root.elapsedHours) + " / " + root.targetHours + "h — " + root.fastingStage(root.elapsedHours).title)
      : (root.hasEatingHistory
        ? (root.formatHm(root.eatingElapsedHours) + " / " + root.formatHm(root.eatingTargetHours) + "h eating window")
        : "No fast running")
    onPressed: function(b) { root.toggle() }

    Row {
      id: contentRow
      anchors.centerIn: parent
      spacing: Style.space(4)

      // Base glyph (utensils, or the checkmark once the target is hit) with
      // a single diagonal strike across it while a fast is actively running
      // — same utensils icon the eating-window state uses, just struck
      // through, so the two states read as one family. A rotated bar instead
      // of a circle-slash glyph: a full prohibition ring read as too busy at
      // bar-icon size.
      Item {
        width: baseGlyph.implicitWidth
        height: baseGlyph.implicitHeight
        anchors.verticalCenter: parent.verticalCenter

        Text {
          id: baseGlyph
          textFormat: Text.PlainText
          text: root.targetReached ? root.iconDone : root.iconIdle
          color: button.active && button.useActiveColor ? button.activeColor : button.foreground
          font.family: root.bar.fontFamily
          font.pixelSize: Style.bar.iconFont
        }

        Rectangle {
          visible: root.fasting && !root.targetReached
          anchors.centerIn: parent
          width: Math.max(parent.width, parent.height) * 1.25
          height: Math.max(2, Style.bar.iconFont * 0.16)
          radius: height / 2
          rotation: -45
          color: button.active && button.useActiveColor ? button.activeColor : button.foreground
        }
      }

      Text {
        visible: root.fasting || root.hasEatingHistory
        textFormat: Text.PlainText
        text: root.fasting ? root.formatHm(root.elapsedHours) : root.formatHm(root.eatingElapsedHours)
        color: button.active && button.useActiveColor ? button.activeColor : button.foreground
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.body
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
    contentWidth: panel.fittedContentWidth(Style.space(300))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(520))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()

      ScrollView {
        id: scrollArea
        anchors.fill: parent
        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: column.implicitHeight > height ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff

        Column {
          id: column
          // Bound to the viewport's own reported width (not just "parent.width")
          // so it matches what the scrollbar reservation actually leaves —
          // same pattern the first-party panels use. A width that's off by a
          // fraction here is what left preset-grid cells with a sub-pixel
          // left edge that 125%/150% scaling could round away entirely.
          width: scrollArea.availableWidth
          spacing: Style.space(14)

          // ---------- Banner ----------
          // Pixel-art wordmark, hand-picked by the plugin's author — a fixed
          // asset rather than a transcoded glyph, so it matches exactly.
          Image {
            id: banner
            source: Qt.resolvedUrl("omfasty-logo.png")
            anchors.horizontalCenter: parent.horizontalCenter
            fillMode: Image.PreserveAspectFit
            smooth: false // keep the pixel edges crisp when scaled, no blur
            width: Math.min(parent.width * 0.85, 220)
            height: sourceSize.width > 0 ? width * (sourceSize.height / sourceSize.width) : 0
          }

          PanelSeparator {
            foreground: root.bar.foreground
          }

          // ---------- Hero ----------
          Item {
            width: parent.width
            implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight)

            // Same icon stack as the bar pill, just at display size: base
            // glyph (utensils, or the checkmark once the target is hit) with
            // a single diagonal strike across it while a fast is actively
            // running, instead of a full prohibition ring.
            Item {
              id: heroIcon
              width: heroBaseGlyph.implicitWidth
              height: heroBaseGlyph.implicitHeight
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter

              Text {
                id: heroBaseGlyph
                textFormat: Text.PlainText
                text: root.baseIcon
                color: root.targetReached ? Color.accent : root.bar.foreground
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.display
              }

              Rectangle {
                visible: root.showBan
                anchors.centerIn: parent
                width: Math.max(parent.width, parent.height) * 1.25
                height: Math.max(2, Style.font.display * 0.16)
                radius: height / 2
                rotation: -45
                color: root.bar.foreground
              }
            }

            Column {
              id: heroLabels
              anchors.left: heroIcon.right
              anchors.leftMargin: Style.space(14)
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(2)

              Text {
                text: root.heroTitle
                color: root.bar.foreground
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.title
                font.bold: true
                width: parent.width
                elide: Text.ElideRight
              }

              Text {
                visible: root.heroStats !== ""
                textFormat: Text.PlainText
                text: root.heroStats
                color: Qt.darker(root.bar.foreground, 1.4)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                width: parent.width
                elide: Text.ElideRight
              }

              Text {
                textFormat: Text.PlainText
                text: root.heroBlurb
                color: Qt.darker(root.bar.foreground, 1.4)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                width: parent.width
                wrapMode: Text.WordWrap
              }
            }
          }

          // ---------- Progress ----------
          PanelSeparator {
            visible: root.fasting
            foreground: root.bar.foreground
          }

          Item {
            visible: root.fasting
            width: parent.width
            implicitHeight: Style.space(8)

            Rectangle {
              id: barTrack
              anchors.fill: parent
              radius: height / 2
              color: Qt.rgba(root.bar.foreground.r, root.bar.foreground.g, root.bar.foreground.b, 0.12)
            }

            Rectangle {
              anchors.left: barTrack.left
              anchors.verticalCenter: barTrack.verticalCenter
              height: barTrack.height
              radius: barTrack.radius
              color: Color.accent
              width: Math.max(barTrack.height, barTrack.width * root.progressFraction)

              Behavior on width { NumberAnimation { duration: 320; easing.type: Easing.OutCubic } }
            }
          }

          // ---------- Target presets ----------
          PanelSeparator {
            foreground: root.bar.foreground
          }

          Column {
            width: parent.width
            spacing: Style.space(6)

            PanelSectionHeader {
              text: "FASTING : EATING"
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
            }

            Grid {
              id: presetRow
              width: parent.width
              columns: 3

              // Grid positions column N at N * (cellWidth + spacing) in LOGICAL
              // pixels. Under a fractional display scale (125%, 150%...) that's
              // only a whole DEVICE pixel if cellWidth+spacing, scaled, is
              // itself a whole number — otherwise each column boundary lands on
              // a sub-pixel device position and antialiasing paints the left
              // edge there at partial coverage (looks like a missing/faint
              // border, worse than the right edge of the same button). So the
              // spacing and cell width are built up FROM whole device pixels
              // and converted back, guaranteeing every boundary is exact.
              readonly property real dpr: Screen.devicePixelRatio || 1
              readonly property real spacingPx: Math.round(Style.spacing.xs * dpr) / dpr
              spacing: spacingPx
              readonly property real cellWidth: Math.floor((width - spacingPx * (columns - 1)) / columns * dpr) / dpr

              Repeater {
                model: root.presets

                Button {
                  required property var modelData
                  width: presetRow.cellWidth
                  text: modelData.label
                  fontSize: Style.font.caption
                  foreground: root.bar.foreground
                  fontFamily: root.bar.fontFamily
                  horizontalPadding: Style.spacing.sm
                  verticalPadding: Style.spacing.controlPaddingY
                  bordered: true
                  active: root.targetHours === modelData.hours
                  enabled: !root.fasting
                  opacity: enabled ? 1.0 : 0.5
                  onClicked: root.setTarget(modelData.hours)
                }
              }
            }
          }

          // ---------- Action ----------
          PanelSeparator {
            foreground: root.bar.foreground
          }

          Button {
            width: parent.width
            text: root.fasting ? "End fast" : "Start fast (" + root.targetHours + "h)"
            fontSize: Style.font.body
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            horizontalPadding: Style.spacing.controlPaddingX
            verticalPadding: Style.spacing.controlPaddingY
            bordered: true
            onClicked: root.fasting ? root.stop() : root.start(root.targetHours)
          }

          // ---------- History ----------
          PanelSeparator {
            visible: root.history.length > 0 || root.longestHistory.length > 0
            foreground: root.bar.foreground
          }

          Column {
            visible: root.history.length > 0
            width: parent.width
            spacing: Style.space(6)

            PanelSectionHeader {
              text: "RECENT"
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
            }

            Repeater {
              model: root.history

              Item {
                required property var modelData
                width: column.width
                implicitHeight: historyRow.implicitHeight

                Row {
                  id: historyRow
                  width: parent.width
                  spacing: Style.space(8)

                  Text {
                    textFormat: Text.PlainText
                    text: modelData.actualHours >= modelData.targetHours ? root.iconDone : root.iconHistoryPartial
                    color: root.bar.foreground
                    font.family: root.bar.fontFamily
                    font.pixelSize: Style.font.body
                  }

                  Text {
                    textFormat: Text.PlainText
                    text: Qt.formatDate(new Date(modelData.end * 1000), "d.M.")
                      + "  " + modelData.actualHours.toFixed(1) + "h / " + modelData.targetHours + "h"
                    color: Qt.darker(root.bar.foreground, 1.2)
                    font.family: root.bar.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                }
              }
            }
          }

          // ---------- Longest fasts ----------
          Column {
            visible: root.longestHistory.length > 0
            width: parent.width
            spacing: Style.space(6)

            PanelSectionHeader {
              text: "LONGEST"
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
            }

            Repeater {
              model: root.longestHistory

              Item {
                required property var modelData
                width: column.width
                implicitHeight: longestRow.implicitHeight

                Row {
                  id: longestRow
                  width: parent.width
                  spacing: Style.space(8)

                  Text {
                    textFormat: Text.PlainText
                    text: modelData.actualHours >= modelData.targetHours ? root.iconDone : root.iconHistoryPartial
                    color: root.bar.foreground
                    font.family: root.bar.fontFamily
                    font.pixelSize: Style.font.body
                  }

                  Text {
                    textFormat: Text.PlainText
                    text: Qt.formatDate(new Date(modelData.end * 1000), "d.M.")
                      + "  " + modelData.actualHours.toFixed(1) + "h / " + modelData.targetHours + "h"
                    color: Qt.darker(root.bar.foreground, 1.2)
                    font.family: root.bar.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                }
              }
            }
          }

          Item {
            width: parent.width
            height: Style.space(4)
          }
        }
      }
    }
  }
}
