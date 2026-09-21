import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// The add/edit event form, extracted from omacal-panel.qml.
// A self-contained Column that binds to the panel root via the `panel`
// property (all root.* references resolve through it) and to the two
// external ids it needs: the PanelKeyCatcher (for focus return) and the
// day-view content column (for width).
Column {
    id: addEventForm
    property var panel: null
    property var keyCatcher: null
    property var dayContent: null
    property alias newEventTitleField: newEventTitleField
    property alias newEventLocationField: newEventLocationField
    property alias newEventDescriptionField: newEventDescriptionField
    property alias calendarDropdown: calendarDropdown
    property alias repeatDropdown: repeatDropdown
    property alias endsDropdown: endsDropdown
    property alias endsCountField: endsCountField
    property alias untilDateField: untilDateField
    property alias startDateField: startDateField
    property alias endDateField: endDateField
    property alias startHourDropdown: startHourDropdown
    property alias startMinuteDropdown: startMinuteDropdown
    property alias endHourDropdown: endHourDropdown
    property alias endMinuteDropdown: endMinuteDropdown

        visible: panel.showAddForm
        width: dayContent.width
        height: visible ? implicitHeight : 0
        spacing: Style.space(8)

        TextField {
                    id: newEventTitleField
                                width: dayContent.width
                                placeholderText: "Event title"
                                foreground: panel.contentForeground
                                onTextChanged: panel.newEventSummary = text
                                    Keys.priority: Keys.BeforeItem
                                    Keys.onPressed: function(event) {
                                        var d = panel.keypadDigitFor(event)
                                        if (d) {
                                            insert(cursorPosition, d)
                                            cursorPosition += 1
                                            event.accepted = true
                                        }
                                    }
                            }

                            TextField {
                                id: newEventLocationField
                                width: dayContent.width
                                placeholderText: "Location"
                                foreground: panel.contentForeground
                                onTextChanged: panel.newEventLocation = text
                                    Keys.priority: Keys.BeforeItem
                                    Keys.onPressed: function(event) {
                                        var d = panel.keypadDigitFor(event)
                                        if (d) {
                                            insert(cursorPosition, d)
                                            cursorPosition += 1
                                            event.accepted = true
                                        }
                                    }
                            }

                            TextField {
                                id: newEventDescriptionField
                                width: dayContent.width
                                placeholderText: "Notes"
                                foreground: panel.contentForeground
                                onTextChanged: panel.newEventDescription = text
                                    Keys.priority: Keys.BeforeItem
                                    Keys.onPressed: function(event) {
                                        var d = panel.keypadDigitFor(event)
                                        if (d) {
                                            insert(cursorPosition, d)
                                            cursorPosition += 1
                                            event.accepted = true
                                        }
                                    }
                            }

                            Item {
                                width: dayContent.width
                                height: Style.spacing.controlHeight
                                        FormLabel {
            panel: panel
            text: "Calendar"
        }
                                Dropdown {
                                    id: calendarDropdown
                                    width: Style.space(240)
                                    height: Style.spacing.controlHeight
                                    showLabel: false
                                    value: ""
                                    options: panel.calendars.filter(function(c) { return c.writable }).map(function(c) {
                                        return { value: String(c.id), label: c.display_name }
                                    })
                                    foreground: panel.contentForeground
                                    fontFamily: panel.contentFontFamily
                                    onChanged: panel.newEventCalendarId = parseInt(value, 10)
                                    onPopupOpenChanged: if (!popupOpen) keyCatcher.forceActiveFocus()
                                    anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                            }

                            Toggle {
                                id: allDayToggle
                                width: dayContent.width
                                label: "All day"
                                description: "Date-only event, no start/end time"
                                checked: panel.newEventAllDay
                                foreground: panel.contentForeground
                                accent: panel.accent
                                fontFamily: panel.contentFontFamily
                                onClicked: panel.newEventAllDay = !panel.newEventAllDay
                            }

                            Toggle {
                                id: editScopeToggle
                                width: dayContent.width
                                // Only when editing a recurring event: choose whether the
                                // change applies to just this occurrence (detached override)
                                // or the whole series. One-off events have no scope question.
                                visible: panel.editingUid !== "" && panel.editingIsRecurring
                                label: "This occurrence only"
                                description: "Edit just this occurrence; the rest of the series is unchanged"
                                checked: panel.editThisOccurrence
                                foreground: panel.contentForeground
                                accent: panel.accent
                                fontFamily: panel.contentFontFamily
                                onClicked: panel.editThisOccurrence = !panel.editThisOccurrence
                            }

                            Item {
                                width: dayContent.width
                                height: Style.spacing.controlHeight
                                // Only when creating: update_event() preserves the stored
                                // rule, so editing it here would be a lie.
                                visible: panel.editingUid === ""
                                        FormLabel {
            panel: panel
            text: "Repeat"
        }
                                Dropdown {
                                    id: repeatDropdown
                                    width: Style.space(240)
                                    height: Style.spacing.controlHeight
                                    showLabel: false
                                    value: ""
                                    options: panel.repeatPresets
                                    foreground: panel.contentForeground
                                    fontFamily: panel.contentFontFamily
                                    onChanged: panel.newEventRepeat = value
                                    onPopupOpenChanged: if (!popupOpen) keyCatcher.forceActiveFocus()
                                    anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                            }

                            Item {
                                width: dayContent.width
                                height: Style.spacing.controlHeight
                                visible: panel.editingUid === "" && panel.newEventRepeat !== ""
                                        FormLabel {
            panel: panel
            text: "Ends"
        }
                                Dropdown {
                                    id: endsDropdown
                                    width: Style.space(240)
                                    height: Style.spacing.controlHeight
                                    showLabel: false
                                    value: ""
                                    options: panel.endsOptions
                                    foreground: panel.contentForeground
                                    fontFamily: panel.contentFontFamily
                                    onChanged: panel.newEventEnds = value
                                    // When "On date" is chosen, auto-focus the date field so the
                                    // user can type straight in. Defer until the popup closes and
                                    // the field is laid out visible.
                                    onPopupOpenChanged: {
                                        if (!popupOpen) {
                                            if (panel.newEventEnds === "until")
                                                Qt.callLater(function() { untilDateField.forceActiveFocus() })
                                            else
                                                keyCatcher.forceActiveFocus()
                                        }
                                    }
                                    anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                            }

                            Item {
                                width: dayContent.width
                                height: Style.spacing.controlHeight
                                visible: panel.editingUid === "" && panel.newEventRepeat !== ""
                                         && panel.newEventEnds !== "never"
                                FormLabel {
                                    id: endsValueLabel
                                    panel: panel
                                    text: panel ? (panel.newEventEnds === "count" ? "Times" : "On") : ""
                                }
                                TextField {
                                    id: endsCountField
                                    visible: panel.newEventEnds === "count"
                                    width: Style.space(90)
                                    height: Style.spacing.controlHeight
                                    horizontalAlignment: Text.AlignHCenter
                                    placeholderText: "COUNT"
                                    foreground: panel.contentForeground
                                    onTextChanged: panel.newEventCount = parseInt(text, 10)
                                        Keys.priority: Keys.BeforeItem
                                        Keys.onPressed: function(event) {
                                            var d = panel.keypadDigitFor(event)
                                            if (d) {
                                                insert(cursorPosition, d)
                                                cursorPosition += 1
                                                event.accepted = true
                                            }
                                        }
                                    anchors.left: endsValueLabel.right
                                    anchors.leftMargin: Style.space(34)
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                DateEntry {
                                    id: untilDateField
                                    visible: panel.newEventEnds === "until"
                                    width: Style.space(120)
                                    height: Style.spacing.controlHeight
                                    foreground: panel.contentForeground
                                    onEditingFinished: keyCatcher.forceActiveFocus()
                                    anchors.left: endsValueLabel.right
                                    anchors.leftMargin: Style.space(34)
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                            }

                            Text {
                                width: dayContent.width
                                visible: panel.editingUid !== "" && panel.newEventRepeat !== ""
                                text: "Repeats: " + panel.repeatSummary() + " \u2014 not editable here"
                                textFormat: Text.PlainText
                                wrapMode: Text.Wrap
                                color: Qt.darker(panel.contentForeground, 1.5)
                                font.family: panel.contentFontFamily
                                font.pixelSize: Style.font.bodySmall
                                font.italic: true
                            }

                            Item {
                                width: dayContent.width
                                height: Style.spacing.controlHeight
                                        FormLabel {
        id: startLabel
            panel: panel
            text: "Start"
        }
                                DateEntry {
                                    id: startDateField
                                    width: panel.newEventAllDay
                                        ? parent.width - Style.space(56) - Style.space(44)
                                        : Style.space(110) + (parent.width - Style.space(280)) / 2 - Style.space(30)
                                    height: Style.spacing.controlHeight
                                    foreground: panel.contentForeground
                                    onEditingFinished: {
                                        keyCatcher.forceActiveFocus()
                                    }
                                    anchors.left: startLabel.right
                                    anchors.leftMargin: Style.space(34)
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                Dropdown {
                                    id: startMinuteDropdown
                                    visible: !panel.newEventAllDay
                                    width: Style.space(50)
                                    height: Style.spacing.controlHeight
                                    showLabel: false
                                    value: ""
                                    options: panel.minuteOptions
                                    foreground: panel.contentForeground
                                    fontFamily: panel.contentFontFamily
                                    onChanged: panel.newEventStartMinute = parseInt(value, 10)
                                    onPopupOpenChanged: if (!popupOpen) keyCatcher.forceActiveFocus()
                                    anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                Dropdown {
                                    id: startHourDropdown
                                    visible: !panel.newEventAllDay
                                    width: Style.space(50)
                                    height: Style.spacing.controlHeight
                                    showLabel: false
                                    value: ""
                                    options: panel.hourOptions
                                    foreground: panel.contentForeground
                                    fontFamily: panel.contentFontFamily
                                    onChanged: panel.newEventStartHour = parseInt(value, 10)
                                    onPopupOpenChanged: if (!popupOpen) keyCatcher.forceActiveFocus()
                                    anchors.right: startColon.left
                                    anchors.rightMargin: Style.space(2)
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                Text {
                                    id: startColon
                                    visible: !panel.newEventAllDay
                                    anchors.right: startMinuteDropdown.left
                                    anchors.rightMargin: Style.space(2)
                                    text: ":"
                                    width: Style.space(6)
                                    color: panel.contentForeground
                                    font.family: panel.contentFontFamily
                                    font.pixelSize: Style.font.body
                                    verticalAlignment: Text.AlignVCenter
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                            }

                            Item {
                                width: dayContent.width
                                height: Style.spacing.controlHeight
                                        FormLabel {
        id: endLabel
            panel: panel
            text: "End"
        }
                                DateEntry {
                                    id: endDateField
                                    width: panel.newEventAllDay
                                        ? parent.width - Style.space(56) - Style.space(44)
                                        : Style.space(110) + (parent.width - Style.space(280)) / 2 - Style.space(30)
                                    height: Style.spacing.controlHeight
                                    foreground: panel.contentForeground
                                    onEditingFinished: {
                                        keyCatcher.forceActiveFocus()
                                    }
                                    anchors.left: endLabel.right
                                    anchors.leftMargin: Style.space(34)
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                Dropdown {
                                    id: endMinuteDropdown
                                    visible: !panel.newEventAllDay
                                    width: Style.space(50)
                                    height: Style.spacing.controlHeight
                                    showLabel: false
                                    value: ""
                                    options: panel.minuteOptions
                                    foreground: panel.contentForeground
                                    fontFamily: panel.contentFontFamily
                                    onChanged: panel.newEventEndMinute = parseInt(value, 10)
                                    onPopupOpenChanged: if (!popupOpen) keyCatcher.forceActiveFocus()
                                    anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                Dropdown {
                                    id: endHourDropdown
                                    visible: !panel.newEventAllDay
                                    width: Style.space(50)
                                    height: Style.spacing.controlHeight
                                    showLabel: false
                                    value: ""
                                    options: panel.hourOptions
                                    foreground: panel.contentForeground
                                    fontFamily: panel.contentFontFamily
                                    onChanged: panel.newEventEndHour = parseInt(value, 10)
                                    onPopupOpenChanged: if (!popupOpen) keyCatcher.forceActiveFocus()
                                    anchors.right: endColon.left
                                    anchors.rightMargin: Style.space(2)
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                Text {
                                    id: endColon
                                    visible: !panel.newEventAllDay
                                    anchors.right: endMinuteDropdown.left
                                    anchors.rightMargin: Style.space(2)
                                    text: ":"
                                    width: Style.space(6)
                                    color: panel.contentForeground
                                    font.family: panel.contentFontFamily
                                    font.pixelSize: Style.font.body
                                    verticalAlignment: Text.AlignVCenter
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                            }

                            Row {
                                width: dayContent.width
                                height: Math.max(Style.spacing.controlHeight, Style.font.body + Style.spacing.controlPaddingY * 2) + 2
                                spacing: Style.space(4)

                                Button {
                                    text: "Cancel"
                                    width: (dayContent.width - Style.space(4)) / 2
                                    onClicked: panel.dismissAddForm()
                                }

                                Button {
                                    text: "Save"
                                    width: (dayContent.width - Style.space(4)) / 2
                                    onClicked: panel.submitAddEvent()
                                }
                            }
}
