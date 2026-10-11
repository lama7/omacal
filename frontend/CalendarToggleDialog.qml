import QtQuick
import Quickshell
import QtQuick.Layouts
import QtQuick.Controls
import qs.Commons
import qs.Ui

// Local clone of an Omarchy confirm-dialog used to choose which calendars are
// visible. One Toggle row per configured calendar plus a Done button. Mirrors
// DeleteConfirmDialog.qml: a local Item (the shell's ConfirmDialog has no
// content slot), a scrim, a top-anchoured card, and a handleKey()/selectedIndex
// interface routed from the panel's PanelKeyCatcher.
//
// `model` is a ListModel of {id, name, enabled}. selectedIndex: 0..(count-1) = a
// row, count = the Done button. `toggled(id, enabled)` fires on click or
// Space/Return over a selected row; the panel owns the POST, so this dialog
// stays presentational (stateless about the DB, like the scope toggle).
Item {
  id: root
  property bool opened: false

  property var model: []
  property int selectedIndex: 0
  property color background: Color.background
  property color foreground: Color.foreground
  property color accent: Color.accent
  property color scrim: Util.alpha(Color.background, 0.7)
  property string fontFamily: Style.font.family
  property int cornerRadius: Style.cornerRadius

  signal closed()
  signal toggled(int id, bool enabled)
  signal addRequested()
  signal addSubmit(string name, string color)
  signal addCancel()
  signal navRequested(int dx)

  // Add-calendar mini form state. The panel flips this on "+" click; the
  // field/buttons live inside the card while it is true. The POST stays in
  // the panel (addSubmit carries the typed name to it).
  property bool addOpen: false
  property string pendingAddName: ""
  property string pendingAddColor: ""
  property alias nameFieldRef: nameField

  // The Done button's slot, after all calendar rows.
  readonly property int doneIndex: root.model.count

  // Stable content estimate: N rows of ~58px + 4px gaps, derived from
  // model.count alone. Used for the card height, the Flickable contentHeight,
  // the content Item height, and the 'is it scrollable' test -- so they all
  // know immediately and never depend on the live listLayout.implicitHeight,
  // which is 0 for one frame on every model rebuild (the source of the blip).
  readonly property int estimatedContentHeight:
      root.model.count * Style.space(58) + Math.max(root.model.count - 1, 0) * Style.space(4)

  function handleKey(event) {
    if (!root.opened) return false
    // While the add-calendar mini form is open: Escape cancels the form
    // (back to the list), Enter/Space submits if a name is typed, arrows
    // navigate months in the panel behind the dialog (the focused TextField
    // swallows arrows as caret moves first; these reach here only when the
    // field does NOT hold focus).
    if (root.addOpen) {
      if (event.key === Qt.Key_Escape) { root.addCancel(); return true }
      if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
        if (root.pendingAddName !== "") root.addSubmit(root.pendingAddName, root.pendingAddColor)
        return true
      }
      if (event.key === Qt.Key_Left || event.key === Qt.Key_Right) {
        root.navRequested(event.key === Qt.Key_Left ? -1 : 1)
        return true
      }
      return false
    }
    var total = root.doneIndex + 1   // rows + done
    if (event.key === Qt.Key_Escape) {
      root.closed()
      return true
    } else if (event.key === Qt.Key_Left || event.key === Qt.Key_Backtab) {
      root.selectedIndex = (root.selectedIndex - 1 + total) % total
      return true
    } else if (event.key === Qt.Key_Right || event.key === Qt.Key_Tab) {
      root.selectedIndex = (root.selectedIndex + 1) % total
      return true
    } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
      if (root.selectedIndex === root.doneIndex) root.closed()
      else {
        var m = root.model.get(root.selectedIndex)
        if (m) root.toggled(m.id, !m.enabled)
      }
      return true
    } else if (event.key === Qt.Key_Space) {
      if (root.selectedIndex === root.doneIndex) { root.closed(); return true }
      var m = root.model.get(root.selectedIndex)
      if (m) root.toggled(m.id, !m.enabled)
      return true
    }
    return false
  }

  visible: opened

  Rectangle {
    anchors.fill: parent
    color: root.scrim
    MouseArea { anchors.fill: parent; onClicked: root.closed() }
  }

  Rectangle {
    id: card
    // Width: most of the panel, capped so it doesn't span the full width on a
    // wide screen. Height: a stable estimate (title + row list + Done button +
    // inner margins) from model.count, clamped by the parent. The Flickable
    // scrolls any content taller than the card (scroll bar when scrollable),
    // so the last row is never clipped and the card height never blips on a
    // toggle press.
    width: Math.min(parent.width - Style.space(32), Style.space(420))
    // The row list term is a stable estimate (see estimatedContentHeight below),
    // never the live listLayout.implicitHeight, which is 0 for one frame on
    // every model rebuild. Keeping it based on model.count is what stops the
    // card and the content region from blipping when a toggle rebuilds the model.
    height: Math.min(
        parent.height - Style.space(48),
        Style.space(16)
        + Math.max(titleText.implicitHeight, addBtn.height)
        + Style.space(10)
        + estimatedContentHeight
        + Style.space(10)
        + doneBtn.height
        + Style.space(16))
    anchors.top: parent.top
    anchors.topMargin: Style.space(24)
    anchors.horizontalCenter: parent.horizontalCenter
    color: root.background
    border.color: root.accent
    border.width: Style.normalBorderWidth
    radius: root.cornerRadius
    clip: true

    MouseArea { anchors.fill: parent; onClicked: {} }

    ColumnLayout {
      id: inner
      anchors.fill: parent
      anchors.margins: Style.space(16)
      spacing: Style.space(10)

      RowLayout {
        id: titleRow
        Layout.fillWidth: true
        spacing: Style.space(8)

        Text {
          id: titleText
          Layout.fillWidth: true
          textFormat: Text.PlainText
          text: "Calendars"
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.title
          font.bold: true
          wrapMode: Text.WordWrap
        }

        Item { Layout.fillWidth: true }   // spacer pushes + to the right

        // "+" button, same glyph as the day-view Add Event button (U+002B).
        // Same component as the panel's header buttons (PanelActionButton):
        // borderless, foreground-tint hover fill, PanelToolTip on hover.
        // Click-only for now (no keyboard route in handleKey); emits
        // addRequested() so the panel owns whatever adding a calendar entails.
        PanelActionButton {
          id: addBtn
          iconText: "\u002b"
          tooltipText: "Add Calendar"
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: root.addRequested()
        }
      }

      // Scrollable list so many calendars never push the card off-screen.
      // Hidden while the add-calendar mini form is open (the form replaces
      // the list; the card height stays put since it is model-count driven).
      Flickable {
        id: listArea
        visible: !root.addOpen
        Layout.fillWidth: true
        Layout.fillHeight: true
        boundsBehavior: Flickable.StopAtBounds
        clip: true
        contentWidth: parent.width
        // contentHeight is the STABLE estimate, not the live listLayout.implicitHeight.
        // The rows are fixed-height Toggles (54px min), so the estimate is exact. A
        // toggle's setProperty re-evaluates one delegate; if its description Text
        // momentarily re-wraps, listLayout.implicitHeight can spike for a frame and
        // grow the Flickable content (the "box gets larger" glitch). Binding to the
        // estimate keeps the content height constant, so nothing grows on a toggle.
        contentHeight: estimatedContentHeight
        interactive: estimatedContentHeight > Math.max(listArea.height, 0)
        flickableDirection: Flickable.VerticalFlick
        ScrollBar.vertical: ScrollBar {
          policy: ScrollBar.AlwaysOn
          visible: listArea.contentHeight > listArea.height + 1
        }

        Item {
          id: list
          width: parent.width
          height: estimatedContentHeight

          ColumnLayout {
            id: listLayout
            anchors.left: parent.left
            anchors.top: parent.top
            spacing: Style.space(4)

            Repeater {
              id: rows
              model: root.model

              Toggle {
                required property var modelData
                required property int index
                Layout.fillWidth: true
                label: modelData.name
                description: modelData.enabled ? "Visible" : "Hidden"
                checked: modelData.enabled
                hasCursor: index === root.selectedIndex
                foreground: root.foreground
                accent: root.accent
                fontFamily: root.fontFamily
                onClicked: root.toggled(modelData.id, !modelData.enabled)
              }
            }
          }
        }
      }

      // Add-calendar mini form: shown in place of the list once "+" is clicked.
      // Enter submits (the focused field owns Enter, like the add-event form),
      // Escape cancels. The panel owns the POST via addSubmit(name).
      ColumnLayout {
        id: addForm
        visible: root.addOpen
        Layout.fillWidth: true
        spacing: Style.space(8)

        TextField {
          id: nameField
          Layout.fillWidth: true
          // implicitHeight, not Style.spacing.controlHeight (28px fixed):
          // the TextField's own implicit height (font metrics + internal
          // padding at subtitle size) exceeds 28, and a fixed 28 clips
          // entered text at the top.
          Layout.preferredHeight: Math.max(implicitHeight, Style.spacing.controlHeight)
          placeholderText: "Calendar name"
          foreground: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.subtitle
          onTextChanged: if (text !== "") root.pendingAddName = text
          Keys.onReturnPressed: root.addSubmit(root.pendingAddName, root.pendingAddColor)
          Keys.onEnterPressed: root.addSubmit(root.pendingAddName, root.pendingAddColor)
          Keys.onEscapePressed: { root.pendingAddName = ""; root.addCancel() }
        }

        // Color picker: preset swatches (Google-Calendar style). Click one to
        // pick the calendar's color; leave it unset for the server default.
        // The chosen hex rides along on addSubmit to the backend, which PROPPATCHes
        // the CalDAV calendar-color property and caches it in the DB.
        RowLayout {
          id: swatchRow
          Layout.fillWidth: true
          spacing: Style.space(8)

          Repeater {
            id: swatches
            model: [
              "#d93025", "#e8710a", "#f2b134", "#8f68ff",
              "#1a73e8", "#12b5e0", "#1e8e3e", "#e91e63"
            ]

            Rectangle {
              required property string modelData
              required property int index
              Layout.preferredWidth: Style.space(22)
              Layout.preferredHeight: Style.space(22)
              radius: width / 2
              color: modelData
              border.color: root.foreground
              border.width: (root.pendingAddColor === modelData)
                  ? Style.normalBorderWidth + 1 : 0
              MouseArea {
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.pendingAddColor = modelData
              }
            }
          }

          Text {
            // Tiny caption so the row reads as a picker, not decoration.
            Layout.fillWidth: true
            textFormat: Text.PlainText
            text: "Color"
            color: root.foreground
            opacity: 0.6
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }

        RowLayout {
          id: addBtnRow
          Layout.fillWidth: true
          spacing: Style.space(10)

          Item { Layout.fillWidth: true }   // spacer pushes Create/Cancel right

          Rectangle {
            id: createBtn
            Layout.preferredWidth: Style.space(96)
            height: Style.space(38)
            radius: root.cornerRadius
            color: "transparent"
            border.color: root.accent
            border.width: Style.normalBorderWidth
            Text {
              textFormat: Text.PlainText
              anchors.centerIn: parent
              text: "Create"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.subtitle
              font.bold: true
            }
            MouseArea {
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: root.addSubmit(root.pendingAddName, root.pendingAddColor)
            }
          }
          Rectangle {
            id: cancelBtn
            Layout.preferredWidth: Style.space(96)
            height: Style.space(38)
            radius: root.cornerRadius
            color: "transparent"
            border.color: root.accent
            border.width: Style.normalBorderWidth
            Text {
              textFormat: Text.PlainText
              anchors.centerIn: parent
              text: "Cancel"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.subtitle
              font.bold: true
            }
            MouseArea {
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: root.addCancel()
            }
          }
        }
      }

      RowLayout {
        id: buttonRow
        Layout.fillWidth: true
        visible: !root.addOpen
        spacing: Style.space(10)

        Item { Layout.fillWidth: true }   // spacer pushes Done to the right

        Rectangle {
          id: doneBtn
          width: Style.space(96)
          height: Style.space(38)
          radius: root.cornerRadius
          color: "transparent"
          border.color: root.accent
          border.width: Style.normalBorderWidth

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
