import QtQuick
import qs.Commons
import qs.Ui

// Local clone of Omarchy's ConfirmDialog with a "Delete whole series" scope
// toggle inside the card (the stock component has no content slot). Keeps the
// same handleKey()/selectedIndex/canceled/confirmed interface the panel's
// PanelKeyCatcher routes to, so keyboard behavior is unchanged.
//
// Uses a plain Rectangle for the card instead of Omarchy's BorderSurface,
// which is a sibling component in the shell's Ui dir and not resolvable from
// a plugin directory.
Item {
  id: root

  property bool opened: false
  property string message: ""
  property string cancelText: "Cancel"
  property string confirmText: "Delete"
  property int selectedIndex: 0
  property bool showScope: false
  property bool scopeChecked: false
  property string scopeLabel: "Delete whole series"
  property string scopeDescription: "Remove every occurrence of this repeating event"
  property color background: Color.background
  property color foreground: Color.foreground
  property color scrim: Util.alpha(Color.background, 0.7)
  property color selectedText: Color.accent
  property string fontFamily: Style.font.family
  property int cornerRadius: Style.cornerRadius

  signal canceled()
  signal confirmed()
  signal scopeToggled(bool checked)

  function handleKey(event) {
    if (!root.opened) return false
    if (event.key === Qt.Key_Escape) {
      root.canceled()
      return true
    } else if (event.key === Qt.Key_Left || event.key === Qt.Key_Right || event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) {
      root.selectedIndex = root.selectedIndex === 0 ? 1 : 0
      return true
    } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
      if (root.selectedIndex === 0) root.canceled()
      else root.confirmed()
      return true
    }
    return false
  }

  visible: opened

  Rectangle {
    anchors.fill: parent
    color: root.scrim
    MouseArea { anchors.fill: parent; onClicked: root.canceled() }
  }

  Rectangle {
    id: card
    width: Math.min(parent.width - Style.space(32), Style.space(370))
    height: Style.space(18) * 2
            + messageText.implicitHeight
            + (root.showScope ? scopeRow.implicitHeight + Style.space(12) : 0)
            + Style.space(20) + Style.space(38)
    // Anchor the card's top to the panel top (not centered) so a tall card on a
    // short panel stays on screen instead of pushing its top off the top edge.
    anchors.top: parent.top
    anchors.topMargin: Style.space(24)
    anchors.horizontalCenter: parent.horizontalCenter
    color: root.background
    border.color: root.selectedText
    border.width: Style.normalBorderWidth
    radius: root.cornerRadius

    MouseArea { anchors.fill: parent; onClicked: {} }

    Item {
      anchors.fill: parent
      anchors.margins: Style.space(18)

      Text {
        id: messageText
        textFormat: Text.PlainText
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        text: root.message
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.title
        wrapMode: Text.WordWrap
      }

      Toggle {
        id: scopeRow
        visible: root.showScope
        width: parent.width
        anchors.top: messageText.bottom
        anchors.topMargin: Style.space(12)
        label: root.scopeLabel
        description: root.scopeDescription
        checked: root.scopeChecked
        foreground: root.foreground
        accent: root.selectedText
        fontFamily: root.fontFamily
        onClicked: root.scopeToggled(!root.scopeChecked)
      }

      Row {
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        spacing: Style.space(10)

        Repeater {
          model: [root.cancelText, root.confirmText]

          DialogButton {
            required property int index
            required property string modelData

            // Keyboard cursor: the panel's handleKey() moves selectedIndex and
            // mouse hover follows it here (onEntered), so the two agree — the
            // same cue the Calendars dialog paints on its Done slot.
            hot: root.selectedIndex === index
            destructive: index === 1
            text: modelData
            foreground: root.foreground
            accent: root.selectedText
            fontFamily: root.fontFamily
            onEntered: root.selectedIndex = index
            onClicked: {
              if (index === 0) root.canceled()
              else root.confirmed()
            }
          }
        }
      }
    }
  }
}
