import QtQuick
import qs.Commons

// Shared dialog action button — the chunky rounded control the dialogs use for
// their actions (Create/Subscribe, Cancel, Save, Done, Back, Sign in/Add
// account, Confirm...).
//
// Extracted so the look lives in ONE place. It had been hand-rolled as
// Rectangle + MouseArea + Text in CalendarToggleDialog, AccountSettingsDialog
// and DeleteConfirmDialog (which had drifted to three sizes and two corner
// styles) and approximated with qs.Ui.Button in the account/add-event forms.
//
// Size is implicit (96x38): a RowLayout uses it as the preferred size, and a
// plain Row picks it up from implicitWidth. Override with
// `Layout.preferredWidth`/`width` where a different size is wanted (e.g. the
// add-event form's half-width Cancel/Save).
//
// `destructive` paints the red confirm variant (DeleteConfirmDialog).
// `hot` is the keyboard-cursor cue a dialog paints when Tab parks on this
// slot. It is deliberately NOT the same as mouse hover: the cursor gets the
// accent (or red) tinted fill and a thicker border, while hover keeps the
// kit's hover tokens — this theme mirrors its focus tokens onto the hover
// tokens, so focus could not tell them apart.
// `enabled` gates clicks and dims the label.
Rectangle {
  id: root

  property string text: ""
  property color foreground: Color.foreground
  property color accent: Color.accent
  property string fontFamily: Style.font.family
  property real fontSize: Style.font.subtitle
  property bool destructive: false
  property bool hot: false

  signal clicked()
  // Emitted when the mouse enters — lets a dialog keep its keyboard cursor
  // (selectedIndex) in step with the mouse, as DeleteConfirmDialog does.
  signal entered()

  readonly property bool _hover: root.enabled && mouse.containsMouse
  readonly property bool _cursor: root.enabled && root.hot
  // Accent normally, red for a destructive button — the cursor cue and the
  // unlit border both key off it.
  readonly property color _cue: root.destructive ? Color.urgent : root.accent

  implicitWidth: Style.space(96)
  implicitHeight: Style.space(38)
  radius: Style.cornerRadius
  // Destructive (Delete/Unsubscribe) is red in EVERY state. The kit's
  // controlFill/controlBorder resolve their state token against `foreground`,
  // so handing Color.urgent in as the accent renders a plain foreground tint —
  // the red has to be painted explicitly.
  //   cursor -> cue tinted fill + thicker cue border (the keyboard position)
  //   hover  -> kit hover tokens (destructive: red fill + full red border)
  //   unlit  -> transparent + dim cue border
  color: root._cursor
    ? Util.alpha(root._cue, root.destructive ? 0.32 : 0.18)
    : (root._hover
        ? (root.destructive
            ? Util.alpha(Color.urgent, 0.22)
            : Style.controlFill(false, true, root.foreground, root.accent))
        : "transparent")
  border.color: root._cursor
    ? root._cue
    : (root._hover
        ? (root.destructive
            ? Color.urgent
            : Style.controlBorder(false, true, root.foreground, root.accent))
        : (root.destructive ? Util.alpha(Color.urgent, 0.56) : root.accent))
  border.width: root._cursor
    ? Style.normalBorderWidth + 1
    : (root._hover ? Style.controlBorderWidth(false, true) : Style.normalBorderWidth)
  Behavior on color { ColorAnimation { duration: 100 } }

  Text {
    textFormat: Text.PlainText
    anchors.centerIn: parent
    text: root.text
    color: root.destructive
      ? Color.urgent
      : (root.enabled ? root.foreground : Util.alpha(root.foreground, 0.4))
    font.family: root.fontFamily
    font.pixelSize: root.fontSize
    font.bold: true
  }

  MouseArea {
    id: mouse
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: root.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
    enabled: root.enabled
    onEntered: root.entered()
    onClicked: root.clicked()
  }
}
