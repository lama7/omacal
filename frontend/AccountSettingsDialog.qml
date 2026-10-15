import QtQuick
import Quickshell
import QtQuick.Layouts
import qs.Commons
import qs.Ui

// Account settings dialog: opened by the header's U+2261 (triple-bar) button.
// Lists one row per provider key in config.json's "oauth" section (supplied
// by the panel via GET /api/account, client secrets redacted server-side).
// Each row: a qs.Ui Toggle (Enabled/Disabled, same control as the calendar
// rows) plus that provider's settings as "key: value" lines. Toggling fires
// providerToggled(name, enabled); the panel owns the POST /api/account/toggle,
// so this dialog stays presentational (like CalendarToggleDialog).
//
// Soft-disable semantics: toggling a provider off flips the enabled flag on
// every DB row of that provider (view/edit queries already filter on
// c.enabled = 1) and gates its sync source; nothing is deleted, so toggling
// back on restores instantly without re-syncing.
//
// Settings lines are read-only display for now (first pass).

Item {
  id: root
  property bool opened: false
  property var accountData: ({})
  // Whole dialog (scrim + card) only renders while open — same gate as
  // CalendarToggleDialog; otherwise the card would always cover the view.
  visible: opened
  property color background: Color.background
  property color foreground: Color.foreground
  property color accent: Color.accent
  property color scrim: Util.alpha(Color.background, 0.7)
  property string fontFamily: Style.font.family
  property int cornerRadius: Style.cornerRadius

  signal closed()
  signal providerToggled(string pname, bool enabled)

  // Keyboard cursor: which provider row is selected (0..count-1).
  property int selectedIndex: 0

  // Provider names come straight from the JSON keys (e.g. "nextcloud",
  // "google"). Bindings re-evaluate whenever accountData changes.
  property var providerNames: Object.keys(accountData)

  // Enabled state: the backend injects an "enabled" key per provider;
  // absent means enabled (fresh config predates the flag).
  function enabledOf(name) {
    var s = accountData[name] || ({})
    return s["enabled"] === undefined ? true : (s["enabled"] ? true : false)
  }

  // Settings keys to display per provider (the "enabled" control key drives
  // the Toggle instead of getting its own text line).
  property var displayKeys: {
    var out = ({})
    for (var i = 0; i < providerNames.length; i++) {
      var p = providerNames[i]
      var ks = []
      var s = accountData[p] || ({})
      for (var k in s)
        if (k !== "enabled") ks.push(k)
      out[p] = ks
    }
    return out
  }

  // Render JSON values as display strings: arrays joined, nested objects
  // flattened to k=v pairs, scalars stringified.
  function fmtVal(v) {
    if (Array.isArray(v)) return v.join(" ")
    if (v === null || v === undefined) return ""
    if (typeof v === "object") {
      var parts = []
      for (var k in v) parts.push(k + "=" + fmtVal(v[k]))
      return parts.join(" ")
    }
    return String(v)
  }

  // Stable content estimates (same discipline as CalendarToggleDialog): the
  // card height is derived from the model alone — per provider: one toggle
  // row + its settings lines — never from live layout implicit heights.
  property int totalSettingRows: {
    var m = 0
    for (var i = 0; i < providerNames.length; i++)
      m += displayKeys[providerNames[i]].length
    return m
  }
  readonly property int rowFont: 14
  readonly property int rowPitch: Style.space(24)
  readonly property int toggleRowHeight: Style.space(40)
  readonly property int providerGap: Style.space(14)
  readonly property int naturalCardHeight:
      Style.space(16)
      + Math.max(titleText.implicitHeight, doneBtn.height)
      + Style.space(10)
      + Style.space(12)
      + providerNames.length * (toggleRowHeight + providerGap)
      + totalSettingRows * rowPitch
      + doneBtn.height
      + Style.space(26)

  // Keyboard: arrows/Tab move between provider rows, Enter/Space toggles the
  // selected row (mirrors the calendar dialog), Escape closes. Everything
  // else is swallowed while open so stray keys never drive the month view.
  function handleKey(event) {
    if (!root.opened) return false
    if (event.key === Qt.Key_Escape) { root.closed(); return true }
    var n = Math.max(providerNames.length, 1)
    if (event.key === Qt.Key_Left || event.key === Qt.Key_Backtab) {
      selectedIndex = (selectedIndex + n - 1) % n
      return true
    }
    if (event.key === Qt.Key_Right || event.key === Qt.Key_Tab) {
      selectedIndex = (selectedIndex + 1) % n
      return true
    }
    if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter
        || event.key === Qt.Key_Space) {
      if (selectedIndex < providerNames.length) {
        var p = providerNames[selectedIndex]
        root.providerToggled(p, !root.enabledOf(p))
      }
      return true
    }
    return true
  }

  // Scrim: dims the month view behind and swallows clicks (a click outside
  // the card closes the dialog, same as the calendars dialog).
  Rectangle {
    anchors.fill: parent
    color: root.scrim
    MouseArea { anchors.fill: parent; onClicked: root.closed() }
  }

  Rectangle {
    id: card
    width: Math.min(parent.width - Style.space(32), Style.space(420))
    height: Math.min(parent.height - Style.space(48), naturalCardHeight)
    anchors.top: parent.top
    anchors.topMargin: Style.space(24)
    anchors.horizontalCenter: parent.horizontalCenter
    color: root.background
    border.color: root.accent
    border.width: Style.normalBorderWidth
    radius: root.cornerRadius
    clip: true

    // Swallow clicks inside the card so they do not fall through to the
    // month view behind.
    MouseArea { anchors.fill: parent; onClicked: {} }

    ColumnLayout {
      anchors.fill: parent
      anchors.margins: Style.space(16)
      spacing: Style.space(10)

      RowLayout {
        id: titleRow
        Layout.fillWidth: true

        Text {
          id: titleText
          Layout.fillWidth: true
          textFormat: Text.PlainText
          text: "Account Settings"
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: 24
          font.bold: true
          horizontalAlignment: Text.AlignHCenter
          wrapMode: Text.WordWrap
        }
      }

      // Horizontal separator under the header, same as the other views.
      PanelSeparator {
        Layout.fillWidth: true
        foreground: root.foreground
      }

      // One row-block per provider: Toggle + that provider's settings lines.
      Repeater {
        model: root.providerNames

        Item {
          id: provBlock
          property int myIndex: index
          Layout.fillWidth: true
          Layout.preferredHeight: block.implicitHeight

          Column {
            id: block
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(4)

            Toggle {
              id: rowToggle
              label: root.providerNames[provBlock.myIndex]
              description: root.enabledOf(root.providerNames[provBlock.myIndex])
                ? "Enabled" : "Disabled"
              checked: root.enabledOf(root.providerNames[provBlock.myIndex])
              hasCursor: provBlock.myIndex === root.selectedIndex
              foreground: root.foreground
              accent: root.accent
              fontFamily: root.fontFamily
              onClicked: {
                var p = root.providerNames[provBlock.myIndex]
                root.providerToggled(p, !root.enabledOf(p))
              }
            }

            Repeater {
              model: root.displayKeys[root.providerNames[provBlock.myIndex]]

              Text {
                property string myKey: modelData
                width: block.width - Style.space(8)
                x: Style.space(4)
                textFormat: Text.PlainText
                text: {
                  var p = root.providerNames[provBlock.myIndex]
                  var v = root.fmtVal(accountData[p][myKey])
                  return myKey + ": " + (v === "" ? "(none)" : v)
                }
                // Disabled providers read dimmed but stay legible.
                color: root.enabledOf(root.providerNames[provBlock.myIndex])
                  ? root.foreground : Util.alpha(root.foreground, 0.45)
                font.family: root.fontFamily
                font.pixelSize: root.rowFont
                wrapMode: Text.WordWrap
              }
            }
          }
        }
      }

      RowLayout {
        id: buttonRow
        Layout.fillWidth: true
        spacing: Style.space(10)

        Item { Layout.fillWidth: true }   // spacer pushes Done to the right

        Rectangle {
          id: doneBtn
          width: Style.space(96)
          height: Style.space(38)
          radius: root.cornerRadius
          color: doneHover.containsMouse
            ? Style.controlFill(false, true, root.foreground, root.accent)
            : "transparent"
          border.color: doneHover.containsMouse
            ? Style.controlBorder(false, true, root.foreground, root.accent)
            : root.accent
          border.width: doneHover.containsMouse
            ? Style.controlBorderWidth(false, true)
            : Style.normalBorderWidth
          Behavior on color { ColorAnimation { duration: 100 } }

          Text {
            textFormat: Text.PlainText
            anchors.centerIn: parent
            text: "Done"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.subtitle
            font.bold: true
          }

          MouseArea {
            id: doneHover
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: root.closed()
          }
        }
      }
    }
  }
}
