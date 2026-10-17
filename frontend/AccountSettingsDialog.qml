import QtQuick
import Quickshell
import QtQuick.Layouts
import qs.Commons
import qs.Ui

// Account settings dialog: opened by the header's U+2261 (triple-bar) button.
// Shows one Toggle row per ACCOUNT — a config.json calendars[] entry, supplied
// by the panel via GET /api/account (CalDAV accounts keyed by their principal
// URL, the Google account by "google"). Toggling fires accountToggled(key,
// enabled); the panel owns the POST /api/account/toggle, so this dialog stays
// presentational.
//
// `model` is a ListModel of { key, name, enabled } built by the panel. It is a
// ListModel (not a plain JS array) so a toggle flips ONE row in place via
// setProperty() — a plain-array rebuild would tear down every delegate, and a
// re-created Toggle appears already-checked so its knob never animates.
//
// Height discipline (the "flashing Done button" bug): every row has a FIXED
// height and the card height is derived from model.count alone, never from a
// live layout implicitHeight. A toggle's setProperty re-evaluates one
// delegate; if a row were sized by live content, its height could change for a
// frame and shift every row below it — including the Done button — which is
// the flash. Same reasoning as CalendarToggleDialog's estimatedContentHeight.
//
// Add Provider (the header "+" button, or the first-run notice's Continue):
// an in-place mini form that replaces the list while addOpen is true, exactly
// like CalendarToggleDialog's add-calendar form. Stage "" is a provider
// chooser; "signin" runs Nextcloud Login Flow v2 (browser sign-in, no OAuth
// client registration, no admin rights); "credentials" adds any CalDAV account
// from server + username + password (a Nextcloud app password works here too).
// Both paths take an optional ACCOUNT NAME (config.json `name`) so several
// CalDAV or Google accounts can be told apart — distinct from `display_name`,
// which is the primary calendar's name captured from the server.
// The panel owns every POST and flips addOpen/addStage and the pending fields,
// so this dialog stays presentational. Google (BYO client) is a later stage.
//
// Soft-disable semantics: toggling an account off persists the flag on its
// config.json calendars[] entry and flips the enabled column on every DB row of
// that account (view/edit queries already filter on c.enabled = 1), and its sync
// source is skipped; nothing is deleted, so toggling back on restores instantly
// without re-syncing. Per-account, not per-provider: every CalDAV account shares
// provider='caldav', so gating keys on the account (principal URL).

