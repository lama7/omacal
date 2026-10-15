import QtQuick
import Quickshell
import QtQuick.Layouts
import qs.Commons
import qs.Ui

// Account settings dialog: opened by the header's U+2261 (triple-bar) button.
// Shows the OAuth account settings straight from config.json — the panel
// supplies them via GET /api/account, which returns the config.json "oauth"
// section with client secrets redacted server-side (nothing sensitive ever
// leaves the backend). One tab per provider key in that JSON section;
// clicking a tab shows that provider's settings as "key: value" lines.
// Read-only: this dialog never writes anything back.

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

  // Keyboard/mouse tab cursor: which provider tab is active.
  property int tabIndex: 0

  // Provider names come straight from the JSON keys (e.g. "nextcloud",
  // "google"). Bindings re-evaluate whenever accountData or tabIndex change.
  property var providerNames: Object.keys(accountData)
  property string currentProvider: tabIndex < providerNames.length ? providerNames[tabIndex] : ""
  property var currentSettings: accountData[currentProvider] || ({})
  property var currentKeys: Object.keys(currentSettings)

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
  // card height uses the TALLEST provider across all tabs, so switching tabs
  // never blips the card, and never reads live layout implicit heights.
  property var maxRows: {
    var m = 1
    for (var i = 0; i < providerNames.length; i++) {
      var n = Object.keys(accountData[providerNames[i]] || ({})).length
      if (n > m) m = n
    }
    return m
  }
  readonly property int rowFont: 14
  readonly property int rowPitch: Style.space(26)
  readonly property int tabRowHeight: Style.space(34)
  readonly property int naturalCardHeight:
      Style.space(16)
      + Math.max(titleText.implicitHeight, doneBtn.height)
      + Style.space(10)
      + tabRowHeight
      + Style.space(10)
      + maxRows * rowPitch
      + Style.space(16)

  // Keyboard: arrows/Tab move between tabs, Enter selects the current tab
  // (no-op visually), Escape closes the dialog. Everything else is swallowed
  // while open so stray keys never drive the month view behind the scrim.
  function handleKey(event) {
    if (!root.opened) return false
    if (event.key === Qt.Key_Escape) { root.closed(); return true }
    var n = Math.max(providerNames.length, 1)
    if (event.key === Qt.Key_Left || event.key === Qt.Key_Backtab) {
      tabIndex = (tabIndex + n - 1) % n
      return true
    }
    if (event.key === Qt.Key_Right || event.key === Qt.Key_Tab) {
      tabIndex = (tabIndex + 1) % n
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

      // Tab bar: one tab per provider key in config.json's oauth section.
      RowLayout {
        id: tabRow
        Layout.fillWidth: true
        spacing: Style.space(8)

        Repeater {
          model: root.providerNames

          Item {
            Layout.fillWidth: true
            property int myIndex: index
            Rectangle {
              anchors.horizontalCenter: parent.horizontalCenter
              anchors.verticalCenter: parent.verticalCenter
              width: labelText.width + Style.space(20)
              height: labelText.height + Style.space(10)
              radius: root.cornerRadius
              property bool selected: myIndex === root.tabIndex
              color: selected ? Util.alpha(root.accent, 0.25) : "transparent"
              border.color: selected ? root.accent : "transparent"
              border.width: selected ? Style.normalBorderWidth : 0
              Text {
                id: labelText
                anchors.centerIn: parent
                textFormat: Text.PlainText
                text: root.providerNames[myIndex]
                color: selected ? root.accent : root.foreground
                font.family: root.fontFamily
                font.pixelSize: root.rowFont
                font.bold: selected
              }
              MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: root.tabIndex = myIndex
              }
            }
          }
        }
      }

      // The selected provider's settings, one "key: value" line per JSON key.
      Column {
        id: rowsArea
        Layout.fillWidth: true
        spacing: rowPitch - root.rowFont

        Repeater {
          model: root.currentKeys

          Text {
            property int myIndex: index
            width: rowsArea.width
            textFormat: Text.PlainText
            text: {
              var k = root.currentKeys[myIndex]
              var v = root.fmtVal(root.currentSettings[k])
              return k + ": " + (v === "" ? "(none)" : v)
            }
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: root.rowFont
            wrapMode: Text.WordWrap
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
