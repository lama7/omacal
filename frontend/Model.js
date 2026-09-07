// Pure date and format math for the omacal clock panel.
// Mirrors the clock widget's Model.js — same functions, same contract.

var MS_PER_DAY = 86400000
var WEEKDAY_NAMES = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]

function pad2(v) {
    var n = Number(v)
    return (n < 10 ? "0" : "") + n
}

function keyForDate(date) {
    if (date === null || date === undefined) return ""
    var y = date.getFullYear()
    var m = date.getMonth() + 1
    var d = date.getDate()
    return y + "-" + pad2(m) + "-" + pad2(d)
}

function normalizedWeekStart(raw, fallback) {
    if (raw === "monday" || raw === "Monday" || raw === 1) return 1
    if (raw === "sunday" || raw === "Sunday" || raw === 0) return 0
    if (raw === null || raw === undefined) return fallback % 7
    return Number(raw) % 7 || 0
}

function weekStartSettingName(day) {
    if (day === 1) return "monday"
    if (day === 0) return "sunday"
    return false
}

function toggledWeekStart(current) {
    if (current === 1) return 0
    return 1
}

function weekdayOrder(ws) {
    var out = []
    for (var i = 0; i < 7; i++) out.push((ws + i) % 7)
    return out
}

function isoWeek(year, month, day) {
    var d = new Date(year, month, day)
    var dayNum = (d.getDay() + 6) % 7
    var jan4 = new Date(year, 0, 4)
    var jan4Day = (jan4.getDay() + 6) % 7
    var ordinal = Math.floor((d - jan4) / MS_PER_DAY) + 1
    return Math.ceil((ordinal - jan4Day + 10) / 7)
}

function stepMonth(year, month, delta) {
    var next = new Date(year, month + 1, 1)
    next.setMonth(next.getMonth() + delta)
    return { year: next.getFullYear(), month: next.getMonth() }
}

// ---- Clock label formats (used by BarWidget for right-click cycling) ----
var CLOCK_FORMATS = [
    "dddd HH:mm",
    "dddd h:mm AP",
    "HH:mm",
    "h:mm AP",
    "ddd d MMM HH:mm",
    "ddd d MMM h:mm AP",
    "d MMMM 'W'ww yyyy",
    "yyyy-MM-dd HH:mm"
]

var VERTICAL_CLOCK_FORMATS = [
    "HH\n—\nmm",
    "h\n—\nmm\nAP",
    "dd\nMMM\n'W'ww\n''yy",
    "HH\nmm"
]

function clockFormats(vertical) {
    return vertical ? VERTICAL_CLOCK_FORMATS.slice() : CLOCK_FORMATS.slice()
}

function clockFormatRing(configured, configuredAlt, presets) {
    var ring = []
    var candidates = (presets || []).concat([configuredAlt, configured])
    for (var i = 0; i < candidates.length; i++) {
        var format = String(candidates[i] === undefined || candidates[i] === null ? "" : candidates[i])
        if (format === "" || ring.indexOf(format) !== -1) continue
        ring.push(format)
    }
    return ring.length > 0 ? ring : ["HH:mm"]
}

function nextClockFormat(ring, current) {
    if (!ring || ring.length === 0) return ""
    var index = ring.indexOf(String(current === undefined || current === null ? "" : current))
    return ring[(index + 1) % ring.length]
}

function isoWeekLiteral(year, month, day) {
    return pad2(isoWeek(year, month, day))
}

if (typeof module !== "undefined") {
    module.exports = {
        dateKey: keyForDate,
        keyForDate: keyForDate,
        normalizedWeekStart: normalizedWeekStart,
        weekStartSettingName: weekStartSettingName,
        toggledWeekStart: toggledWeekStart,
        weekdayOrder: weekdayOrder,
        isoWeek: isoWeek,
        stepMonth: stepMonth,
        clockFormats: clockFormats,
        clockFormatRing: clockFormatRing,
        nextClockFormat: nextClockFormat,
        isoWeekLiteral: isoWeekLiteral
    }
}