Item {
  id: root
  property bool opened: false
  // The keyboard-cursor cue stays hidden until the user actually navigates
  // (arrows/Tab in handleKey). Otherwise the first account row / the first
  // provider choice is painted with the hot fill on open and reads as a stuck
  // hover. Mouse hover has its own cue and is unaffected.
  property bool cursorEngaged: false
  onOpenedChanged: if (opened) cursorEngaged = false
  // Account rows as a ListModel of { key, name, enabled } (panel-built).
  property var model: null
  readonly property int accountCount: model ? model.count : 0
  // Account detail view (the row's edit button): the account object from
  // /api/account, or null for the list. Read-only for now — it shows the
  // non-secret settings (the password lives in the keyring and is never
  // fetched). The panel sets it; the dialog emits accountViewClosed() to go
  // back.
  property var viewAccount: null
  readonly property bool viewOpen: viewAccount !== null
  // Label/value pairs for the detail view, in display order.
  function viewRows() {
    var a = root.viewAccount || {}
    var rows = [
      { label: "Account name", value: String(a.name || "") },
      { label: "Provider", value: a.provider === "google" ? "Google" : "CalDAV" },
    ]
    if (a.username) rows.push({ label: "Username", value: String(a.username) })
    if (a.url) rows.push({ label: "Server", value: String(a.url) })
    rows.push({ label: "Calendars", value: String(a.calendars != null ? a.calendars : 0) })
    rows.push({ label: "Status", value: a.enabled ? "Enabled" : "Disabled" })
    return rows
  }
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
  signal accountToggled(string key, bool enabled)
  signal accountEditRequested(string key)
  signal accountViewClosed()

  // --- Add-provider form (panel-driven, like CalendarToggleDialog's) --------
  property bool addOpen: false
  // "" = provider chooser; "signin" = Nextcloud browser sign-in;
  // "credentials" = CalDAV server + username + password.
  property string addStage: ""
  // Keyboard cursor within the provider chooser (this panel is keyboard-driven).
  property int chooserIndex: 0
  // Pending field values. The server field is shared between the two Nextcloud
  // stages, so switching stage carries the typed server over; `pendingName` is
  // the optional account label, shared by both stages too.
  property string pendingServer: ""
  property string pendingName: ""
  property string pendingUser: ""
  property string pendingPass: ""
  property alias serverFieldRef: serverField
  property alias caldavServerFieldRef: caldavServerField
  // Feedback shown by the panel while the flow runs / on failure.
  property bool addBusy: false
  property string addStatus: ""
  property string addError: ""
  // Chooser options, in cursor order.
  readonly property var providerChoices: [
    { key: "signin", title: "Nextcloud",
      detail: "Sign in with your browser \u2014 no server password to type." },
    { key: "credentials", title: "CalDAV server",
      detail: "Enter a server, username and password or app password." },
  ]

  signal addRequested()
  signal addCancel()
  signal providerChoiceRequested(string key)
  signal navBack()
  signal ncSignInRequested(string server, string name)
  signal caldavAddSubmit(string url, string name, string user, string pass)

  // Keyboard cursor: which account row is selected (0..count-1).
  property int selectedIndex: 0

  // Stable content estimate (same discipline as CalendarToggleDialog): one
  // fixed-height toggle row per account, plus a small gap between rows. The
  // card height and the rows region are both derived from this, so a toggle
  // press never changes them.
  readonly property int rowHeight: Style.space(58)
  readonly property int rowGap: Style.space(4)
  readonly property int estimatedRowsHeight:
      accountCount * rowHeight + Math.max(accountCount - 1, 0) * rowGap

  readonly property int gap: Style.space(10)
  readonly property int naturalCardHeight:
      Style.space(16) * 2
      + titleText.implicitHeight
      + sep.implicitHeight
      + (root.addOpen
          ? addForm.implicitHeight + gap * 2
          : root.viewOpen
            ? viewForm.implicitHeight + gap * 2
            : estimatedRowsHeight + doneBtn.height + gap * 4)

  // Reset the add form to its chooser stage (the panel calls this when it
  // opens the form, so a previous attempt never leaks into a fresh one).
  // Clear the pending values AND the field texts: setting the pending value
  // alone leaves the TextField still showing the last entry (the field mirrors
  // into the pending value, never back out), which is how stale input leaked
  // into a later add. Called on open and on cancel — not on Back, which
  // deliberately carries the typed server between the two Nextcloud stages.
  function resetAddForm() {
    serverField.text = ""
    signinNameField.text = ""
    caldavServerField.text = ""
    nameField.text = ""
    userField.text = ""
    passField.text = ""
    addStage = ""
    chooserIndex = 0
    cursorEngaged = false
    pendingServer = ""
    pendingName = ""
    pendingUser = ""
    pendingPass = ""
    addBusy = false
    addStatus = ""
    addError = ""
  }
  function chooseProvider(key) {
    root.providerChoiceRequested(key)
  }

  // Keyboard: arrows/Tab move between account rows, Enter/Space toggles the
  // selected row (mirrors the calendar dialog), Escape closes. Everything
  // else is swallowed while open so stray keys never drive the month view.
  function handleKey(event) {
    if (!root.opened) return false
    if (root.viewOpen) {
      // Escape/Back returns to the account list (nothing here is focusable).
      if (event.key === Qt.Key_Escape || event.key === Qt.Key_Backspace) {
        root.accountViewClosed()
        return true
      }
      return true
    }
    if (root.addOpen) {
      // Escape leaves the form (back to the chooser, then back to the list).
      if (event.key === Qt.Key_Escape) {
        if (root.addStage === "") root.addCancel()
        else root.navBack()
        return true
      }
      if (root.addStage === "") {
        // Chooser: arrows/Tab move the cursor, Enter activates the choice.
        if (event.key === Qt.Key_Left || event.key === Qt.Key_Up
            || event.key === Qt.Key_Backtab) {
          chooserIndex = (chooserIndex + providerChoices.length - 1)
              % providerChoices.length
          cursorEngaged = true
          return true
        }
        if (event.key === Qt.Key_Right || event.key === Qt.Key_Down
            || event.key === Qt.Key_Tab) {
          chooserIndex = (chooserIndex + 1) % providerChoices.length
          cursorEngaged = true
          return true
        }
        if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter
            || event.key === Qt.Key_Space) {
          root.chooseProvider(providerChoices[chooserIndex].key)
          return true
        }
      }
      // In a sub-form the focused TextField owns Enter (its Keys.onReturnPressed
      // submits) and Escape (back to the chooser); unhandled keys fall through
      // so the fields receive their characters.
      return false
    }
    if (event.key === Qt.Key_Escape) { root.closed(); return true }
    var n = Math.max(accountCount, 1)
    if (event.key === Qt.Key_Left || event.key === Qt.Key_Backtab) {
      selectedIndex = (selectedIndex + n - 1) % n
      cursorEngaged = true
      return true
    }
    if (event.key === Qt.Key_Right || event.key === Qt.Key_Tab) {
      selectedIndex = (selectedIndex + 1) % n
      cursorEngaged = true
      return true
    }
    if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter
        || event.key === Qt.Key_Space) {
      if (selectedIndex < accountCount) {
        var m = model.get(selectedIndex)
        root.accountToggled(m.key, !m.enabled)
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
        spacing: Style.space(8)

        Text {
          id: titleText
          Layout.fillWidth: true
          textFormat: Text.PlainText
          // While the add-provider form is open the header reads "Add Provider"
          // and the + button is hidden (it opened the form).
          text: root.addOpen ? "Add Provider"
              : (root.viewOpen ? String(root.viewAccount.name || "Account")
                               : "Account Settings")
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: 24
          font.bold: true
          horizontalAlignment: Text.AlignHCenter
          wrapMode: Text.WordWrap
        }

        // Add-Provider button: same glyph as the Calendars dialog's add button
        // and the day-view Add Event button (U+F00F3).
        PanelActionButton {
          id: addBtn
          iconText: "\uDB80\uDCF3"
          fontSize: 22
          tooltipText: "Add Provider"
          foreground: root.foreground
          fontFamily: root.fontFamily
          visible: !root.addOpen && !root.viewOpen
          enabled: !root.addOpen && !root.viewOpen
          onClicked: root.addRequested()
        }
      }

      // Horizontal separator under the header, same as the other views.
      PanelSeparator {
        id: sep
        Layout.fillWidth: true
        foreground: root.foreground
      }

      // One fixed-height Toggle row per account. Bound to the panel's
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
          visible: !root.addOpen && !root.viewOpen
          // implicitWidth matters: the ColumnLayout derives its own width from
          // children's implicit widths, and a bare Item is 0.
          implicitWidth: rowToggle.implicitWidth
          Layout.fillWidth: true
          Layout.preferredHeight: root.rowHeight

          DialogToggle {
            id: rowToggle
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            // Reserve the action-button gutter on EVERY row so all toggles are
            // the same width and account names still fit.
            anchors.rightMargin: rowEditBtn.width + Style.space(8)
            label: provRow.modelData.name
            description: provRow.modelData.enabled ? "Enabled" : "Disabled"
            checked: provRow.modelData.enabled
            cursorOn: root.cursorEngaged
                && provRow.index === root.selectedIndex
            foreground: root.foreground
            accent: root.accent
            fontFamily: root.fontFamily
            onClicked: root.accountToggled(provRow.modelData.key,
                                           !provRow.modelData.enabled)
          }

          // Per-account action, right-justified in the row: the same
          // edit_calendar glyph (U+F08A7) the calendar rows use. Its behaviour
          // is not decided yet, so the dialog only emits accountEditRequested
          // and stays presentational (the panel owns whatever it does).
          PanelActionButton {
            id: rowEditBtn
            anchors.right: parent.right
            anchors.rightMargin: Style.space(6)
            anchors.verticalCenter: parent.verticalCenter
            iconText: "\uDB82\uDCA7"
            fontSize: 22
            tooltipText: "Edit account"
            foreground: root.foreground
            fontFamily: root.fontFamily
            onClicked: root.accountEditRequested(provRow.modelData.key)
          }
        }
      }

      // Flexible spacer: absorbs any slack between the model-count estimate and
      // the real layout, pinning the Done button to the bottom of the card so
      // it never moves as rows change state.
      Item {
        Layout.fillWidth: true
        Layout.fillHeight: true
        visible: !root.addOpen && !root.viewOpen
      }

      RowLayout {
        id: buttonRow
        Layout.fillWidth: true
        spacing: Style.space(10)
        visible: !root.addOpen && !root.viewOpen

        Item { Layout.fillWidth: true }   // spacer pushes Done to the right

        DialogButton {
          id: doneBtn
          text: "Done"
          foreground: root.foreground
          accent: root.accent
          fontFamily: root.fontFamily
          onClicked: root.closed()
        }
      }

      // Account detail view: replaces the list while viewOpen is true (the
      // row's edit button). Read-only — the non-secret settings only; the
      // password lives in the keyring and is never fetched. Editing comes
      // later.
      ColumnLayout {
        id: viewForm
        visible: root.viewOpen
        Layout.fillWidth: true
        spacing: Style.space(10)

        Repeater {
          model: root.viewRows()

          RowLayout {
            required property var modelData
            Layout.fillWidth: true
            spacing: Style.space(8)

            Text {
              Layout.preferredWidth: Style.space(110)
              textFormat: Text.PlainText
              text: modelData.label
              color: root.foreground
              opacity: 0.6
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }
            Text {
              Layout.fillWidth: true
              textFormat: Text.PlainText
              wrapMode: Text.WrapAnywhere
              text: modelData.value
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }
          }
        }

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(10)

          Item { Layout.fillWidth: true }   // spacer pushes buttons right

          DialogButton {
            text: "Back"
            foreground: root.foreground
            accent: root.accent
            fontFamily: root.fontFamily
            onClicked: root.accountViewClosed()
          }
          DialogButton {
            text: "Done"
            foreground: root.foreground
            accent: root.accent
            fontFamily: root.fontFamily
            onClicked: root.closed()
          }
        }
      }

      // Add-provider mini form: replaces the list while addOpen is true. The
      // panel owns every POST; this only emits and displays the stage state.
      ColumnLayout {
        id: addForm
        visible: root.addOpen
        Layout.fillWidth: true
        spacing: Style.space(8)

        // ---- Stage "": provider chooser ----
        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.space(6)
          visible: root.addStage === ""

          Text {
            Layout.fillWidth: true
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            text: "Connect a calendar account"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            font.bold: true
          }

          Repeater {
            model: root.providerChoices

            Rectangle {
              id: choiceRow
              required property var modelData
              required property int index
              Layout.fillWidth: true
              // Two cues, deliberately different: the KEYBOARD cursor is
              // painted explicitly with the theme accent (tinted fill +
              // thicker border) so the selection reads clearly; hover keeps
              // the kit's hover tokens. The kit's focus tokens can't be used
              // here because this theme mirrors them onto the hover tokens,
              // so focus would render identical to hover.
              readonly property bool cursorOn: root.cursorEngaged
                  && root.chooserIndex === choiceRow.index
              readonly property bool hot: hover.containsMouse || cursorOn
              implicitHeight: choiceCol.implicitHeight + Style.space(16)
              radius: root.cornerRadius
              color: cursorOn
                ? Util.alpha(root.accent, 0.18)
                : (hot ? Style.controlFill(false, true, root.foreground,
                                           root.accent)
                       : "transparent")
              border.color: cursorOn
                ? root.accent
                : (hot ? Style.controlBorder(false, true, root.foreground,
                                             root.accent)
                       : root.accent)
              border.width: cursorOn
                ? Style.normalBorderWidth + 1
                : (hot ? Style.controlBorderWidth(false, true)
                       : Style.normalBorderWidth)
              Behavior on color { ColorAnimation { duration: 100 } }

              Column {
                id: choiceCol
                anchors.fill: parent
                anchors.margins: Style.space(8)
                spacing: Style.space(2)
                Text {
                  textFormat: Text.PlainText
                  text: choiceRow.modelData.title
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.subtitle
                  font.bold: true
                }
                Text {
                  textFormat: Text.PlainText
                  width: choiceCol.width
                  wrapMode: Text.Wrap
                  text: choiceRow.modelData.detail
                  color: root.foreground
                  opacity: 0.6
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                }
              }

              MouseArea {
                id: hover
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.chooseProvider(choiceRow.modelData.key)
              }
            }
          }
        }

        // ---- Stage "signin": Nextcloud Login Flow v2 ----
        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.space(8)
          visible: root.addStage === "signin"

          Text {
            Layout.fillWidth: true
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            text: "Sign in to Nextcloud. We'll open your browser to authenticate "
                + "(2-factor codes included) and you'll get a per-device app "
                + "password you can revoke later."
            color: root.foreground
            opacity: 0.7
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          TextField {
            id: serverField
            Layout.fillWidth: true
            Layout.preferredHeight: Math.max(implicitHeight, Style.spacing.controlHeight)
            placeholderText: "Nextcloud server (e.g. cloud.example.com)"
            foreground: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.subtitle
            onTextChanged: root.pendingServer = text
            Keys.onReturnPressed: root.ncSignInRequested(root.pendingServer, root.pendingName)
            Keys.onEnterPressed: root.ncSignInRequested(root.pendingServer, root.pendingName)
            Keys.onEscapePressed: root.navBack()
          }

          TextField {
            id: signinNameField
            Layout.fillWidth: true
            Layout.preferredHeight: Math.max(implicitHeight, Style.spacing.controlHeight)
            placeholderText: "Account name (optional)"
            foreground: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.subtitle
            onTextChanged: root.pendingName = text
            Keys.onReturnPressed: root.ncSignInRequested(root.pendingServer, root.pendingName)
            Keys.onEnterPressed: root.ncSignInRequested(root.pendingServer, root.pendingName)
            Keys.onEscapePressed: root.navBack()
          }
        }

        // ---- Stage "credentials": any CalDAV account ----
        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.space(8)
          visible: root.addStage === "credentials"

          TextField {
            id: caldavServerField
            Layout.fillWidth: true
            Layout.preferredHeight: Math.max(implicitHeight, Style.spacing.controlHeight)
            placeholderText: "Server URL (e.g. https://cloud.example.com)"
            foreground: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.subtitle
            onTextChanged: root.pendingServer = text
            Keys.onReturnPressed: root.caldavAddSubmit(root.pendingServer, root.pendingName, root.pendingUser, root.pendingPass)
            Keys.onEnterPressed: root.caldavAddSubmit(root.pendingServer, root.pendingName, root.pendingUser, root.pendingPass)
            Keys.onEscapePressed: root.navBack()
          }

          TextField {
            id: nameField
            Layout.fillWidth: true
            Layout.preferredHeight: Math.max(implicitHeight, Style.spacing.controlHeight)
            placeholderText: "Account name (optional)"
            foreground: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.subtitle
            onTextChanged: root.pendingName = text
            Keys.onReturnPressed: root.caldavAddSubmit(root.pendingServer, root.pendingName, root.pendingUser, root.pendingPass)
            Keys.onEnterPressed: root.caldavAddSubmit(root.pendingServer, root.pendingName, root.pendingUser, root.pendingPass)
            Keys.onEscapePressed: root.navBack()
          }

          TextField {
            id: userField
            Layout.fillWidth: true
            Layout.preferredHeight: Math.max(implicitHeight, Style.spacing.controlHeight)
            placeholderText: "Username"
            foreground: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.subtitle
            onTextChanged: root.pendingUser = text
            Keys.onReturnPressed: root.caldavAddSubmit(root.pendingServer, root.pendingName, root.pendingUser, root.pendingPass)
            Keys.onEnterPressed: root.caldavAddSubmit(root.pendingServer, root.pendingName, root.pendingUser, root.pendingPass)
            Keys.onEscapePressed: root.navBack()
          }

          TextField {
            id: passField
            Layout.fillWidth: true
            Layout.preferredHeight: Math.max(implicitHeight, Style.spacing.controlHeight)
            placeholderText: "Password or app password"
            password: true
            foreground: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.subtitle
            onTextChanged: root.pendingPass = text
            Keys.onReturnPressed: root.caldavAddSubmit(root.pendingServer, root.pendingName, root.pendingUser, root.pendingPass)
            Keys.onEnterPressed: root.caldavAddSubmit(root.pendingServer, root.pendingName, root.pendingUser, root.pendingPass)
            Keys.onEscapePressed: root.navBack()
          }
        }

        // Shared feedback: status while working, error on failure.
        Text {
          Layout.fillWidth: true
          visible: root.addStatus !== ""
          textFormat: Text.PlainText
          wrapMode: Text.Wrap
          text: root.addStatus
          color: root.accent
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          font.italic: true
        }
        Text {
          Layout.fillWidth: true
          visible: root.addError !== ""
          textFormat: Text.PlainText
          wrapMode: Text.Wrap
          text: root.addError
          color: "#e06060"
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          font.italic: true
        }

        // Nav row: Back (sub-forms) / Cancel (always) / primary action.
        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(10)

          Item { Layout.fillWidth: true }   // spacer pushes buttons right

          DialogButton {
            text: "Back"
            visible: root.addStage !== ""
            foreground: root.foreground
            accent: root.accent
            fontFamily: root.fontFamily
            onClicked: root.navBack()
          }
          DialogButton {
            text: "Cancel"
            foreground: root.foreground
            accent: root.accent
            fontFamily: root.fontFamily
            onClicked: root.addCancel()
          }
          DialogButton {
            text: root.addStage === "signin" ? "Sign in" : "Add account"
            visible: root.addStage !== ""
            enabled: !root.addBusy
            foreground: root.foreground
            accent: root.accent
            fontFamily: root.fontFamily
            onClicked: root.addStage === "signin"
              ? root.ncSignInRequested(root.pendingServer, root.pendingName)
              : root.caldavAddSubmit(root.pendingServer, root.pendingName,
                                     root.pendingUser, root.pendingPass)
          }
        }
      }
    }
  }
}
