import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

Panel {
    id: root
    moduleName: "gerry.clock"
    ipcTarget: "gerry.clock"
    manageIpc: false

    property var anchorItem: null
    property var hostWidget: null
    readonly property var barIdentity: hostWidget || root
    property bool opened: false // NOT readonly — must flip before controller toggles

    readonly property color contentForeground: root.bar ? root.bar.foreground : Color.foreground
    readonly property string contentFontFamily: root.bar ? root.bar.fontFamily : Style.font.family

    property date today: new Date()
    readonly property string todayKey: Model.keyForDate(today)

    readonly property string apiBase: "http://127.0.0.1:9876"
    readonly property string calendarsUrl: apiBase + "/api/calendars"
    readonly property string eventsUrl: apiBase + "/api/events"

    property var calendars: []
    property var events: []
    property var monthEvents: []
    property bool loadingCalendars: false
    property bool loadingEvents: false
    property string error: ""

    property var weekRows: []

    readonly property int weekStart: Model.normalizedWeekStart(setting("weekStartDay", null), Qt.locale().firstDayOfWeek)
    readonly property var labelLocale: Qt.locale("en_US")
    readonly property var weekdays: (function() {
        var start = Model.normalizedWeekStart(weekStart, 1)
        var out = []
        for (var i = 0; i < 7; i++) out.push((start + i) % 7)
        return out
    })()
    function weekdayLabel(weekday) {
        return String(labelLocale.dayName(weekday, Locale.ShortFormat)).toUpperCase()
    }

    property var viewYear: 0
    property var viewMonth: 0
    property var rangeStartJs: null
    property string rangeStartLabelStr: ""
    property var rangeDaysArr: []
    property string viewMode: "month"
    property var dayDate: null
    property var dayEvents: []
    property var pendingDayDate: null

    // Add-event form state
    property bool showAddForm: false
    property string newEventSummary: ""
    property int newEventStartHour: 9
    property int newEventStartMinute: 0
    property int newEventEndHour: 10
    property int newEventEndMinute: 0
    property int newEventCalendarId: 0
    property string newEventStartDate: ""
    property string newEventEndDate: ""
    property string newEventLocation: ""
    property bool startDateValid: true
    property bool endDateValid: true

    readonly property var hourOptions: (function() {
        var a = []; for (var h = 0; h < 24; h++) a.push({value: String(h), label: h < 10 ? " " + h : String(h)}); return a
    })()
    readonly property var minuteOptions: (function() {
        var a = []; for (var m = 0; m < 60; m += 5) a.push({value: String(m), label: m < 10 ? "0" + m : String(m)}); return a
    })()

    function initView() {
        viewYear = today.getFullYear()
        viewMonth = today.getMonth()
    }

    function initRange() {
        rangeStartJs = new Date(viewYear, viewMonth, 1)
        rangeStartLabelStr = Qt.formatDate(rangeStartJs, "MMMM yyyy")
    }

    function getRangeStart() {
        if (!rangeStartJs) initRange()
        return rangeStartJs
    }

    function computeRangeDays() {
        var days = []
        var start = new Date(viewYear, viewMonth, 1)
        var end = new Date(viewYear, viewMonth + 1, 0)
        var startWeekday = (start.getDay() - weekStart + 7) % 7
        var cursor = new Date(start)
        cursor.setDate(cursor.getDate() - startWeekday)
        for (var r = 0; r < 6; r++) {
            var row = []
            for (var c = 0; c < 7; c++) {
                var d = new Date(cursor)
                var key = pad2(d.getFullYear()) + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate())
                var inMonth = d.getMonth() === viewMonth
                row.push({
                    dayLabel: String(d.getDate()),
                    weekdayLabel: String(labelLocale.dayName(d.getDay(), Locale.ShortFormat)).toUpperCase(),
                    isToday: key === todayKey,
                    isCurrentMonth: inMonth,
                    dayEvents: inMonth ? eventsForDay(d.getFullYear(), d.getMonth(), d.getDate()) : [],
                    year: d.getFullYear(),
                    month: d.getMonth(),
                    date: d.getDate(),
                    key: key,
                    weekIndex: r
                })
                cursor.setDate(cursor.getDate() + 1)
            }
            days.push(row)
            if (cursor.getMonth() > viewMonth && r >= 4) break
        }
        return days
    }

    function computeWeekRows() {
        var rows = []
        for (var i = 0; i < 5; i++) {
            if (rangeDaysArr.length > i) {
                rows.push({ idx: i, days: rangeDaysArr[i] })
            }
        }
        weekRows = rows
    }

    property double lastShiftMonthTime: 0
    function shiftMonth(delta) {
        var next = Model.stepMonth(viewYear, viewMonth, delta)
        viewYear = next.year
        viewMonth = next.month
        rangeDaysArr = []
        loadRangeEvents(true)
    }

    function goToToday() {
        viewYear = today.getFullYear()
        viewMonth = today.getMonth()
        viewMode = "month"
        rangeDaysArr = []
        dayDate = null
        dayEvents = []
        pendingDayDate = null
        loadRangeEvents(true)
    }

    function gotoDay(date) {
        viewMode = "day"
        showAddForm = false
        error = ""
        dayDate = new Date(date)
        dayEvents = []
        var y = dayDate.getFullYear()
        var m = dayDate.getMonth()
        if (y !== viewYear || m !== viewMonth) {
            viewYear = y
            viewMonth = m
            pendingDayDate = dayDate
            loadRangeEvents(true)
        } else {
            var key = Model.keyForDate(dayDate)
            dayEvents = monthEvents[key] || []
        }
    }

    function backToMonth() {
        viewMode = "month"
        showAddForm = false
        error = ""
        dayDate = null
        dayEvents = []
        pendingDayDate = null
        initRange()
        rangeDaysArr = []
        loadRangeEvents(true)
    }

    function shiftDay(delta) {
        if (!dayDate) return
        showAddForm = false
        var d = new Date(dayDate)
        d.setDate(d.getDate() + delta)
        if (d.getMonth() !== viewMonth || d.getFullYear() !== viewYear) {
            dayDate = d
            pendingDayDate = d
            dayEvents = []
            shiftMonth(delta > 0 ? 1 : -1)
        } else {
            gotoDay(d)
        }
    }

    function openAddForm() {
        showAddForm = true
        error = ""
        newEventSummary = ""
        newEventTitleField.text = ""
        newEventLocation = ""
        newEventLocationField.text = ""
        newEventStartHour = 9
        newEventStartMinute = 0
        newEventEndHour = 10
        newEventEndMinute = 0
        // Default calendar: prefer "Dad's Calendar", then first writable
        var writable = calendars.filter(function(c) { return c.writable })
        var dadCal = writable.find(function(c) { return c.display_name === "Dad's Calendar" })
        newEventCalendarId = dadCal ? dadCal.id : (writable.length > 0 ? writable[0].id : 0)
        // Sync dropdown initial values — Dropdowns use direct assignment
        // (selectCurrent does root.value = v), so bindings can't be used.
        calendarDropdown.value = String(newEventCalendarId)
        startHourDropdown.value = String(newEventStartHour)
        startMinuteDropdown.value = String(newEventStartMinute)
        endHourDropdown.value = String(newEventEndHour)
        endMinuteDropdown.value = String(newEventEndMinute)
        var dd = new Date(dayDate)
        newEventStartDate = formatDateInput(dd)
        newEventEndDate = formatDateInput(dd)
        startDateField.text = newEventStartDate
        endDateField.text = newEventEndDate
    }

    function dismissAddForm() {
        showAddForm = false
        error = ""
        newEventSummary = ""
        newEventTitleField.text = ""
        newEventLocation = ""
        newEventLocationField.text = ""
        newEventStartDate = ""
        newEventEndDate = ""
        startDateField.text = ""
        endDateField.text = ""
        newEventStartHour = 9
        newEventStartMinute = 0
        newEventEndHour = 10
        newEventEndMinute = 0
    }

    function submitAddEvent() {
        if (!dayDate || !newEventSummary.trim()) return
        var startDate = parseDateInput(newEventStartDate)
        var endDate = parseDateInput(newEventEndDate)
        if (!startDate || !endDate) {
            error = "Invalid date"
            return
        }
        var start = new Date(startDate.getFullYear(), startDate.getMonth(), startDate.getDate(),
                             newEventStartHour, newEventStartMinute)
        var end = new Date(endDate.getFullYear(), endDate.getMonth(), endDate.getDate(),
                           newEventEndHour, newEventEndMinute)
        if (end <= start) end = new Date(end.getTime() + 3600000)
        var calId = newEventCalendarId
        var xhr = new XMLHttpRequest()
        xhr.open("POST", apiBase + "/api/events", true)
        xhr.setRequestHeader("Content-Type", "application/json")
        xhr.onreadystatechange = function() {
            if (xhr.readyState === XMLHttpRequest.DONE) {
                if (xhr.status === 200) {
                    dismissAddForm()
                    loadRangeEvents(true)
                } else {
                    error = "Failed to create event"
                }
            }
        }
        xhr.send(JSON.stringify({
            summary: newEventSummary,
            start: start.toISOString(),
            end: end.toISOString(),
            all_day: false,
            calendar_id: calId,
            location: newEventLocation
        }))
    }

    function eventTimeStr(ev) {
        if (Number(ev.all_day)) return "All day"
        var start = new Date(ev.start)
        if (ev.end) {
            var end = new Date(ev.end)
            var startDay = new Date(start.getFullYear(), start.getMonth(), start.getDate())
            var endDay = new Date(end.getFullYear(), end.getMonth(), end.getDate())
            if (startDay.getTime() !== endDay.getTime()) {
                return Qt.formatDateTime(start, "MMM d, HH:mm") + " \u2013 " + Qt.formatDateTime(end, "MMM d, HH:mm")
            }
        }
        return Qt.formatTime(start, "HH:mm")
    }

    function loadCalendars() {
        loadingCalendars = true
        error = ""
        var xhr = new XMLHttpRequest()
        xhr.open("GET", calendarsUrl, true)
        xhr.onreadystatechange = function() {
            if (xhr.readyState === XMLHttpRequest.DONE) {
                loadingCalendars = false
                if (xhr.status === 200) {
                    var data = JSON.parse(xhr.responseText)
                    if (Array.isArray(data) && data.length > 0) {
                        calendars = data
                        loadRangeEvents(false)
                    } else {
                        error = "No calendars found"
                    }
                } else {
                    error = "Cannot reach omacal API"
                }
            }
        }
        xhr.send()
    }

    function loadRangeEvents(refreshLabels) {
        if (calendars.length === 0) return
        loadingEvents = true
        var calIds = []
        for (var i = 0; i < calendars.length; i++) calIds.push(calendars[i].id)
        var start = new Date(viewYear, viewMonth, 1)
        var end = new Date(viewYear, viewMonth + 1, 0, 23, 59, 59)
        var url = eventsUrl + "?start=" + encodeURIComponent(start.toISOString())
            + "&end=" + encodeURIComponent(end.toISOString())
            + "&calendars=" + encodeURIComponent(calIds.join(","))
        var xhr = new XMLHttpRequest()
        xhr.open("GET", url, true)
        xhr.onreadystatechange = function() {
            if (xhr.readyState === XMLHttpRequest.DONE) {
                loadingEvents = false
                if (xhr.status === 200) {
                    var data = JSON.parse(xhr.responseText)
                    if (Array.isArray(data)) {
                        events = data
                        monthEvents = groupEventsByDay(data)
                        if (viewMode === "month") {
                            rangeDaysArr = computeRangeDays()
                            computeWeekRows()
                        } else if (viewMode === "day" && pendingDayDate) {
                            gotoDay(pendingDayDate)
                            pendingDayDate = null
                        } else if (viewMode === "day" && dayDate) {
                            var dayKey = Model.keyForDate(dayDate)
                            dayEvents = monthEvents[dayKey] || []
                        }
                        if (refreshLabels) initRange()
                    } else {
                        error = "API error"
                    }
                } else {
                    error = "API error " + xhr.status
                }
            }
        }
        xhr.send()
    }

    function groupEventsByDay(evlist) {
        var map = {}
        for (var i = 0; i < evlist.length; i++) {
            var ev = evlist[i]
            var start = new Date(ev.start)
            var end = ev.end ? new Date(ev.end) : null
            var day = new Date(start.getFullYear(), start.getMonth(), start.getDate())
            var lastDay = day
            if (end) {
                lastDay = new Date(end.getFullYear(), end.getMonth(), end.getDate())
                // All-day events: DTEND is exclusive (day after last day)
                if (Number(ev.all_day)) {
                    lastDay = new Date(lastDay.getFullYear(), lastDay.getMonth(), lastDay.getDate() - 1)
                }
            }
            while (day <= lastDay) {
                var key = day.getFullYear() + "-" + pad2(day.getMonth() + 1) + "-" + pad2(day.getDate())
                if (!map[key]) map[key] = []
                map[key].push(ev)
                day.setDate(day.getDate() + 1)
            }
        }
        return map
    }

    function pad2(v) { var n = Number(v); return (n < 10 ? "0" : "") + n }

    function parseDateInput(text) {
        var match = String(text || "").match(/^(\d{2})\/(\d{2})\/(\d{4})$/)
        if (!match) return null
        var m = parseInt(match[1], 10) - 1
        var d = parseInt(match[2], 10)
        var y = parseInt(match[3], 10)
        var date = new Date(y, m, d)
        if (date.getFullYear() !== y || date.getMonth() !== m || date.getDate() !== d) return null
        return date
    }

    function formatDateInput(d) {
        if (!d) return ""
        return pad2(d.getMonth() + 1) + "/" + pad2(d.getDate()) + "/" + d.getFullYear()
    }

    function eventsForDay(year, month, day) {
        var key = year + "-" + pad2(month + 1) + "-" + pad2(day)
        return monthEvents[key] || []
    }

    function refresh() {
        today = new Date()
        initView()
        initRange()
        rangeDaysArr = []
        viewMode = "month"
        dayDate = null
        dayEvents = []
        pendingDayDate = null
        loadCalendars()
    }

    function persistSettings(values) {
        var entry = { id: root.moduleName }
        for (var k in root.settings) if (k !== "id") entry[k] = root.settings[k]
        for (var k in values) entry[k] = values[k]
        root.settings = entry
        if (root.hostWidget && "settings" in root.hostWidget) root.hostWidget.settings = entry
        if (root.bar && root.bar.shell && typeof root.bar.shell.updateEntryInline === "function")
            root.bar.shell.updateEntryInline(root.moduleName, entry)
    }

    function setWeekStart(day) {
        var next = Model.normalizedWeekStart(day, root.weekStart)
        if (next === root.weekStart) return
        root.persistSettings({ weekStartDay: Model.weekStartSettingName(next) })
    }

    function toggleWeekStart() {
        var oldStart = root.weekStart
        root.setWeekStart(Model.toggledWeekStart(root.weekStart))
        if (root.weekStart !== oldStart) {
            rangeDaysArr = []
            loadRangeEvents(true)
        }
    }

    function switchPanel(direction) {
        if (root.bar && typeof root.bar.switchPanelFrom === "function")
            return root.bar.switchPanelFrom(root.barIdentity, direction)
        return false
    }

    function open() {
        refresh()
        root.opened = true
        root.controller.show()
        if (root.bar && typeof root.bar.setCenterHoverRevealSuppressed === "function") root.bar.setCenterHoverRevealSuppressed(true)
        viewMode = "month"
        dayDate = null
        pendingDayDate = null
        if (rangeDaysArr.length === 0) {
            initView()
            initRange()
            loadCalendars()
        }
    }

    function close() {
        root.opened = false
        if (root.bar && typeof root.bar.setCenterHoverRevealSuppressed === "function") root.bar.setCenterHoverRevealSuppressed(false)
        root.controller.hide()
    }

    function toggle() {
        if (root.opened) root.close()
        else root.open()
    }

    SystemClock {
        id: clock
        precision: SystemClock.Minutes
        onDateChanged: {
            var newKey = Model.keyForDate(date)
            if (newKey !== String(root.todayKey)) {
                today = date
                root.goToToday()
            }
        }
    }

    KeyboardPanel {
        id: panel
        anchorItem: root.anchorItem
        owner: root.barIdentity
        bar: root.bar
        open: root.opened
        centerOnBar: true
        focusTarget: keyCatcher
        contentWidth: panel.fittedContentWidth(360)
        contentHeight: panel.fittedContentHeight(contentColumn.implicitHeight)

        PanelKeyCatcher {
            id: keyCatcher
            anchors.fill: parent
            onMoveRequested: function(dx, dy) {
                if (root.showAddForm) return
                if (root.viewMode === "day") {
                    if (dx !== 0) root.shiftDay(dx)
                } else {
                    if (dx !== 0) root.shiftMonth(dx)
                    if (dy !== 0) root.shiftMonth(dy * 12)
                }
            }
            onActivateRequested: root.showAddForm ? root.submitAddEvent() : root.close()
            onCloseRequested: root.showAddForm ? root.dismissAddForm() : root.close()
            onTabRequested: function(direction) { root.switchPanel(direction) }
            onTextKey: function(t) {
                if (root.showAddForm) {
                    if (t === "\b" || t === "\x7F") root.dismissAddForm()
                    return
                }
                if (t === "[" || t === "{") root.shiftMonth(-1)
                else if (t === "]" || t === "}") root.shiftMonth(1)
                else if (t === "t" || t === "T") root.goToToday()
                else if (t === "w" || t === "W") root.toggleWeekStart()
                else if (t === "\b" || t === "\x7F") root.backToMonth()
            }
        }

        Flickable {
            id: scroll
            anchors.fill: parent
            contentWidth: contentColumn.width
            contentHeight: contentColumn.implicitHeight
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            interactive: contentColumn.implicitHeight > height

            Column {
                id: contentColumn
                width: scroll.width
                anchors.top: parent.top
                spacing: Style.space(4)

                Item {
                    width: contentColumn.width
                    height: Style.space(30)

                    Item {
                        id: headerRow
                        width: contentColumn.width
                        anchors.horizontalCenter: parent.horizontalCenter
                        anchors.verticalCenter: parent.verticalCenter
                        height: Style.space(22)

                        PanelActionButton {
                            id: leftAction
                            visible: !root.showAddForm
                            enabled: !root.showAddForm
                            anchors.left: parent.left
                            anchors.verticalCenter: parent.verticalCenter
                            iconText: "\u2190"
                            tooltipText: (root.viewMode === "day" && root.dayDate) ? "Previous Day" : "Previous Month"
                            foreground: root.contentForeground
                            fontFamily: root.contentFontFamily
                            onClicked: (root.viewMode === "day" && root.dayDate) ? root.shiftDay(-1) : root.shiftMonth(-1)
                        }

                        PanelActionButton {
                            id: left2Action
                            enabled: !root.showAddForm
                            visible: root.viewMode === "day" && !root.showAddForm
                            anchors.left: leftAction.right
                            iconText: "\u2191"
                            tooltipText: "Back to Month"
                            foreground: root.contentForeground
                            fontFamily: root.contentFontFamily
                            onClicked: (root.viewMode === "day" && root.dayDate) ? root.backToMonth() : root.shiftMonth(-1)
                        }

                        Text {
                            id: headerLabel
                            textFormat: Text.PlainText
                            anchors.left: root.viewMode === "day" ? left2Action.right : leftAction.right
                            anchors.right: root.viewMode === "day" ? right2Action.left : rightAction.left
                            anchors.leftMargin: Style.space(6)
                            anchors.rightMargin: Style.space(6)
                            anchors.verticalCenter: parent.verticalCenter
                            horizontalAlignment: Text.AlignHCenter
                            font.family: root.contentFontFamily
                            font.pixelSize: Style.font.body
                            font.bold: true
                            font.letterSpacing: 0.5
                            color: Qt.darker(root.contentForeground, 1.3)
                            text: (function() {
                                if (root.viewMode === "day" && root.dayDate) {
                                    return Qt.formatDate(root.dayDate, "dddd, d MMM yyyy")
                                }
                                return Qt.formatDate(new Date(root.viewYear, root.viewMonth, 1), "MMMM yyyy").toUpperCase()
                            })()
                        }

                        PanelActionButton {
                            id: right2Action
                            enabled: !root.showAddForm
                            visible: root.viewMode === "day" && !root.showAddForm
                            anchors.right: rightAction.left
                            anchors.verticalCenter: parent.verticalCenter
                            iconText: "\u002b"
                            tooltipText: "Add Event"
                            foreground: root.contentForeground
                            fontFamily: root.contentFontFamily
                            onClicked: root.viewMode === "day" ? root.openAddForm() : root.shiftMonth(1)
                        }
                         PanelActionButton {
                            id: rightAction
                            visible: !root.showAddForm
                            enabled: !root.showAddForm
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            iconText: "\u2192"
                            tooltipText: (root.viewMode === "day" && root.dayDate) ? "Next Day" : "Next Month"
                            foreground: root.contentForeground
                            fontFamily: root.contentFontFamily
                            onClicked: (root.viewMode === "day" && root.dayDate) ? root.shiftDay(1) : root.shiftMonth(1)
                        }
                    }
                }

                Row {
                    visible: root.viewMode !== "day"
                    width: contentColumn.width
                    spacing: Style.space(2)
                    height: Style.space(18)
                    Repeater {
                        model: root.weekdays
                        Text {
                            text: root.weekdayLabel(modelData)
                            width: Math.floor((contentColumn.width - Style.space(2) * 6) / 7)
                            textFormat: Text.PlainText
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                            color: Qt.darker(root.contentForeground, 1.8)
                            font.family: root.contentFontFamily
                            font.pixelSize: Style.font.caption
                            font.bold: true
                            font.letterSpacing: 0.5
                        }
                    }
                }

                Item {
                    visible: root.viewMode === "month"
                    width: contentColumn.width
                    height: monthContent.implicitHeight

                    Column {
                        id: monthContent
                        width: contentColumn.width
                        spacing: Style.space(2)

                        Rectangle {
                            width: contentColumn.width
                            height: 1
                            color: Qt.darker(root.contentForeground, 2.0)
                            opacity: 0.4
                        }

                        Repeater {
                            id: weekRowRepeater
                            model: root.weekRows

                            Item {
                                width: contentColumn.width
                                height: weekGrid.implicitHeight + 2

                                Grid {
                                    id: weekGrid
                                    columns: 7
                                    width: contentColumn.width
                                    rowSpacing: 2
                                    columnSpacing: 2

                                    property real dayCellWidth: Math.floor((width - columnSpacing * (columns - 1)) / columns)

                                    Repeater {
                                        model: modelData.days

                                        Item {
                                            width: weekGrid.dayCellWidth
                                            height: 50
                                            property bool isLeadingOrTrailing: !modelData.isCurrentMonth

                                            Rectangle {
                                                anchors.fill: parent
                                                radius: 6
                                                color: modelData.isToday
                                                    ? Color.accent
                                                    : (modelData.isLeadingOrTrailing
                                                        ? Qt.darker(root.contentForeground, 2.8)
                                                        : "transparent")
                                            }

                                            Text {
                                                anchors.top: parent.top
                                                anchors.horizontalCenter: parent.horizontalCenter
                                                anchors.topMargin: 3
                                                text: modelData.dayLabel
                                                textFormat: Text.PlainText
                                                horizontalAlignment: Text.AlignHCenter
                                                font.family: root.contentFontFamily
                                                font.pixelSize: Style.font.body
                                                font.bold: modelData.isToday
                                                color: modelData.isToday ? "#FFFFFF"
                                                    : (modelData.isLeadingOrTrailing
                                                        ? Qt.darker(root.contentForeground, 3.0)
                                                        : root.contentForeground)
                                            }

                                            Row {
                                                anchors.bottom: parent.bottom
                                                anchors.horizontalCenter: parent.horizontalCenter
                                                anchors.bottomMargin: 3
                                                spacing: 2
                                                Repeater {
                                                    model: modelData.dayEvents
                                                    Rectangle {
                                                        width: 5
                                                        height: 5
                                                        radius: 2
                                                        color: (function() {
                                                            var cal = root.calendars.find(function(c) { return c.id === modelData.calendar_id })
                                                            return cal ? (cal.color || "#888888") : "#888888"
                                                        })()
                                                    }
                                                }
                                            }
                                            MouseArea {
                                                anchors.fill: parent
                                                cursorShape: Qt.PointingHandCursor
                                                onClicked: root.gotoDay(new Date(modelData.year, modelData.month, modelData.date))
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

                Item {
                    id: dayView
                    visible: root.viewMode === "day" && root.dayDate
                    width: contentColumn.width
                    height: 1 + Style.space(4) + (root.showAddForm ? addEventForm.implicitHeight : dayEventsList.implicitHeight)

                    Column {
                        id: dayContent
                        width: contentColumn.width
                        spacing: Style.space(4)

                        Rectangle {
                            width: contentColumn.width
                            height: 1
                            color: Qt.darker(root.contentForeground, 2.0)
                            opacity: 0.4
                        }

                        Column {
                            id: addEventForm
                            visible: root.showAddForm
                            width: contentColumn.width
                            height: visible ? implicitHeight : 0
                            spacing: Style.space(8)

                            TextField {
                                id: newEventTitleField
                                width: dayContent.width
                                placeholderText: "Event title"
                                foreground: root.contentForeground
                                onTextChanged: root.newEventSummary = text
                            }

                            TextField {
                                id: newEventLocationField
                                width: dayContent.width
                                placeholderText: "Location"
                                foreground: root.contentForeground
                                onTextChanged: root.newEventLocation = text
                            }

                            Item {
                                width: dayContent.width
                                height: Style.spacing.controlHeight
                                Text {
                                    text: "Calendar"
                                    width: Style.space(56)
                                    color: Qt.darker(root.contentForeground, 1.5)
                                    font.family: root.contentFontFamily
                                    font.pixelSize: Style.font.bodySmall
                                    verticalAlignment: Text.AlignVCenter
                                    anchors.left: parent.left
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                Dropdown {
                                    id: calendarDropdown
                                    width: Style.space(240)
                                    height: Style.spacing.controlHeight
                                    showLabel: false
                                    value: ""
                                    options: root.calendars.filter(function(c) { return c.writable }).map(function(c) {
                                        return { value: String(c.id), label: c.display_name }
                                    })
                                    foreground: root.contentForeground
                                    fontFamily: root.contentFontFamily
                                    onChanged: root.newEventCalendarId = parseInt(value, 10)
                                    onPopupOpenChanged: if (!popupOpen) keyCatcher.forceActiveFocus()
                                    anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                            }

                            Item {
                                width: dayContent.width
                                height: Style.spacing.controlHeight
                                Text {
                                    id: startLabel
                                    text: "Start"
                                    width: Style.space(56)
                                    color: Qt.darker(root.contentForeground, 1.5)
                                    font.family: root.contentFontFamily
                                    font.pixelSize: Style.font.bodySmall
                                    verticalAlignment: Text.AlignVCenter
                                    anchors.left: parent.left
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                TextField {
                                    id: startDateField
                                    width: Style.space(110) + (parent.width - Style.space(280)) / 2 - Style.space(30)
                                    height: Style.spacing.controlHeight
                                    horizontalAlignment: Text.AlignHCenter
                                    placeholderText: "MM/dd/yyyy"
                                    inputMask: "00/00/0000"
                                    foreground: startDateValid ? root.contentForeground : Color.urgent
                                    onTextChanged: {
                                        root.newEventStartDate = text
                                        root.startDateValid = root.parseDateInput(text) !== null
                                    }
                                    onEditingFinished: {
                                        keyCatcher.forceActiveFocus()
                                    }
                                    anchors.left: startLabel.right
                                    anchors.leftMargin: Style.space(34)
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                Dropdown {
                                    id: startMinuteDropdown
                                    width: Style.space(50)
                                    height: Style.spacing.controlHeight
                                    showLabel: false
                                    value: ""
                                    options: root.minuteOptions
                                    foreground: root.contentForeground
                                    fontFamily: root.contentFontFamily
                                    onChanged: root.newEventStartMinute = parseInt(value, 10)
                                    onPopupOpenChanged: if (!popupOpen) keyCatcher.forceActiveFocus()
                                    anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                Dropdown {
                                    id: startHourDropdown
                                    width: Style.space(50)
                                    height: Style.spacing.controlHeight
                                    showLabel: false
                                    value: ""
                                    options: root.hourOptions
                                    foreground: root.contentForeground
                                    fontFamily: root.contentFontFamily
                                    onChanged: root.newEventStartHour = parseInt(value, 10)
                                    onPopupOpenChanged: if (!popupOpen) keyCatcher.forceActiveFocus()
                                    anchors.right: startColon.left
                                    anchors.rightMargin: Style.space(2)
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                Text {
                                    id: startColon
                                    anchors.right: startMinuteDropdown.left
                                    anchors.rightMargin: Style.space(2)
                                    text: ":"
                                    width: Style.space(6)
                                    color: root.contentForeground
                                    font.family: root.contentFontFamily
                                    font.pixelSize: Style.font.body
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                            }

                            Item {
                                width: dayContent.width
                                height: Style.spacing.controlHeight
                                Text {
                                    id: endLabel
                                    text: "End"
                                    width: Style.space(56)
                                    color: Qt.darker(root.contentForeground, 1.5)
                                    font.family: root.contentFontFamily
                                    font.pixelSize: Style.font.bodySmall
                                    verticalAlignment: Text.AlignVCenter
                                    anchors.left: parent.left
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                TextField {
                                    id: endDateField
                                    width: Style.space(110) + (parent.width - Style.space(280)) / 2 - Style.space(30)
                                    height: Style.spacing.controlHeight
                                    horizontalAlignment: Text.AlignHCenter
                                    placeholderText: "MM/dd/yyyy"
                                    inputMask: "00/00/0000"
                                    foreground: endDateValid ? root.contentForeground : Color.urgent
                                    onTextChanged: {
                                        root.newEventEndDate = text
                                        root.endDateValid = root.parseDateInput(text) !== null
                                    }
                                    onEditingFinished: {
                                        keyCatcher.forceActiveFocus()
                                    }
                                    anchors.left: endLabel.right
                                    anchors.leftMargin: Style.space(34)
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                Dropdown {
                                    id: endMinuteDropdown
                                    width: Style.space(50)
                                    height: Style.spacing.controlHeight
                                    showLabel: false
                                    value: ""
                                    options: root.minuteOptions
                                    foreground: root.contentForeground
                                    fontFamily: root.contentFontFamily
                                    onChanged: root.newEventEndMinute = parseInt(value, 10)
                                    onPopupOpenChanged: if (!popupOpen) keyCatcher.forceActiveFocus()
                                    anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                Dropdown {
                                    id: endHourDropdown
                                    width: Style.space(50)
                                    height: Style.spacing.controlHeight
                                    showLabel: false
                                    value: ""
                                    options: root.hourOptions
                                    foreground: root.contentForeground
                                    fontFamily: root.contentFontFamily
                                    onChanged: root.newEventEndHour = parseInt(value, 10)
                                    onPopupOpenChanged: if (!popupOpen) keyCatcher.forceActiveFocus()
                                    anchors.right: endColon.left
                                    anchors.rightMargin: Style.space(2)
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                Text {
                                    id: endColon
                                    anchors.right: endMinuteDropdown.left
                                    anchors.rightMargin: Style.space(2)
                                    text: ":"
                                    width: Style.space(6)
                                    color: root.contentForeground
                                    font.family: root.contentFontFamily
                                    font.pixelSize: Style.font.body
                                    horizontalAlignment: Text.AlignHCenter
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
                                    onClicked: root.dismissAddForm()
                                }

                                Button {
                                    text: "Save"
                                    width: (dayContent.width - Style.space(4)) / 2
                                    onClicked: root.submitAddEvent()
                                }
                            }
                        }

                        Column {
                            id: dayEventsList
                            visible: !root.showAddForm
                            width: contentColumn.width
                            height: visible ? implicitHeight : 0
                            spacing: Style.space(2)

                            Repeater {
                                model: root.dayEvents
                                Item {
                                    width: dayEventsList.width
                                    height: eventText.implicitHeight

                                    Rectangle {
                                        anchors.left: parent.left
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: 6
                                        height: 6
                                        radius: 3
                                        color: (function() {
                                            var cal = root.calendars.find(function(c) { return c.id === modelData.calendar_id })
                                            return cal ? (cal.color || "#888888") : "#888888"
                                        })()
                                    }

                                    Text {
                                        id: eventText
                                        anchors.left: parent.left
                                        anchors.leftMargin: 10
                                        anchors.right: parent.right
                                        anchors.verticalCenter: parent.verticalCenter
                                        text: root.eventTimeStr(modelData) + " \u2014 " + modelData.summary
                                        textFormat: Text.PlainText
                                        wrapMode: Text.WordWrap
                                        horizontalAlignment: Text.AlignLeft
                                        font.family: root.contentFontFamily
                                        font.pixelSize: Style.font.bodySmall
                                        color: root.contentForeground
                                    }
                                }
                            }

                            Text {
                                visible: root.dayEvents.length === 0 && !root.loadingEvents
                                width: dayEventsList.width
                                height: implicitHeight
                                text: "No events for this day"
                                textFormat: Text.PlainText
                                font.family: root.contentFontFamily
                                font.pixelSize: Style.font.bodySmall
                                color: Qt.darker(root.contentForeground, 1.5)
                                font.italic: true
                            }
                        }
                    }
                }

                Item {
                    width: contentColumn.width
                    anchors.horizontalCenter: parent.horizontalCenter
                    height: statusText.visible ? statusText.implicitHeight + Style.space(4) : 0
                    Text {
                        id: statusText
                        textFormat: Text.PlainText
                        width: contentColumn.width
                        anchors.horizontalCenter: parent.horizontalCenter
                        text: (function() {
                            if (root.loadingCalendars || root.loadingEvents) return "Loading\u2026"
                            if (root.error) return root.error
                            if (root.calendars.length === 0) return "No calendars configured"
                            return ""
                        })()
                        color: root.error ? Qt.rgba(1, 0.3, 0.3, 1) : Qt.darker(root.contentForeground, 1.5)
                        font.family: root.contentFontFamily
                        font.pixelSize: Style.font.bodySmall
                        font.italic: true
                        visible: text !== ""
                    }
                }
            }
        }
    }

    IpcHandler {
        target: "gerry.clock"
        function refresh(): void { root.refresh() }
        function open(): void { root.open() }
        function close(): void { root.close() }
        function show(): void { root.open() }
        function hide(): void { root.close() }
        function toggle(): void { root.toggle() }
        function toggleWeekStart(): void { root.toggleWeekStart() }
    }
}
