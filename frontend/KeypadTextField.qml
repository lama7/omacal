import QtQuick
import qs.Commons
import qs.Ui

// TextField that accepts keypad digits. The compositor's NumLock handling
// turns keypad digits into cursor/canvas keys (4=Left, 6=Right, 2=Down,
// 8=Up, 7=Home, 1=End, 9=PgUp, 3=PgDn, 0=Insert) plus Qt.KeypadModifier, so
// they never arrive as Qt.Key_0..9. This handler maps the keycode back to
// its digit and inserts it. It must live ON the field (the focused item) for
// Keys.priority: Keys.BeforeItem to apply.
//
// The panel root is passed in for its contentForeground; guard against panel
// being null at bind time (it's set after creation).
TextField {
    id: root
    property var formPanel: null
    foreground: formPanel ? formPanel.contentForeground : Color.foreground
    Keys.priority: Keys.BeforeItem
    Keys.onPressed: function(event) {
        var d = root.formPanel.keypadDigitFor(event)
        if (d) {
            insert(cursorPosition, d)
            cursorPosition += 1
            event.accepted = true
        }
    }
}
