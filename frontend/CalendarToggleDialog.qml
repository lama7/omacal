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
  signal addSubmit(string name, string color, string url, string provider)
  signal addCancel()
  signal navRequested(int dx)
  signal deleteRequested(int id)
  signal editRequested(int id)
  signal editSubmit(int id, string name, string color)
  signal editCancel()

  // Hex colors already used by the visible calendars (set by the panel from
  // the /api/calendars response). The add-calendar color picker hides palette
  // swatches that are "close" to any of these: RGB Euclidean distance < 60
  // (scale 0..441), so a new calendar never gets a color that looks the same
  // as an existing one.
  property var usedColors: []

  // Add-calendar mini form state. The panel flips this on "+" click; the
  // field/buttons live inside the card while it is true. The POST stays in
  // the panel (addSubmit carries the typed name to it).
  property bool addOpen: false
  property string pendingAddName: ""
  property string pendingAddColor: ""
  property string pendingAddUrl: ""
  property string pendingAddProvider: ""
  property alias nameFieldRef: nameField

  // Edit-calendar mini form state. The panel flips editOpen on the row edit
  // button / 'e'; the form lives in the card while true (the list hides, like
  // the add form). editId is the row being edited; the PUT stays in the panel
  // (editSubmit carries the typed name/color to it).
  property bool editOpen: false
  property int editId: 0
  property string pendingEditName: ""
  property string pendingEditColor: ""
  // Current name/color of the row being edited (set by the panel when the
  // form opens): the name pre-populates the text field, the color shows as
  // a labeled dot so the user can see and keep/revert it.
  property string editCurrentName: ""
  property string editCurrentColor: ""
  property alias editNameFieldRef: editNameField

  // Color palette for the picker. Swatches "close" to a color already used by
  // a visible calendar are hidden so a new calendar never looks like an
  // existing one. "Close" = RGB Euclidean distance < 60 (scale 0..441):
  // deterministic, not subjective — hex codes themselves are not perceptually
  // ordered, so the comparison happens in RGB space after parsing.
  //
  // The picker always offers at least MIN_SWATCHES (4): the palette is 8 wide,
  // so with many calendars the strict filter can leave fewer than 4. Rather
  // than starving the picker, swatches are RANKED by how far they are from the
  // closest used color — everything at distance >= 60 is preferred, then the
  // least-colliding hidden colors top the list back up to 4. So the "no
  // duplicate colors" rule is honored whenever it can be, and degrades
  // gracefully (closest-first) when it cannot.
  readonly property int minSwatches: 4
  property var palette: [
    "#d93025", "#e8710a", "#f2b134", "#8f68ff",
    "#1a73e8", "#12b5e0", "#1e8e3e", "#e91e63"
  ]
  function hexToRgb(hex) {
    return [
      parseInt(hex.substring(1, 3), 16),
      parseInt(hex.substring(3, 5), 16),
      parseInt(hex.substring(5, 7), 16)
    ]
  }
  // Distance from `hex` to the nearest color already in use (441 = max
  // possible, i.e. nothing in use).
  function colorDistanceToUsed(hex) {
    var rgb = hexToRgb(hex)
    var best = 441
    for (var i = 0; i < usedColors.length; i++) {
      var u = hexToRgb(String(usedColors[i]))
      var dr = rgb[0] - u[0]
      var dg = rgb[1] - u[1]
      var db = rgb[2] - u[2]
      var d = Math.sqrt(dr * dr + dg * dg + db * db)
      if (d < best) best = d
    }
    return best
  }
  function colorTooClose(hex) {
    return colorDistanceToUsed(hex) < 60
  }
  // Ranked: strict survivors (distance >= 60) first, in palette order, then
  // the hidden colors by descending distance, trimmed to at least minSwatches
  // entries (never more than the palette itself).
  property var availableSwatches: {
    var strict = []
    var hidden = []
    for (var i = 0; i < palette.length; i++) {
      var hex = palette[i]
      if (colorTooClose(hex)) hidden.push([hex, colorDistanceToUsed(hex)])
      else strict.push(hex)
    }
    if (strict.length < minSwatches && hidden.length > 0) {
      hidden.sort(function(a, b) { return b[1] - a[1] })   // least colliding first
      for (var j = 0; strict.length < minSwatches && j < hidden.length; j++)
        strict.push(hidden[j][0])
    }
    return strict
  }

  // The Done button's slot, after all calendar rows.
  readonly property int doneIndex: root.model.count

  // Keyboard selection follows the scroll: when arrows/Tab move the cursor
  // past the visible window, re-anchor contentY so the selected row stays on
  // screen. Row pitch matches estimatedContentHeight's terms (58px rows +
  // 4px gaps); the window is the Flickable's own height.
  function scrollToSelection() {
    if (root.selectedIndex >= root.doneIndex) return   // Done button, no row
    var rowStep = Style.space(58) + Style.space(4)
    var window = Math.max(listArea.height, 0)
    var target = root.selectedIndex * rowStep - Math.max(window - rowStep, 0) / 2
    listArea.contentY = Math.max(0, Math.min(target, estimatedContentHeight - window))
  }

  // Stable content estimate: N rows of ~58px + 4px gaps, derived from
  // model.count alone. Used for the card height, the Flickable contentHeight,
  // the content Item height, and the 'is it scrollable' test -- so they all
  // know immediately and never depend on the live listLayout.implicitHeight,
  // which is 0 for one frame on every model rebuild (the source of the blip).
  readonly property int estimatedContentHeight:
      root.model.count * Style.space(58) + Math.max(root.model.count - 1, 0) * Style.space(4)

  // The card's height WITHOUT the parent clamp — the panel feeds this into its
  // own contentHeight so opening the dialog in a short view (day view) grows
  // the popup to fit the card, instead of the card being squeezed into the
  // shorter main view. Same terms as the card's own height sum, so the two
  // agree exactly; the card still clamps to the parent as a floor guard.
  // While the add-calendar mini form is open the list and Done button are
  // hidden, so the middle term is the form's own height instead.
  readonly property int naturalCardHeight:
      Style.space(16)
      + Math.max(titleText.implicitHeight, addBtn.height)
      + Style.space(10)
      + ((root.addOpen || root.editOpen) ? (root.addOpen ? addForm.implicitHeight : editForm.implicitHeight) : estimatedContentHeight)
      + Style.space(10)
      + ((root.addOpen || root.editOpen) ? 0 : doneBtn.height)
      + Style.space(16)

  function handleKey(event) {
    if (!root.opened) return false
    // While the add-calendar mini form is open: Escape cancels the form
    // (back to the list), Enter/Space submits if a name is typed, arrows
    // navigate months in the panel behind the dialog (the focused TextField
    // swallows arrows as caret moves first; these reach here only when the
    // field does NOT hold focus).
    // While the edit-calendar mini form is open: Escape cancels (back to the
    // list), Enter submits if a name or color was picked; arrows navigate
    // months in the panel behind the dialog (same routing as the add form).
    if (root.editOpen) {
      if (event.key === Qt.Key_Escape) { root.editCancel(); return true }
      if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
        if (root.pendingEditName !== "" || root.pendingEditColor !== "")
          root.editSubmit(root.editId, root.pendingEditName, root.pendingEditColor)
        return true
      }
      if (event.key === Qt.Key_Left || event.key === Qt.Key_Right) {
        root.navRequested(event.key === Qt.Key_Left ? -1 : 1)
        return true
      }
      return false
    }
    if (root.addOpen) {
      if (event.key === Qt.Key_Escape) { root.addCancel(); return true }
      if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
        if (root.pendingAddName !== "" || root.pendingAddUrl !== "") root.addSubmit(root.pendingAddName, root.pendingAddColor, root.pendingAddUrl, root.pendingAddProvider)
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
      root.scrollToSelection()
      return true
    } else if (event.key === Qt.Key_Right || event.key === Qt.Key_Tab) {
      root.selectedIndex = (root.selectedIndex + 1) % total
      root.scrollToSelection()
      return true
    } else if (event.key === Qt.Key_Plus || event.text === "+") {
      // '+' opens the add-calendar mini form (same as clicking the + button).
      root.addRequested()
      return true
    } else if (event.key === Qt.Key_D || event.text === "d" || event.text === "D") {
      // 'd' deletes the calendar under the keyboard cursor (same flow as
      // right-click). Only for calendars in the signed-in user's namespace.
      var m = root.model.get(root.selectedIndex)
      if (m && m.deletable) root.deleteRequested(m.id)
      return true
    } else if (event.key === Qt.Key_E || event.text === "e" || event.text === "E") {
      // 'e' opens the edit mini form for the row under the keyboard cursor
      // (owned calendars only; same flow as the row's edit button).
      var m = root.model.get(root.selectedIndex)
      if (m && m.editable) root.editRequested(m.id)
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
    height: Math.min(parent.height - Style.space(48), naturalCardHeight)
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

        // Title spans the row (minus the + button) and centers within it.
        Text {
          id: titleText
          Layout.fillWidth: true
          textFormat: Text.PlainText
          // While the add-calendar mini form is open the header reads
          // "Add Calendar" and the + button is hidden (it opened the form).
          text: root.addOpen ? "Add Calendar" : root.editOpen ? "Edit Calendar" : "Calendars"
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: 24
          font.bold: true
          horizontalAlignment: Text.AlignHCenter
          wrapMode: Text.WordWrap
        }

        // Add-Calendar button: same glyph as the day-view Add Event button
        // (U+F00F3, surrogate pair so it renders on every JS engine).
        PanelActionButton {
          id: addBtn
          iconText: "\uDB80\uDCF3"
          fontSize: 22
          tooltipText: "Add Calendar"
          foreground: root.foreground
          fontFamily: root.fontFamily
          visible: !root.addOpen && !root.editOpen
          enabled: !root.addOpen && !root.editOpen
          onClicked: root.addRequested()
        }
      }

      // Horizontal separator under the header, same as the month/day views.
      PanelSeparator {
        Layout.fillWidth: true
        foreground: root.foreground
      }

      // Scrollable list so many calendars never push the card off-screen.
      // Hidden while the add-calendar mini form is open (the form replaces
      // the list; the card height stays put since it is model-count driven).
      Flickable {
        id: listArea
        visible: !root.addOpen && !root.editOpen
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

              Item {
                required property var modelData
                required property int index
                // implicitWidth matters: the ColumnLayout's own width derives
                // from children's implicit widths, and a bare Item is 0 —
                // Layout.fillWidth alone would collapse the row to zero width.
                implicitWidth: rowToggle.implicitWidth
                Layout.fillWidth: true
                Layout.preferredHeight: rowToggle.implicitHeight

                Toggle {
                  id: rowToggle
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.top: parent.top
                  anchors.bottom: parent.bottom
                  // Reserve the edit-button gutter on EVERY row so all
                  // toggles stay uniform width; the button itself is
                  // right-justified and only shown on owned (editable) rows.
                  anchors.rightMargin: rowEditBtn.width + Style.space(8)
                  label: modelData.name
                  description: modelData.enabled ? "Visible" : "Hidden"
                  checked: modelData.enabled
                  hasCursor: index === root.selectedIndex
                  foreground: root.foreground
                  accent: root.accent
                  fontFamily: root.fontFamily
                  onClicked: root.toggled(modelData.id, !modelData.enabled)
                }

                // Right-click overlay: right-clicking a row asks to delete
                // that calendar (mirrors the event rows' right-click ->
                // delete-confirm flow). Only accepted for calendars in the
                // signed-in user's own namespace (deletable flag from the
                // API); the server is the final arbiter. acceptedButtons is
                // right-only, so left-clicks fall through to the Toggle.
                MouseArea {
                  anchors.fill: parent
                  acceptedButtons: Qt.RightButton
                  onClicked: if (modelData.deletable) root.deleteRequested(modelData.id)
                }

                // Edit button: Material Symbols "edit_calendar" (U+F08A7,
                // surrogate pair so it renders on every JS engine). Owned
                // calendars only (editable flag from the panel); shared and
                // subscribed rows never show it.
                PanelActionButton {
                  id: rowEditBtn
                  anchors.right: parent.right
                  anchors.rightMargin: Style.space(6)
                  anchors.verticalCenter: parent.verticalCenter
                  iconText: "\uDB82\uDCA7"
                  fontSize: 22
                  tooltipText: "Edit calendar"
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                  visible: modelData.editable
                  enabled: modelData.editable
                  onClicked: root.editRequested(modelData.id)
                }
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
          Keys.onReturnPressed: root.addSubmit(root.pendingAddName, root.pendingAddColor, root.pendingAddUrl, root.pendingAddProvider)
          Keys.onEnterPressed: root.addSubmit(root.pendingAddName, root.pendingAddColor, root.pendingAddUrl, root.pendingAddProvider)
          Keys.onEscapePressed: { root.pendingAddName = ""; root.pendingAddUrl = ""; root.pendingAddProvider = ""; root.addCancel() }
        }

        TextField {
          id: urlField
          Layout.fillWidth: true
          Layout.preferredHeight: Math.max(implicitHeight, Style.spacing.controlHeight)
          // Subscribe-by-URL: paste an existing calendar's collection URL
          // (or a public .ics). Filling this in switches the submit from
          // "create a new calendar" to "subscribe to that calendar" — the
          // name above becomes an optional label override.
          placeholderText: "Calendar URL (subscribe)"
          foreground: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.subtitle
          onTextChanged: if (text !== "") root.pendingAddUrl = text
          Keys.onReturnPressed: root.addSubmit(root.pendingAddName, root.pendingAddColor, root.pendingAddUrl, root.pendingAddProvider)
          Keys.onEnterPressed: root.addSubmit(root.pendingAddName, root.pendingAddColor, root.pendingAddUrl, root.pendingAddProvider)
          Keys.onEscapePressed: { root.pendingAddUrl = ""; root.pendingAddProvider = ""; root.addCancel() }
        }

        // Provider switch: checked creates the calendar on the consented Google
        // account instead of the configured CalDAV server. Unchecked keeps the
        // default (CalDAV when an account is configured). Consent errors come
        // from the backend with a clear message.
        CheckBox {
          id: googleBtn
          Layout.fillWidth: true
          text: "Create on Google"
          checked: root.pendingAddProvider === "google"
          foreground: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.subtitle
          onCheckedStateChanged: root.pendingAddProvider = checked ? "google" : ""
        }

        // Color picker: preset swatches (Google-Calendar style). Click one to
        // pick the calendar's color; leave it unset for the server default.
        // The chosen hex rides along on addSubmit to the backend, which PROPPATCHes
        // the CalDAV calendar-color property and caches it in the DB.
        Text {
          // Caption above the swatch row so the row reads as a picker.
          Layout.fillWidth: true
          textFormat: Text.PlainText
          text: "Select calendar color:"
          color: root.foreground
          opacity: 0.6
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
        RowLayout {
          id: swatchRow
          Layout.fillWidth: true
          spacing: Style.space(8)

          Repeater {
            id: swatches
            model: root.availableSwatches   // palette minus colors close to existing calendars'

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
            // Hover highlight: same fill/border treatment as the Done button's
            // cursor cue, so the add-form buttons read like the other controls.
            readonly property bool hot: createHover.containsMouse
            color: hot
              ? Style.controlFill(false, true, root.foreground, root.accent)
              : "transparent"
            border.color: hot
              ? Style.controlBorder(false, true, root.foreground, root.accent)
              : root.accent
            border.width: hot
              ? Style.controlBorderWidth(false, true)
              : Style.normalBorderWidth
            Behavior on color { ColorAnimation { duration: 100 } }
            Text {
              textFormat: Text.PlainText
              anchors.centerIn: parent
              // Subscribe mode: a URL was typed, so the action is subscribing
              // to an existing calendar, not creating a new one.
              text: root.pendingAddUrl !== "" ? "Subscribe" : "Create"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.subtitle
              font.bold: true
            }
            MouseArea {
              id: createHover
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: root.addSubmit(root.pendingAddName, root.pendingAddColor, root.pendingAddUrl, root.pendingAddProvider)
            }
          }
          Rectangle {
            id: cancelBtn
            Layout.preferredWidth: Style.space(96)
            height: Style.space(38)
            radius: root.cornerRadius
            // Hover highlight: same fill/border treatment as the Done button's
            // cursor cue, so the add-form buttons read like the other controls.
            readonly property bool hot: cancelHover.containsMouse
            color: hot
              ? Style.controlFill(false, true, root.foreground, root.accent)
              : "transparent"
            border.color: hot
              ? Style.controlBorder(false, true, root.foreground, root.accent)
              : root.accent
            border.width: hot
              ? Style.controlBorderWidth(false, true)
              : Style.normalBorderWidth
            Behavior on color { ColorAnimation { duration: 100 } }
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
              id: cancelHover
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: root.addCancel()
            }
          }
        }
      }

      // Edit-calendar mini form: replaces the list once a row's edit button
      // (or 'e') fires. Name and color are both optional — leave one alone to
      // keep it; Save needs at least one (the panel guards the empty case).
      // The panel owns the PUT via editSubmit(id, name, color).
      ColumnLayout {
        id: editForm
        visible: root.editOpen
        Layout.fillWidth: true
        spacing: Style.space(8)

        TextField {
          id: editNameField
          Layout.fillWidth: true
          Layout.preferredHeight: Math.max(implicitHeight, Style.spacing.controlHeight)
          placeholderText: "New calendar name"
          foreground: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.subtitle
          onTextChanged: if (text !== "") root.pendingEditName = text
          Keys.onReturnPressed: root.editSubmit(root.editId, root.pendingEditName, root.pendingEditColor)
          Keys.onEnterPressed: root.editSubmit(root.editId, root.pendingEditName, root.pendingEditColor)
          Keys.onEscapePressed: { root.pendingEditName = ""; root.editCancel() }
        }

        Text {
          Layout.fillWidth: true
          textFormat: Text.PlainText
          text: "Current color:"
          color: root.foreground
          opacity: 0.6
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
        Rectangle {
          id: currentColorDot
          Layout.preferredWidth: Style.space(22)
          Layout.preferredHeight: Style.space(22)
          radius: width / 2
          color: root.editCurrentColor
          border.color: root.foreground
          border.width: (root.pendingEditColor === root.editCurrentColor)
              ? Style.normalBorderWidth + 1 : 0
          MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: root.pendingEditColor = root.editCurrentColor
          }
        }

        Text {
          Layout.fillWidth: true
          textFormat: Text.PlainText
          text: "Select calendar color:"
          color: root.foreground
          opacity: 0.6
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
        RowLayout {
          id: editSwatchRow
          Layout.fillWidth: true
          spacing: Style.space(8)

          Repeater {
            model: root.availableSwatches

            Rectangle {
              required property string modelData
              required property int index
              Layout.preferredWidth: Style.space(22)
              Layout.preferredHeight: Style.space(22)
              radius: width / 2
              color: modelData
              border.color: root.foreground
              border.width: (root.pendingEditColor === modelData)
                  ? Style.normalBorderWidth + 1 : 0
              MouseArea {
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.pendingEditColor = modelData
              }
            }
          }
        }

        RowLayout {
          id: editBtnRow
          Layout.fillWidth: true
          spacing: Style.space(10)

          Item { Layout.fillWidth: true }   // spacer pushes Save/Cancel right

          Rectangle {
            id: saveBtn
            Layout.preferredWidth: Style.space(96)
            height: Style.space(38)
            radius: root.cornerRadius
            readonly property bool hot: saveHover.containsMouse
            color: hot
              ? Style.controlFill(false, true, root.foreground, root.accent)
              : "transparent"
            border.color: hot
              ? Style.controlBorder(false, true, root.foreground, root.accent)
              : root.accent
            border.width: hot
              ? Style.controlBorderWidth(false, true)
              : Style.normalBorderWidth
            Behavior on color { ColorAnimation { duration: 100 } }
            Text {
              textFormat: Text.PlainText
              anchors.centerIn: parent
              text: "Save"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.subtitle
              font.bold: true
            }
            MouseArea {
              id: saveHover
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: root.editSubmit(root.editId, root.pendingEditName, root.pendingEditColor)
            }
          }
          Rectangle {
            id: editCancelBtn
            Layout.preferredWidth: Style.space(96)
            height: Style.space(38)
            radius: root.cornerRadius
            readonly property bool hot: editCancelHover.containsMouse
            color: hot
              ? Style.controlFill(false, true, root.foreground, root.accent)
              : "transparent"
            border.color: hot
              ? Style.controlBorder(false, true, root.foreground, root.accent)
              : root.accent
            border.width: hot
              ? Style.controlBorderWidth(false, true)
              : Style.normalBorderWidth
            Behavior on color { ColorAnimation { duration: 100 } }
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
              id: editCancelHover
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: root.editCancel()
            }
          }
        }
      }

      RowLayout {
        id: buttonRow
        Layout.fillWidth: true
        visible: !root.addOpen && !root.editOpen
        spacing: Style.space(10)

        Item { Layout.fillWidth: true }   // spacer pushes Done to the right

        Rectangle {
          id: doneBtn
          width: Style.space(96)
          height: Style.space(38)
          radius: root.cornerRadius
          // Keyboard-cursor cue: when Tab/Shift+Tab parks the cursor on the
          // Done slot, paint the same hover-cursor fill/border the Toggles
          // use for hasCursor, so the keyboard position is visible. (Done
          // lives in the button row outside the Flickable, so it is always
          // on screen; the highlight is the whole fix.)
          // hoverHot mirrors the Toggles' hasCursor hover highlight.
          readonly property bool hot: root.opened && !root.addOpen
              && (root.selectedIndex === root.doneIndex || doneHover.containsMouse)
          color: hot
            ? Style.controlFill(false, true, root.foreground, root.accent)
            : "transparent"
          border.color: hot
            ? Style.controlBorder(false, true, root.foreground, root.accent)
            : root.accent
          border.width: hot
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
