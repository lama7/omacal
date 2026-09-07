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
    readonly property bool opened: root.controller && root.controller.open

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
    property var weekStartDate: null
    property var weekDays: []
    property var weekEvents: {}
    property var weekViewData: []

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
        weekStartDate = null
        weekDays = []
        loadRangeEvents(true)
    }

    function gotoWeek(year, month, weekIndex) {
        viewMode = "week"
        weekStartDate = new Date(year, month, 1)
        weekStartDate.setDate(weekStartDate.getDate() + weekIndex * 7)
        weekDays = computeWeekDays(weekStartDate)
        weekEvents = groupEventsByDay(events)
        weekViewData = computeWeekViewData()
        loadRangeEvents(true)
    }

    function backToMonth() {
        viewMode = "month"
        weekStartDate = null
        weekDays = []
        weekViewData = []
        initRange()
        rangeDaysArr = []
        loadRangeEvents(true)
    }

    function computeWeekDays(startDate) {
        var days = []
        for (var i = 0; i < 7; i++) {
            var d = new Date(startDate)
            d.setDate(d.getDate() + i)
            var key = d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate())
            days.push({
                dayLabel: String(d.getDate()),
                weekdayLabel: String(labelLocale.dayName(d.getDay(), Locale.ShortFormat)).toUpperCase(),
                isToday: key === todayKey,
                year: d.getFullYear(),
                month: d.getMonth(),
                date: d.getDate(),
                key: key
            })
        }
        return days
    }

    function computeWeekViewData() {
        var result = []
        for (var i = 0; i < weekDays.length; i++) {
            var day = weekDays[i]
            result.push({
                day: day,
                events: weekDayEventList(day)
            })
        }
        return result
    }

    function weekDayEventList(day) {
        return weekEvents[day.key] || []
    }

    function eventTimeStr(ev) {
        if (Number(ev.all_day)) return "All day"
        var start = new Date(ev.start)
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
                        } else if (viewMode === "week" && weekStartDate) {
                            weekDays = computeWeekDays(weekStartDate)
                            weekEvents = groupEventsByDay(events)
                            weekViewData = computeWeekViewData()
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
            var d = new Date(ev.start)
            var key = d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate())
            if (!map[key]) map[key] = []
            map[key].push(ev)
        }
        return map
    }

    function pad2(v) { var n = Number(v); return (n < 10 ? "0" : "") + n }

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
        weekStartDate = null
        weekDays = []
        weekViewData = []
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
        root.setWeekStart(Model.toggledWeekStart(root.weekStart))
    }

    function switchPanel(direction) {
        if (root.bar && typeof root.bar.switchPanelFrom === "function")
            return root.bar.switchPanelFrom(root.barIdentity, direction)
        return false
    }

    function open() {
        root.controller.show()
        if (root.bar && "centerHoverRevealSuppressed" in root.bar) root.bar.centerHoverRevealSuppressed = true
        // Always reset to month view when popup opens
        viewMode = "month"
        weekStartDate = null
        weekDays = []
        weekViewData = []
        if (rangeDaysArr.length === 0) {
            initView()
            initRange()
            loadCalendars()
        }
    }

    function close() {
        if (root.bar && "centerHoverRevealSuppressed" in root.bar) root.bar.centerHoverRevealSuppressed = false
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
        contentHeight: contentColumn.implicitHeight

        PanelKeyCatcher {
            id: keyCatcher
            anchors.fill: parent
            onMoveRequested: function(dx, dy) {
                if (dy !== 0) root.shiftMonth(dy * 12)
            }
            onActivateRequested: root.goToToday()
            onCloseRequested: root.close()
            onTabRequested: function(direction) { root.switchPanel(direction) }
            onTextKey: function(t) {
                if (t === "[") root.shiftMonth(-1)
                else if (t === "]") root.shiftMonth(1)
                else if (t === "t" || t === "T") root.goToToday()
                else if (t === "w" || t === "W") root.toggleWeekStart()
                else if (t === "Escape") root.backToMonth()
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
                width: 360
                anchors.centerIn: parent
                spacing: Style.space(4)

                Item {
                    width: 360
                    height: headerRow.implicitHeight + Style.space(8)

                    Item {
                        id: headerRow
                        width: 360
                        anchors.horizontalCenter: parent.horizontalCenter
                        anchors.verticalCenter: parent.verticalCenter
                        height: 40

                        Text {
                            id: headerLabel
                            textFormat: Text.PlainText
                            anchors.left: parent.left
                            anchors.right: rightAction.left
                            anchors.leftMargin: Style.space(8)
                            anchors.rightMargin: Style.space(8)
                            anchors.verticalCenter: parent.verticalCenter
                            font.family: root.contentFontFamily
                            font.pixelSize: Style.font.body
                            font.bold: true
                            font.letterSpacing: 0.5
                            color: Qt.darker(root.contentForeground, 1.3)
                            text: (function() {
                                if (Array.isArray(root.weekDays) && root.weekDays.length > 0) {
                                    var end = new Date(root.weekStartDate)
                                    end.setDate(end.getDate() + 6)
                                    return Qt.formatDate(root.weekStartDate, "dd MMM") + " \u2013 " + Qt.formatDate(end, "dd MMM yyyy")
                                }
                                return Qt.formatDate(new Date(root.viewYear, root.viewMonth, 1), "MMMM yyyy").toUpperCase()
                            })()
                        }

                        PanelActionButton {
                            id: rightAction
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            width: 40
                            iconText: (Array.isArray(root.weekDays) && root.weekDays.length > 0) ? "\uE0CE" : "\uE0C0"
                            tooltipText: (Array.isArray(root.weekDays) && root.weekDays.length > 0) ? "Back to month" : "Next month"
                            foreground: root.contentForeground
                            fontFamily: root.contentFontFamily
                            onClicked: (Array.isArray(root.weekDays) && root.weekDays.length > 0) ? root.backToMonth() : root.shiftMonth(1)
                        }
                    }
                }

                Row {
                    width: 360
                    spacing: Style.space(2)
                    height: Style.space(18)
                    Repeater {
                        model: root.weekdays
                        Text {
                            text: root.weekdayLabel(modelData)
                            width: 46
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
                    width: 360
                    height: monthContent.implicitHeight

                    Column {
                        id: monthContent
                        width: 360
                        spacing: Style.space(2)

                        Repeater {
                            id: weekRowRepeater
                            model: root.weekRows

                            Item {
                                width: 360
                                height: weekGrid.implicitHeight + 2

                                Grid {
                                    id: weekGrid
                                    columns: 7
                                    width: 360
                                    rowSpacing: 2
                                    columnSpacing: 2

                                    Repeater {
                                        model: modelData.days

                                        Item {
                                            width: 46
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
                                        }
                                    }
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: {
                                        var days = modelData.days
                                        if (days && days.length > 0) {
                                            var first = days[0]
                                            root.gotoWeek(first.year, first.month, modelData.idx)
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

                Item {
                    id: weekView
                    visible: Array.isArray(root.weekViewData) && root.weekViewData.length > 0
                    width: 360
                    height: weekContent.implicitHeight

                    Column {
                        id: weekContent
                        width: 360
                        spacing: Style.space(2)

                        Row {
                            width: 360
                            spacing: Style.space(2)
                            Repeater {
                                id: weekDayRepeater
                                model: root.weekViewData

                                Column {
                                    width: 47
                                    spacing: 2

                                    Rectangle {
                                        width: 44
                                        height: 56
                                        radius: 6
                                        color: modelData.day.isToday ? Color.accent : Qt.darker(root.contentForeground, 2.8)
                                    }

                                    Text {
                                        text: modelData.day.dayLabel
                                        textFormat: Text.PlainText
                                        horizontalAlignment: Text.AlignHCenter
                                        width: 44
                                        color: "#FFFFFF"
                                        font.family: root.contentFontFamily
                                        font.pixelSize: Style.font.body
                                        font.bold: true
                                        anchors.top: parent.top
                                        anchors.topMargin: 6
                                    }

                                    Item {
                                        width: 44
                                        height: eventsRepeater.implicitHeight
                                        Repeater {
                                            id: eventsRepeater
                                            model: modelData.events
                                            Text {
                                                text: root.eventTimeStr(modelData) + " \u2014 " + modelData.summary
                                                textFormat: Text.PlainText
                                                width: childrenRect.width
                                                horizontalAlignment: Text.AlignLeft
                                                font.family: root.contentFontFamily
                                                font.pixelSize: Style.font.bodySmall
                                                wrapMode: Text.Wrap
                                                color: root.contentForeground
                                                elide: Text.ElideRight
                                                maximumLineCount: 3
                                                leftPadding: 3
                                                rightPadding: 3
                                                topPadding: 1
                                                bottomPadding: 1
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

                Item {
                    width: 360
                    anchors.horizontalCenter: parent.horizontalCenter
                    height: statusText.implicitHeight + Style.space(4)
                    Text {
                        id: statusText
                        textFormat: Text.PlainText
                        width: 360
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
