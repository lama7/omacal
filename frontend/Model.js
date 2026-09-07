// Pure date and format math for the omacal clock panel.
// Mirrors the clock widget's Model.js — same functions, same contract.

var MS_PER_DAY = 86400000
var WEEKDAY_NAMES = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]

function coerceWeekStart(value) {
    if (value === undefined || value === null) return null
    if (typeof value === "number")
        return isFinite(value) ? ((Math.round(value) % 7) + 7) % 7 : null

    var text = String(value).replace(/^\s+|\s+$/g, "").toLowerCase()
    if (text === "") return null

    for (var i = 0; i < WEEKDAY_NAMES.length; i++)
        if (WEEKDAY_NAMES[i] === text || WEEKDAY_NAMES[i].substr(0, 3) === text) return i

    var parsed = parseInt(text, 10)
    return isFinite(parsed) ? ((parsed % 7) + 7) % 7 : null
}

function normalizedWeekStart(value, fallback) {
    var configured = coerceWeekStart(value)
    if (configured !== null) return configured
    var fallbackStart = coerceWeekStart(fallback)
    return fallbackStart === null ? 1 : fallbackStart
}

function weekStartSettingName(index) {
    return WEEKDAY_NAMES[normalizedWeekStart(index, 1)]
}

function toggledWeekStart(index) {
    return normalizedWeekStart(index, 1) === 1 ? 0 : 1
}

function weekdayOrder(weekStart) {
    var start = normalizedWeekStart(weekStart, 1)
    var out = []
    for (var i = 0; i < 7; i++) out.push((start + i) % 7)
    return out
}

function dateKey(year, month, day) {
    return year + "-" + pad2(Number(month) + 1) + "-" + pad2(day)
}

function keyForDate(date) {
    return dateKey(date.getFullYear(), date.getMonth(), date.getDate())
}

function pad2(value) {
    var n = Number(value)
    return (n < 10 ? "0" : "") + n
}

// ISO-8601 week number
function isoWeek(year, month, day) {
    var date = new Date(Date.UTC(year, month, day))
    var weekday = date.getUTCDay() || 7
    date.setUTCDate(date.getUTCDate() + 4 - weekday)
    var yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1))
    return Math.ceil(((date.getTime() - yearStart.getTime()) / MS_PER_DAY + 1) / 7)
}

function stepMonth(year, month, delta) {
    var target = new Date(year, Number(month) + Number(delta), 1)
    return { year: target.getFullYear(), month: target.getMonth() }
}

// ---- Clock label formats (used by BarWidget for right-click cycling)
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
