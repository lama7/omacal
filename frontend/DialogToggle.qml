import QtQuick
import qs.Commons
import qs.Ui

// Toggle row carrying the dialogs' keyboard-cursor cue. The kit's Toggle paints
// hasCursor with the hover-cursor tokens, which under themes that mirror focus
// onto hover reads exactly like a mouse hover (the "the Nextcloud button looks
// hovered" complaint). Dialogs instead paint the cursor with the theme accent —
// a tinted fill plus a thicker border — the same cue DialogButton and the Add
// Provider chooser use, so the keyboard position is unmistakable.
//
// Only the cursor state differs: normal/hover keep the kit's own fill and
// border tokens, so a row looks stock until the keyboard cursor lands on it.
// hasCursor is left off — the cursor is painted here instead.
Toggle {
  id: root

  // The dialog's keyboard cursor is on this row.
  property bool cursorOn: false

  // Mouse hover, tracked through the kit's public hovered() signal so we never
  // reach into the kit's private _hot.
  property bool _mouseOver: false
  onHovered: function (isHovered) { root._mouseOver = isHovered }

  color: root.cursorOn
    ? Util.alpha(root.accent, 0.18)
    : Style.controlFill(activeFocus, root._mouseOver, root.foreground, root.accent)
  borderSpec: root.cursorOn
    ? Border.flat(root.accent, Style.normalBorderWidth + 1)
    : Border.controlSpec(activeFocus ? "focus"
                                     : (root._mouseOver ? "hover-cursor" : "normal"),
                         root.foreground, root.accent)
}
