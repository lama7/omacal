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
    property string newEventDescription: ""
    property bool startDateValid: true
    property bool endDateValid: true
    property bool newEventAllDay: false
    property string newEventRepeat: ""
    // RRULE presets. "Every 2 weeks" anchors on the weekday of the event's own
    // start date, which is what users expect from a repeat dropdown.
    readonly property var repeatPresets: [
        { value: "", label: "Does not repeat" },
        { value: "FREQ=DAILY", label: "Every day" },
        { value: "FREQ=WEEKLY", label: "Every week" },
        { value: "FREQ=WEEKLY;INTERVAL=2", label: "Every 2 weeks" },
        { value: "FREQ=MONTHLY", label: "Every month" },
        { value: "FREQ=YEARLY", label: "Every year" }
    ]
    // Repeat end condition ("Ends" row). "never" leaves the RRULE unbounded,
    // which is what a repeat with no COUNT/UNTIL means.
    property string newEventEnds: "never"
    property int newEventCount: 5
    property string newEventUntilDate: ""
    property bool untilDateValid: true
    readonly property var endsOptions: [
        { value: "never", label: "Never" },
        { value: "count", label: "After N occurrences" },
        { value: "until", label: "On date" }
    ]

    // Edit/delete state
    property string editingUid: ""
    property int editingCalendarId: 0
    property string editingOccurrence: ""   // recurrence_id of the occurrence being edited (recurring only)
    property bool editingIsRecurring: false
    property bool editThisOccurrence: true  // scope toggle: true = this occurrence, false = whole series
    property bool deleteWholeSeries: false  // delete scope: true = whole series, false = this occurrence
    property var pendingDeleteEvent: null
    property bool showDeleteConfirm: false

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

    // First cell of the month grid. It is often a day from the previous month,
    // and the last rendered row likewise spills into the next month.
    function gridStartDate() {
        var first = new Date(viewYear, viewMonth, 1)
        var startWeekday = (first.getDay() - weekStart + 7) % 7
        var cursor = new Date(first)
        cursor.setDate(cursor.getDate() - startWeekday)
        return cursor
    }

    function computeRangeDays() {
        var days = []
        var cursor = gridStartDate()
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
                    dayEvents: eventsForDay(d.getFullYear(), d.getMonth(), d.getDate()),
                    year: d.getFullYear(),
                    month: d.getMonth(),
                    date: d.getDate(),
                    key: key,
                    weekIndex: r
                })
                cursor.setDate(cursor.getDate() + 1)
            }
            days.push(row)
            // Stop once the grid has covered the month AND the cursor has moved
            // into a later month. The old test compared only the month index, so
            // it never fired in December (January's 0 is not > December's 11) and
            // December always rendered a full 6th row of next-year days.
            var spilled = cursor.getFullYear() > viewYear
                || (cursor.getFullYear() === viewYear && cursor.getMonth() > viewMonth)
            if (r >= 4 && spilled) break
        }
        return days
    }

    function computeWeekRows() {
        var rows = []
        // Render every row computeRangeDays() produced. Slicing to 5 dropped the
        // 6th row whole, so any month that needs it lost those days entirely --
        // no cell, no dot, no click target (2026-05, 2026-08, 2027-01, ...).
        for (var i = 0; i < rangeDaysArr.length; i++) {
            rows.push({ idx: i, days: rangeDaysArr[i] })
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
        editingUid = ""
        error = ""
        newEventSummary = ""
        newEventTitleField.text = ""
        newEventLocation = ""
        newEventLocationField.text = ""
        newEventDescription = ""
        newEventDescriptionField.text = ""
        newEventStartHour = 9
        newEventStartMinute = 0
        newEventEndHour = 10
        newEventEndMinute = 0
        newEventAllDay = false
        newEventRepeat = ""
        repeatDropdown.value = ""
        newEventEnds = "never"
        newEventCount = 5
        newEventUntilDate = ""
        endsDropdown.value = "never"
        endsCountField.text = ""
        untilDateField.text = ""
        untilDateValid = true
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
        editingUid = ""
        editingIsRecurring = false
        editingOccurrence = ""
        editThisOccurrence = true
        error = ""
        newEventSummary = ""
        newEventTitleField.text = ""
        newEventLocation = ""
        newEventLocationField.text = ""
        newEventDescription = ""
        newEventDescriptionField.text = ""
        newEventStartDate = ""
        newEventEndDate = ""
        startDateField.text = ""
        endDateField.text = ""
        newEventStartHour = 9
        newEventStartMinute = 0
        newEventEndHour = 10
        newEventEndMinute = 0
        newEventAllDay = false
        newEventRepeat = ""
        repeatDropdown.value = ""
        newEventEnds = "never"
        newEventCount = 5
        newEventUntilDate = ""
        endsDropdown.value = "never"
        endsCountField.text = ""
        untilDateField.text = ""
        untilDateValid = true
    }

    // RRULE for the current form state: preset + end condition.
    // "" = no repeat; null = the end date is unusable.
    function buildRrule() {
        if (!newEventRepeat) return ""
        var rule = newEventRepeat
        if (newEventEnds === "count") {
            rule += ";COUNT=" + Math.min(999, Math.max(1, Math.floor(newEventCount || 1)))
        } else if (newEventEnds === "until") {
            var d = parseDateInput(newEventUntilDate)
            if (!d) return null
            rule += ";UNTIL=" + untilStamp(d)
        }
        return rule
    }

    // UNTIL has to be UTC when DTSTART is timezone-aware; an all-day series uses
    // the plain DATE form. recur.normalise_rrule() accepts either.
    function untilStamp(d) {
        var stamp = d.getFullYear() + pad2(d.getMonth() + 1) + pad2(d.getDate())
        return newEventAllDay ? stamp : (stamp + "T235959Z")
    }

    // Read-only summary of a stored rule, for an event that already exists.
    function repeatSummary() {
        if (!newEventRepeat) return ""
        var preset = repeatPresets.find(function(p) { return p.value === newEventRepeat })
        var label = preset ? preset.label : newEventRepeat
        var c = /COUNT=(\d+)/.exec(newEventRepeat)
        var u = /UNTIL=(\d{8})/.exec(newEventRepeat)
        if (c) label += ", " + c[1] + " occurrences"
        else if (u) label += ", until " + u[1].slice(4, 6) + "/" + u[1].slice(6, 8) + "/" + u[1].slice(0, 4)
        return label
    }

    function submitAddEvent() {
        if (!dayDate || !newEventSummary.trim()) return
        var startDate = parseDateInput(newEventStartDate)
        var endDate = parseDateInput(newEventEndDate)
        if (!startDate || !endDate) {
            error = "Invalid date"
            return
        }
        // Validate the repeat rule before touching the server: "On date" with an
        // unusable date would otherwise silently create an endless series.
        var rrule = buildRrule()
        if (rrule === null) {
            error = "Invalid repeat end date"
            return
        }
        var start, end
        if (newEventAllDay) {
            // All-day: midnight of the chosen dates. The form's End Date is the
            // event's LAST day but iCalendar DTEND is exclusive, so send the
            // following midnight (a one-day event sends start + 1 as well, which
            // the backend also stores). Without this a 3-day all-day event was
            // created one day short.
            start = new Date(startDate.getFullYear(), startDate.getMonth(), startDate.getDate(), 0, 0, 0)
            end = new Date(endDate.getFullYear(), endDate.getMonth(), endDate.getDate() + 1, 0, 0, 0)
            if (end <= start) end = new Date(start.getTime() + 86400000)
        } else {
            start = new Date(startDate.getFullYear(), startDate.getMonth(), startDate.getDate(),
                             newEventStartHour, newEventStartMinute)
            end = new Date(endDate.getFullYear(), endDate.getMonth(), endDate.getDate(),
                           newEventEndHour, newEventEndMinute)
            if (end <= start) end = new Date(end.getTime() + 3600000)
        }
        var calId = newEventCalendarId
        var xhr = new XMLHttpRequest()
        var method, url
        if (editingUid) {
            method = "PUT"
            url = apiBase + "/api/events?uid=" + encodeURIComponent(editingUid)
            // Editing one occurrence of a recurring series: pass the occurrence
            // so the backend writes a detached override instead of moving the
            // whole series. Whole-series edits omit it.
            if (editingIsRecurring && editThisOccurrence && editingOccurrence)
                url += "&occurrence=" + encodeURIComponent(editingOccurrence)
        } else {
            method = "POST"
            url = apiBase + "/api/events"
        }
        xhr.open(method, url, true)
        xhr.setRequestHeader("Content-Type", "application/json")
        // The backend pushes to CalDAV synchronously, so a write gets longer than
        // a read -- but never forever: with no timeout a wedged API left the form
        // open with no feedback at all.
        xhr.timeout = 30000
        xhr.onreadystatechange = function() {
            if (xhr.readyState === XMLHttpRequest.DONE) {
                if (xhr.status === 200) {
                    editingUid = ""
                    dismissAddForm()
                    loadRangeEvents(true)
                } else {
                    error = xhr.status === 0 ? "No response from omacal API" : "Failed to save event (" + xhr.status + ")"
                }
            }
        }
        var payload = {
            summary: newEventSummary,
            start: start.toISOString(),
            end: end.toISOString(),
            all_day: newEventAllDay,
            calendar_id: calId,
            location: newEventLocation,
            description: newEventDescription
        }
        if (rrule) payload.rrule = rrule
        xhr.send(JSON.stringify(payload))
    }

    function editEvent(ev) {
        editingUid = ev.uid
        editingCalendarId = ev.calendar_id
        editingOccurrence = ev.recurrence_id || ev.start || ""
        editingIsRecurring = !!ev.is_recurring
        editThisOccurrence = true
        showAddForm = true
        error = ""
        newEventSummary = ev.summary || ""
        newEventTitleField.text = ev.summary || ""
        newEventLocation = ev.location || ""
        newEventLocationField.text = ev.location || ""
        newEventDescription = ev.description || ""
        newEventDescriptionField.text = ev.description || ""
        // All-day rows are cached at UTC midnight with an exclusive DTEND, so
        // read the DATE part of the ISO string (new Date() reads the previous
        // day west of UTC, and a save then PUTs the shifted dates) and turn the
        // exclusive end back into the last day the form edits.
        var allDay = !!ev.all_day
        var start, end
        if (allDay) {
            start = dateFromIso(ev.start)
            end = dateFromIso(ev.end || ev.start)
            end = new Date(end.getFullYear(), end.getMonth(), end.getDate() - 1)
            if (end < start) end = start
        } else {
            start = new Date(ev.start)
            end = ev.end ? new Date(ev.end) : new Date(start.getTime() + 3600000)
        }
        newEventStartDate = formatDateInput(start)
        newEventEndDate = formatDateInput(end)
        startDateField.text = newEventStartDate
        endDateField.text = newEventEndDate
        newEventStartHour = start.getHours()
        newEventStartMinute = start.getMinutes()
        newEventEndHour = end.getHours()
        newEventEndMinute = end.getMinutes()
        newEventCalendarId = ev.calendar_id
        calendarDropdown.value = String(ev.calendar_id)
        newEventAllDay = !!ev.all_day
        newEventRepeat = ev.rrule || ""
        repeatDropdown.value = newEventRepeat
        // Feed the read-only summary on an existing event (the Repeat/Ends
        // controls stay hidden while editing).
        var cnt = /COUNT=(\d+)/.exec(newEventRepeat)
        var unt = /UNTIL=(\d{8})/.exec(newEventRepeat)
        newEventEnds = cnt ? "count" : (unt ? "until" : "never")
        if (cnt) newEventCount = parseInt(cnt[1], 10)
        if (unt) newEventUntilDate = unt[1].slice(4, 6) + "/" + unt[1].slice(6, 8) + "/" + unt[1].slice(0, 4)
        startHourDropdown.value = String(newEventStartHour)
        startMinuteDropdown.value = String(newEventStartMinute)
        endHourDropdown.value = String(newEventEndHour)
        endMinuteDropdown.value = String(newEventEndMinute)
    }

    function openDeleteConfirm(ev) {
        pendingDeleteEvent = ev
        deleteWholeSeries = false
        showDeleteConfirm = true
    }

    function dismissDeleteConfirm() {
        showDeleteConfirm = false
        pendingDeleteEvent = null
        deleteWholeSeries = false
    }

    function deleteEvent() {
        if (!pendingDeleteEvent) return
        var ev = pendingDeleteEvent
        var url = apiBase + "/api/events?uid=" + encodeURIComponent(ev.uid) + "&calendar_id=" + ev.calendar_id
        // A repeating event is deleted one occurrence at a time: pass the
        // occurrence date the user right-clicked so the backend excludes just
        // that day (EXDATE) instead of removing the series.
        if (ev.is_recurring) {
            if (deleteWholeSeries) url += "&series=true"
            else url += "&occurrence=" + encodeURIComponent(ev.recurrence_id || ev.start)
        }
        var xhr = new XMLHttpRequest()
        xhr.open("DELETE", url, true)
        xhr.timeout = 30000
        xhr.onreadystatechange = function() {
            if (xhr.readyState === XMLHttpRequest.DONE) {
                dismissDeleteConfirm()
                if (xhr.status === 200) {
                    loadRangeEvents(true)
                } else {
                    var msg = ""
                    try { msg = JSON.parse(xhr.responseText).error || "" } catch (e) { msg = "" }
                    error = xhr.status === 0
                        ? "No response from omacal API"
                        : "Delete failed (" + xhr.status + ")" + (msg ? ": " + msg : "")
                }
            }
        }
        xhr.send()
    }

    function deleteConfirmText() {
        var ev = pendingDeleteEvent
        if (!ev) return "Delete this event?"
        if (!ev.is_recurring) return "Delete \"" + ev.summary + "\"?"
        if (deleteWholeSeries) return "Delete the whole series \"" + ev.summary + "\"?"
        return "Delete \"" + ev.summary + "\" on " + Qt.formatDateTime(new Date(ev.start), "MMM d") + "?"
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

    // Whether an event's calendar accepts writes. Shared read-only calendars
    // (a spouse's, e.g.) have no stored credentials, so the API reports
    // writable:false -- the panel must not offer edit/delete on those.
    function calendarWritable(ev) {
        var cal = calendars.find(function(c) { return c.id === ev.calendar_id })
        return cal ? !!cal.writable : false
    }

    function loadCalendars() {
        loadingCalendars = true
        error = ""
        var xhr = new XMLHttpRequest()
        xhr.open("GET", calendarsUrl, true)
        xhr.timeout = 15000
        xhr.onreadystatechange = function() {
            if (xhr.readyState === XMLHttpRequest.DONE) {
                loadingCalendars = false
                if (xhr.status === 200) {
                    var data = null
                    try { data = JSON.parse(xhr.responseText) } catch (e) { data = null }
                    if (Array.isArray(data) && data.length > 0) {
                        calendars = data
                        loadRangeEvents(false)
                    } else if (data === null) {
                        error = "Bad response from omacal API"
                    } else {
                        error = "No calendars found"
                    }
                } else {
                    error = xhr.status === 0 ? "omacal API did not respond" : "Cannot reach omacal API (" + xhr.status + ")"
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
        // Fetch the whole rendered grid, not just the calendar month: the grid
        // starts with days from the previous month and ends in the next one,
        // and those cells need their events to draw their dots.
        var start = gridStartDate()
        var end = new Date(start.getFullYear(), start.getMonth(), start.getDate() + 42)
        var url = eventsUrl + "?start=" + encodeURIComponent(start.toISOString())
            + "&end=" + encodeURIComponent(end.toISOString())
            + "&calendars=" + encodeURIComponent(calIds.join(","))
        var xhr = new XMLHttpRequest()
        xhr.open("GET", url, true)
        xhr.timeout = 15000
        xhr.onreadystatechange = function() {
            if (xhr.readyState === XMLHttpRequest.DONE) {
                loadingEvents = false
                if (xhr.status === 200) {
                    var data = null
                    try { data = JSON.parse(xhr.responseText) } catch (e) { data = null }
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
                    } else if (data === null) {
                        error = "Bad response from omacal API"
                    } else {
                        error = "API error"
                    }
                } else {
                    error = xhr.status === 0 ? "omacal API did not respond" : "API error " + xhr.status
                }
            }
        }
        xhr.send()
    }

    // Colour for an event's month-grid dot, darkened when the cell is
    // de-emphasised so the dots dim along with the day number.
    function eventDotColor(ev, dim) {
        var cal = calendars.find(function(c) { return c.id === ev.calendar_id })
        var base = (cal && cal.color) ? cal.color : "#888888"
        return dim ? Qt.darker(base, 1.9) : base
    }

    // Day object from the DATE part of an ISO string, with no timezone shift.
    function dateFromIso(iso) {
        var m = String(iso || "").match(/^(\d{4})-(\d{2})-(\d{2})/)
        if (!m) return new Date(iso)
        return new Date(parseInt(m[1], 10), parseInt(m[2], 10) - 1, parseInt(m[3], 10))
    }

    function groupEventsByDay(evlist) {
        var map = {}
        for (var i = 0; i < evlist.length; i++) {
            var ev = evlist[i]
            // All-day events are date-valued and land in the cache at UTC
            // midnight. Bucketing them through local time pushed them a day
            // earlier for anyone west of UTC (an Oct 1 all-day event's dot sat
            // on Sep 30), so read their date straight off the ISO string.
            var allDay = Number(ev.all_day) === 1
            var day, lastDay
            if (allDay) {
                day = dateFromIso(ev.start)
                lastDay = day
                if (ev.end) {
                    // DTEND is exclusive (day after the last day)
                    var e = dateFromIso(ev.end)
                    lastDay = new Date(e.getFullYear(), e.getMonth(), e.getDate() - 1)
                }
            } else {
                var start = new Date(ev.start)
                day = new Date(start.getFullYear(), start.getMonth(), start.getDate())
                lastDay = day
                if (ev.end) {
                    var end = new Date(ev.end)
                    lastDay = new Date(end.getFullYear(), end.getMonth(), end.getDate())
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
        // refresh() already kicked off loadCalendars() (which chains
        // loadRangeEvents), and it cleared rangeDaysArr -- so the old
        // "length === 0" guard below was always true and fetched everything twice.
        // Ask the backend to refresh if its cache is stale; the panel already
        // rendered from cache, so this never blocks the open.
        requestSync()
    }

    // Fire-and-forget on-demand refresh. The backend skips the sync entirely
    // when its cache is fresher than 60s, so opening the panel repeatedly costs
    // one small local request. When a sync did run, re-read the events.
    function requestSync() {
        var xhr = new XMLHttpRequest()
        xhr.open("POST", apiBase + "/api/sync?if-stale=60", true)
        xhr.timeout = 30000
        xhr.onreadystatechange = function() {
            if (xhr.readyState !== XMLHttpRequest.DONE) return
            if (xhr.status !== 200) return
            var data = null
            try { data = JSON.parse(xhr.responseText) } catch (e) { data = null }
            // {"status":"fresh","synced":false} means there was nothing to do.
            if (data && data.synced === false) return
            loadRangeEvents(false)
        }
        xhr.send()
    }

    function close() {
        dismissDeleteConfirm()
        editingUid = ""
        root.opened = false
        if (root.bar && typeof root.bar.setCenterHoverRevealSuppressed === "function") root.bar.setCenterHoverRevealSuppressed(false)
        root.controller.hide()
    }

    onOpenedChanged: {
        if (!root.opened) {
            dismissDeleteConfirm()
            // Drop a half-filled add/edit form too: reopening used to show the
            // stale draft the user had abandoned.
            dismissAddForm()
            editingUid = ""
        }
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
            // A focused form field must receive its keys (digits, letters)
            // instead of the catcher swallowing them. Per the component's
            // contract: blocked: <editor>.activeFocus.
            blocked: root.showAddForm && (newEventTitleField.activeFocus
                || newEventLocationField.activeFocus
                || newEventDescriptionField.activeFocus
                || endsCountField.activeFocus
                || startDateField.activeFocus
                || endDateField.activeFocus
                || untilDateField.activeFocus)
            onMoveRequested: function(dx, dy) {
                if (root.showDeleteConfirm) {
                    if (dx !== 0) deleteConfirm.handleKey({ key: dx < 0 ? Qt.Key_Left : Qt.Key_Right })
                    return
                }
                if (root.showAddForm) return
                if (root.viewMode === "day") {
                    if (dx !== 0) root.shiftDay(dx)
                } else {
                    if (dx !== 0) root.shiftMonth(dx)
                    if (dy !== 0) root.shiftMonth(dy * 12)
                }
            }
            // ConfirmDialog.selectedIndex defaults to 0 (Cancel), so Enter/Return
            // cancels; you must click Delete (or arrow to it) to confirm.
            onActivateRequested: function() {
                if (root.showDeleteConfirm) { deleteConfirm.handleKey({ key: Qt.Key_Return }); return }
                if (root.showAddForm) root.submitAddEvent()
                else root.close()
            }
            onCloseRequested: root.showDeleteConfirm ? deleteConfirm.handleKey({ key: Qt.Key_Escape }) : (root.showAddForm ? root.dismissAddForm() : root.close())
            onTabRequested: function(direction) {
                if (root.showDeleteConfirm) { deleteConfirm.handleKey({ key: Qt.Key_Tab }); return }
                root.switchPanel(direction)
            }
            onTextKey: function(t) {
                if (root.showDeleteConfirm) return
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
                                            // Today keeps full strength even when it lands on a
                                            // leading/trailing day (viewing a month other than today's).
                                            property bool isDimmed: isLeadingOrTrailing && !modelData.isToday

                                            Rectangle {
                                                anchors.fill: parent
                                                radius: 6
                                                // Spill days carry no fill: the dimming lives in
                                                // the number colour. A darker(fg, 2.8) box put
                                                // the number (#434444) nearly on top of its own
                                                // background (#484949).
                                                color: modelData.isToday ? Color.accent : "transparent"
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
                                                // Leading/trailing days get a legible dim:
                                                // darker(fg, 3.0) was ~#434444, invisible on a
                                                // dark panel; 1.9 reads clearly as de-emphasised.
                                                color: modelData.isToday ? "#FFFFFF"
                                                    : (isDimmed
                                                        ? Qt.darker(root.contentForeground, 1.9)
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
                                                        color: root.eventDotColor(modelData, isDimmed)
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

                            TextField {
                                id: newEventDescriptionField
                                width: dayContent.width
                                placeholderText: "Notes"
                                foreground: root.contentForeground
                                onTextChanged: root.newEventDescription = text
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

                            Toggle {
                                id: allDayToggle
                                width: dayContent.width
                                label: "All day"
                                description: "Date-only event, no start/end time"
                                checked: root.newEventAllDay
                                foreground: root.contentForeground
                                accent: (root.bar && root.bar.accent) ? root.bar.accent : Color.accent
                                fontFamily: root.contentFontFamily
                                onClicked: root.newEventAllDay = !root.newEventAllDay
                            }

                            Toggle {
                                id: editScopeToggle
                                width: dayContent.width
                                // Only when editing a recurring event: choose whether the
                                // change applies to just this occurrence (detached override)
                                // or the whole series. One-off events have no scope question.
                                visible: root.editingUid !== "" && root.editingIsRecurring
                                label: "This occurrence only"
                                description: "Edit just this occurrence; the rest of the series is unchanged"
                                checked: root.editThisOccurrence
                                foreground: root.contentForeground
                                accent: (root.bar && root.bar.accent) ? root.bar.accent : Color.accent
                                fontFamily: root.contentFontFamily
                                onClicked: root.editThisOccurrence = !root.editThisOccurrence
                            }

                            Item {
                                width: dayContent.width
                                height: Style.spacing.controlHeight
                                // Only when creating: update_event() preserves the stored
                                // rule, so editing it here would be a lie.
                                visible: root.editingUid === ""
                                Text {
                                    text: "Repeat"
                                    width: Style.space(56)
                                    color: Qt.darker(root.contentForeground, 1.5)
                                    font.family: root.contentFontFamily
                                    font.pixelSize: Style.font.bodySmall
                                    verticalAlignment: Text.AlignVCenter
                                    anchors.left: parent.left
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                Dropdown {
                                    id: repeatDropdown
                                    width: Style.space(240)
                                    height: Style.spacing.controlHeight
                                    showLabel: false
                                    value: ""
                                    options: root.repeatPresets
                                    foreground: root.contentForeground
                                    fontFamily: root.contentFontFamily
                                    onChanged: root.newEventRepeat = value
                                    onPopupOpenChanged: if (!popupOpen) keyCatcher.forceActiveFocus()
                                    anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                            }

                            Item {
                                width: dayContent.width
                                height: Style.spacing.controlHeight
                                visible: root.editingUid === "" && root.newEventRepeat !== ""
                                Text {
                                    text: "Ends"
                                    width: Style.space(56)
                                    color: Qt.darker(root.contentForeground, 1.5)
                                    font.family: root.contentFontFamily
                                    font.pixelSize: Style.font.bodySmall
                                    verticalAlignment: Text.AlignVCenter
                                    anchors.left: parent.left
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                Dropdown {
                                    id: endsDropdown
                                    width: Style.space(240)
                                    height: Style.spacing.controlHeight
                                    showLabel: false
                                    value: ""
                                    options: root.endsOptions
                                    foreground: root.contentForeground
                                    fontFamily: root.contentFontFamily
                                    onChanged: root.newEventEnds = value
                                    onPopupOpenChanged: if (!popupOpen) keyCatcher.forceActiveFocus()
                                    anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                            }

                            Item {
                                width: dayContent.width
                                height: Style.spacing.controlHeight
                                visible: root.editingUid === "" && root.newEventRepeat !== ""
                                         && root.newEventEnds !== "never"
                                Text {
                                    id: endsValueLabel
                                    text: root.newEventEnds === "count" ? "Times" : "On"
                                    width: Style.space(56)
                                    color: Qt.darker(root.contentForeground, 1.5)
                                    font.family: root.contentFontFamily
                                    font.pixelSize: Style.font.bodySmall
                                    verticalAlignment: Text.AlignVCenter
                                    anchors.left: parent.left
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                TextField {
                                    id: endsCountField
                                    visible: root.newEventEnds === "count"
                                    width: Style.space(90)
                                    height: Style.spacing.controlHeight
                                    horizontalAlignment: Text.AlignHCenter
                                    placeholderText: "COUNT"
                                    foreground: root.contentForeground
                                    onTextChanged: root.newEventCount = parseInt(text, 10)
                                    anchors.left: endsValueLabel.right
                                    anchors.leftMargin: Style.space(34)
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                TextField {
                                    id: untilDateField
                                    visible: root.newEventEnds === "until"
                                    width: Style.space(120)
                                    height: Style.spacing.controlHeight
                                    horizontalAlignment: Text.AlignHCenter
                                    placeholderText: "MM/dd/yyyy"
                                    inputMask: "00/00/0000"
                                    foreground: root.untilDateValid ? root.contentForeground : Color.urgent
                                    onTextChanged: {
                                        root.newEventUntilDate = text
                                        root.untilDateValid = root.parseDateInput(text) !== null
                                    }
                                    onEditingFinished: keyCatcher.forceActiveFocus()
                                    anchors.left: endsValueLabel.right
                                    anchors.leftMargin: Style.space(34)
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                            }

                            Text {
                                width: dayContent.width
                                visible: root.editingUid !== "" && root.newEventRepeat !== ""
                                text: "Repeats: " + root.repeatSummary() + " \u2014 not editable here"
                                textFormat: Text.PlainText
                                wrapMode: Text.Wrap
                                color: Qt.darker(root.contentForeground, 1.5)
                                font.family: root.contentFontFamily
                                font.pixelSize: Style.font.bodySmall
                                font.italic: true
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
                                    width: root.newEventAllDay
                                        ? parent.width - Style.space(56) - Style.space(44)
                                        : Style.space(110) + (parent.width - Style.space(280)) / 2 - Style.space(30)
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
                                    visible: !root.newEventAllDay
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
                                    visible: !root.newEventAllDay
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
                                    visible: !root.newEventAllDay
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
                                    width: root.newEventAllDay
                                        ? parent.width - Style.space(56) - Style.space(44)
                                        : Style.space(110) + (parent.width - Style.space(280)) / 2 - Style.space(30)
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
                                    visible: !root.newEventAllDay
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
                                    visible: !root.newEventAllDay
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
                                    visible: !root.newEventAllDay
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
                                    height: eventText.implicitHeight + (modelData.description ? eventDescText.implicitHeight + Style.space(2) : 0)

                                    MouseArea {
                                        anchors.fill: parent
                                        acceptedButtons: Qt.LeftButton | Qt.RightButton
                                        cursorShape: root.calendarWritable(modelData) ? Qt.PointingHandCursor : Qt.ArrowCursor
                                        onClicked: {
                                            // Read-only shared calendars (a spouse's, e.g.) reject
                                            // writes server-side, so don't offer edit/delete on them.
                                            if (!root.calendarWritable(modelData)) {
                                                root.error = "Read-only calendar \u2014 can't edit"
                                                return
                                            }
                                            if (mouse.button === Qt.LeftButton) {
                                                root.editEvent(modelData)
                                            } else {
                                                root.openDeleteConfirm(modelData)
                                            }
                                        }
                                    }

                                    Rectangle {
                                        anchors.left: parent.left
                                        anchors.verticalCenter: eventText.verticalCenter
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
                                        anchors.top: parent.top
                                        text: root.eventTimeStr(modelData) + " \u2014 " + modelData.summary
                                        textFormat: Text.PlainText
                                        wrapMode: Text.WordWrap
                                        horizontalAlignment: Text.AlignLeft
                                        font.family: root.contentFontFamily
                                        font.pixelSize: Style.font.bodySmall
                                        color: root.contentForeground
                                    }

                                    Text {
                                        id: eventDescText
                                        visible: modelData.description && modelData.description !== ""
                                        anchors.left: parent.left
                                        anchors.leftMargin: 10
                                        anchors.right: parent.right
                                        anchors.top: eventText.bottom
                                        anchors.topMargin: Style.space(2)
                                        text: modelData.description
                                        textFormat: Text.PlainText
                                        wrapMode: Text.WordWrap
                                        font.family: root.contentFontFamily
                                        font.pixelSize: Style.font.bodySmall
                                        color: Qt.darker(root.contentForeground, 1.5)
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
        // Delete confirmation — local clone of ConfirmDialog with a scope toggle
        // inside the card (the stock component has no content slot). selectedIndex
        // 0 = Cancel, so Enter/Return cancels; you must click Delete (or arrow to
        // it) to confirm. Keys are routed from PanelKeyCatcher.
        DeleteConfirmDialog {
            id: deleteConfirm
            anchors.fill: parent
            z: 10
            opened: root.showDeleteConfirm
            message: root.deleteConfirmText()
            confirmText: "Delete"
            cancelText: "Cancel"
            selectedIndex: 0
            showScope: root.pendingDeleteEvent && root.pendingDeleteEvent.is_recurring
            scopeChecked: root.deleteWholeSeries
            foreground: root.contentForeground
            fontFamily: root.contentFontFamily
            onCanceled: root.dismissDeleteConfirm()
            onConfirmed: root.deleteEvent()
            onScopeToggled: root.deleteWholeSeries = checked
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
