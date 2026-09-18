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

// Weekday-name/number coercion. Accepts "monday"/"Mon"/"MONDAY"/1/"1" and
// returns null for anything that carries no weekday. The previous version only
// matched the exact words "monday"/"sunday" case-sensitively, so "Mon",
// "MONDAY", "wednesday" and "" all silently fell through to Sunday.
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

function normalizedWeekStart(raw, fallback) {
    var configured = coerceWeekStart(raw)
    if (configured !== null) return configured
    var fallbackStart = coerceWeekStart(fallback)
    return fallbackStart === null ? 1 : fallbackStart
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

// ISO 8601 week number, computed in UTC. The previous local-time day-of-year
// version was wrong on ~70% of dates (2026-11-09 rendered W45 for the true W46;
// early-January dates fell a week behind) -- reachable through the bar tooltip's
// right-click format ring ('W'ww). This is Omarchy's own clock/Model.js
// implementation, which is unit-tested upstream.
function isoWeek(year, month, day) {
    var date = new Date(Date.UTC(year, month, day))
    var weekday = date.getUTCDay() || 7
    date.setUTCDate(date.getUTCDate() + 4 - weekday)
    var yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1))
    return Math.ceil(((date.getTime() - yearStart.getTime()) / MS_PER_DAY + 1) / 7)
}

function stepMonth(year, month, delta) {
    var next = new Date(year, month, 1)
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
        coerceWeekStart: coerceWeekStart,
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
