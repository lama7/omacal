// DateEntry.qml — keyboard-native MM/DD/YYYY date entry field.
//
// SPIKE. Built on Omarchy's TextField (a stock Qt Quick Controls TextField,
// so placeholderText / Keys.priority / key handlers are all available).
// The component owns its text entirely: a phase state machine (month -> day
// -> year) auto-formats as the user types, and backspace deletes the rightmost
// char, un-committing a segment when it removes a slash.
//
// Public API (drop-in for the current inputMask TextField):
//   value      : JS Date | null  — parsed date, null while incomplete/invalid
//   dateValid  : bool            — true when a complete, real date is entered
//   setDate(d) : void            — prefill from a JS Date (edit mode)
//   clear()    : void            — reset to empty
//
// The pure formatting logic lives in DateEntryLogic.js so it can be unit-tested
// in Node without a QML runtime. This file is a thin QML shell over it.

import QtQuick
import qs.Commons
import qs.Ui
import "DateEntryLogic.js" as Logic

TextField {
    id: root

    // --- Public API ---
    property var value: null          // JS Date | null (a QML `date` can't hold null)
    property bool dateValid: false
    // The form that owns the field (set to the AddEventForm root when used in
    // the add/edit form), so ESC can cancel the form while this field has focus.
    property var formPanel: null

    // --- Internal state ---
    property int currentYear: new Date().getFullYear() // year preloaded on day->year entry
    property bool prefill: true       // master toggle for the year-prefill mock-up
    property bool overwriteOnType: true // setDate() fill: first typed digit restarts the entry
    property bool _prefilled: false   // true while the year is a pristine typeoverable default
    property bool _resetOnType: false // true while the field holds a programmatic default
    property string _text: ""         // the raw MM/DD/YYYY string we own
    property string _phase: "month"   // "month" | "day" | "year"

    placeholderText: "MM/DD/YYYY"
    placeholderTextColor: Qt.darker(root.foreground, 1.6)
    // QQC hides the placeholder whenever the field is focused AND its text is
    // centered (Basic/TextField.qml gating on horizontalAlignment===AlignHCenter).
    // So we center entered text but left-align while empty, keeping the "MM/DD/YYYY"
    // prompt visible even when the field has focus.
    horizontalAlignment: (root._text === "") ? Text.AlignLeft : Text.AlignHCenter
    // Auto flag invalid/partial entries red; placeholder stays theme normal.
    color: (root._text === "" || root.dateValid) ? root.foreground : Color.urgent

    // We manage text ourselves; never let the field's own editing run.
    readOnly: true
    text: _text

    // Route all keys through our state machine before the field sees them.
    Keys.priority: Keys.BeforeItem
    Keys.onPressed: function(event) {
        var handled = root._handleKey(event)
        if (handled) event.accepted = true
    }
    // ESC cancels the add/edit form. The keyCatcher is a sibling of the form,
    // not an ancestor, so while this field has focus the catcher never sees the
    // key — the field must dismiss the form itself (same pattern as Enter).
    Keys.onEscapePressed: { if (root.formPanel) root.formPanel.dismissAddForm() }

    function _handleKey(event) {
        // Backspace: delete rightmost char, un-commit segment if it was a slash.
        if (event.key === Qt.Key_Backspace) {
            var res = Logic.backspace(root._text, root._phase)
            root._text = res.text
            root._phase = res.phase
            root._prefilled = res.prefilled
            root._resetOnType = false // an edit cancels the restart-the-field default
            root._sync()
            return true
        }

        // Keypad digits arrive as cursor/canvas keys + KeypadModifier (NumLock).
        var d = root.keypadDigitFor(event)
        if (d === null) {
            // Top-row digits 0-9.
            if (event.key >= Qt.Key_0 && event.key <= Qt.Key_9) d = String(event.key - Qt.Key_0)
            else if (event.key === Qt.Key_Slash) d = "/"
            else return false // quietly discard everything else
        }

        // A field preloaded via setDate(): the first typed digit restarts the
        // whole entry from month, instead of typeover-editing the year we filled.
        if (root._resetOnType && d !== "/") {
            root._text = ""
            root._phase = "month"
            root._prefilled = false
            root._resetOnType = false
        }

        var curYear = root.prefill ? root.currentYear : null
        var res2 = Logic.type(root._text, root._phase, d, curYear, root._prefilled)
        if (res2.accepted) {
            root._text = res2.text
            root._phase = res2.phase
            root._prefilled = res2.prefilled === true
            root._sync()
        }
        return true // consumed (even if discarded)
    }

    function _sync() {
        var parsed = Logic.parse(root._text)
        root.value = parsed.date
        root.dateValid = parsed.valid
    }

    // Reuse the panel's keypad mapping (same logic as omacal-panel.qml).
    function keypadDigitFor(event) {
        if (!(event.modifiers & Qt.KeypadModifier)) return null
        switch (event.key) {
        case Qt.Key_0: return "0"; case Qt.Key_1: return "1"
        case Qt.Key_2: return "2"; case Qt.Key_3: return "3"
        case Qt.Key_4: return "4"; case Qt.Key_5: return "5"
        case Qt.Key_6: return "6"; case Qt.Key_7: return "7"
        case Qt.Key_8: return "8"; case Qt.Key_9: return "9"
        case Qt.Key_Left: return "4"; case Qt.Key_Right: return "6"
        case Qt.Key_Up: return "8"; case Qt.Key_Down: return "2"
        case Qt.Key_Home: return "7"; case Qt.Key_End: return "1"
        case Qt.Key_PageUp: return "9"; case Qt.Key_PageDown: return "3"
        case Qt.Key_Insert: return "0"; case Qt.Key_Clear: return "5"
        case Qt.Key_Delete: return "."
        }
        return null
    }

    // Prefill from a JS Date (edit mode). The year is left typeoverable.
    function setDate(d) {
        if (!d) { root.clear(); return }
        var mm = String(d.getMonth() + 1).padStart(2, "0")
        var dd = String(d.getDate()).padStart(2, "0")
        var yyyy = String(d.getFullYear()).padStart(4, "0")
        root._text = mm + "/" + dd + "/" + yyyy
        root._phase = "year"
        root._prefilled = true
        root._resetOnType = root.overwriteOnType
        root._sync()
    }

    function clear() {
        root._text = ""
        root._phase = "month"
        root._prefilled = false
        root._resetOnType = false
        root._sync()
    }

    Component.onCompleted: root._sync()
}