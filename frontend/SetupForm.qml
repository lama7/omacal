import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// First-run setup form: configure a single CalDAV calendar. The password
// goes to the system keyring (via POST /api/setup) and never touches disk.
// On success the panel switches to the calendar; on failure an error string
// is shown and the form stays for another try.
Column {
    id: root
    width: hostColumn ? hostColumn.width : 0
    spacing: Style.space(10)

    property var panel: null
    property var hostColumn: null
    property bool busy: false
    property string error: ""

    Text {
        width: root.width
        text: "Set up your calendar"
        textFormat: Text.PlainText
        font.family: root.panel ? root.panel.contentFontFamily : Style.font.family
        font.pixelSize: Style.font.body
        font.bold: true
        color: root.panel ? root.panel.contentForeground : Color.foreground
        wrapMode: Text.Wrap
    }

    Text {
        width: root.width
        text: "Enter your CalDAV server details. Credentials are stored securely in the system keyring."
        textFormat: Text.PlainText
        font.family: root.panel ? root.panel.contentFontFamily : Style.font.family
        font.pixelSize: Style.font.bodySmall
        color: Qt.darker(root.panel ? root.panel.contentForeground : Color.foreground, 1.5)
        wrapMode: Text.Wrap
    }

    // ---- Fields ----
    Repeater {
        model: [
            { ph: "Server URL", key: "url", echo: TextInput.Normal, tip: "e.g. https://calendar.example.com/dav/" },
            { ph: "Calendar name", key: "name", echo: TextInput.Normal, tip: "" },
            { ph: "Username", key: "user", echo: TextInput.Normal, tip: "" },
            { ph: "Password", key: "pass", echo: TextInput.Password, tip: "" },
        ]
        Item {
            width: root.width
            height: Style.spacing.controlHeight
            TextField {
                width: parent.width
                height: parent.height
                placeholderText: modelData.ph
                echoMode: modelData.echo
                foreground: root.panel ? root.panel.contentForeground : Color.foreground
                onTextChanged: root.setField(modelData.key, text)
            }
        }
    }

    Text {
        width: root.width
        visible: root.error !== ""
        text: root.error
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        font.family: root.panel ? root.panel.contentFontFamily : Style.font.family
        font.pixelSize: Style.font.bodySmall
        font.italic: true
        color: "#e06060"
    }

    Row {
        width: root.width
        height: Style.spacing.controlHeight + 4
        spacing: Style.space(8)

        Button {
            text: "Cancel"
            width: (root.width - Style.space(8)) / 2
            onClicked: root.panel ? root.panel.close() : 0
        }
        Button {
            text: root.busy ? "Setting up..." : "Set up"
            width: (root.width - Style.space(8)) / 2
            enabled: !root.busy
            onClicked: root.submit()
        }
    }

    // Pass-through model: fields write into a plain string map on the panel.
    property var _fields: ({ url: "", name: "", user: "", pass: "" })
    function setField(key, value) {
        _fields[key] = value
    }

    function submit() {
        var u = String(_fields.url || "").trim()
        var n = (String(_fields.name || "").trim()) || "Calendar"
        var user = (String(_fields.user || "")).trim()
        var pass = String(_fields.pass || "")
        if (!u) { root.error = "Server URL is required."; return }
        if (!user) { root.error = "Username is required."; return }
        if (!pass) { root.error = "Password is required."; return }
        root.error = ""
        root.busy = true

        var xhr = new XMLHttpRequest()
        xhr.open("POST", "http://127.0.0.1:9876/api/setup", true)
        xhr.timeout = 45000
        xhr.setRequestHeader("Content-Type", "application/json")
        xhr.onreadystatechange = function() {
            if (xhr.readyState !== XMLHttpRequest.DONE) return
            root.busy = false
            var data = null
            try { data = JSON.parse(xhr.responseText) } catch (e) { data = null }
            if (xhr.status === 200 && data && data.ok) {
                // Success: switch to the calendar view and refresh.
                if (root.panel) {
                    root.panel.needsSetup = false
                    root.panel.viewMode = "month"
                    root.panel.refresh()
                }
            } else {
                root.error = (data && data.error) || "Setup failed: server returned status " + xhr.status
            }
        }
        xhr.onerror = function() { root.busy = false; root.error = "Could not reach the omacal backend." }
        xhr.ontimeout = function() { root.busy = false; root.error = "Setup timed out contacting the backend." }
        xhr.send(JSON.stringify({ url: u, display_name: n, username: user, password: pass }))
    }
}