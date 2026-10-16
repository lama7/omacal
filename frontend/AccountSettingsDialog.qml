import QtQuick
import Quickshell
import QtQuick.Layouts
import qs.Commons
import qs.Ui

// Account settings dialog: opened by the header's U+2261 (triple-bar) button.
// First pass shows one Toggle row per provider key in config.json's "oauth"
// section (supplied by the panel via GET /api/account), matching the calendar
// dialog's control. Toggling fires providerToggled(name, enabled); the panel
// owns the POST /api/account/toggle, so this dialog stays presentational.
//
// `model` is a ListModel of { name, enabled } built by the panel (the panel
// also carries each provider's `settings` string; this pass renders only the
// toggles). It is a ListModel (not a plain JS array) so a toggle flips ONE row
// in place via setProperty() — a plain-array rebuild would tear down every
// delegate, and a re-created Toggle appears already-checked so its knob never
// animates.
//
// Height discipline (the "flashing Done button" bug): every row has a FIXED
// height and the card height is derived from model.count alone, never from a
// live layout implicitHeight. A toggle's setProperty re-evaluates one
// delegate; if a row were sized by live content, its height could change for a
// frame and shift every row below it — including the Done button — which is
// the flash. Same reasoning as CalendarToggleDialog's estimatedContentHeight.
//
// Soft-disable semantics: toggling a provider off flips the enabled flag on
// every DB row of that provider (view/edit queries already filter on
// c.enabled = 1) and gates its sync source; nothing is deleted, so toggling
// back on restores instantly without re-syncing.

Item {
  id: root
  property bool opened: false
  // Provider rows as a ListModel of { name, enabled } (panel-built).
  property var model: null
  readonly property int providerCount: model ? model.count : 0
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

  // Stable content estimate (same discipline as CalendarToggleDialog): one
  // fixed-height toggle row per provider, plus a small gap between rows. The
  // card height and the rows region are both derived from this, so a toggle
  // press never changes them.
  readonly property int rowHeight: Style.space(58)
  readonly property int rowGap: Style.space(4)
  readonly property int estimatedRowsHeight:
      providerCount * rowHeight + Math.max(providerCount - 1, 0) * rowGap

  readonly property int naturalCardHeight:
      Style.space(16) * 2
      + titleText.implicitHeight
      + sep.implicitHeight
      + doneBtn.height
      + estimatedRowsHeight
      + Style.space(10) * 4   // gaps: title|sep|rows|spacer|Done

  // Keyboard: arrows/Tab move between provider rows, Enter/Space toggles the
  // selected row (mirrors the calendar dialog), Escape closes. Everything
  // else is swallowed while open so stray keys never drive the month view.
  function handleKey(event) {
    if (!root.opened) return false
    if (event.key === Qt.Key_Escape) { root.closed(); return true }
    var n = Math.max(providerCount, 1)
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
      if (selectedIndex < providerCount) {
        var m = model.get(selectedIndex)
        root.providerToggled(m.name, !m.enabled)
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
        id: sep
        Layout.fillWidth: true
        foreground: root.foreground
      }

      // One fixed-height Toggle row per provider. Bound to the panel's
      // ListModel; a toggle flips one row via setProperty so this delegate is
      // NOT re-created and the knob animates. The row's height is a constant
      // (rowHeight), never the Toggle's live implicitHeight, so a toggle press
      // cannot move the rows below it or the Done button.
      Repeater {
        model: root.model

        Item {
          id: provRow
          required property var modelData
          required property int index
          // implicitWidth matters: the ColumnLayout derives its own width from
          // children's implicit widths, and a bare Item is 0.
          implicitWidth: rowToggle.implicitWidth
          Layout.fillWidth: true
          Layout.preferredHeight: root.rowHeight

          Toggle {
            id: rowToggle
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            label: provRow.modelData.name
            description: provRow.modelData.enabled ? "Enabled" : "Disabled"
            checked: provRow.modelData.enabled
            hasCursor: provRow.index === root.selectedIndex
            foreground: root.foreground
            accent: root.accent
            fontFamily: root.fontFamily
            onClicked: root.providerToggled(provRow.modelData.name,
                                            !provRow.modelData.enabled)
          }
        }
      }

      // Flexible spacer: absorbs any slack between the model-count estimate and
      // the real layout, pinning the Done button to the bottom of the card so
      // it never moves as rows change state.
      Item {
        Layout.fillWidth: true
        Layout.fillHeight: true
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
