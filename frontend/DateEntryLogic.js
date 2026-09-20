// DateEntryLogic.js — pure MM/DD/YYYY date-entry state machine.
//
// SPIKE. No QML/Qt dependencies: pure functions over (text, phase) so the
// formatting rules can be unit-tested in Node. The QML component (DateEntry.qml)
// is a thin shell that calls these and owns the TextField.
//
// Phases: "month" -> "day" -> "year". A segment is "committed" when its slash
// is present in the text. Backspace deletes the rightmost char and un-commits
// a segment when it removes the slash.
//
// Rules (per Gerry's spec):
//   month: 1st digit 0-9. If >=2 -> pad + slash, go day. If 1 -> wait for 0-2
//          or '/'. If 0 -> wait for 1-9, then slash, go day.
//   day:   1st digit 0-9. If 4-9 -> single-digit day, 0-pad + slash, go year.
//          0-3 waits. Validate against month max (Feb allows 29 while typing).
//   year:  digits only, max 4. '/' discarded. Optionally pre-filled with the
//          current year on day->year transition; the first typed digit then
//          REPLACES the prefilled year (typeover) instead of appending.
//
// State is (text, phase, prefilled). `prefilled` is only meaningful for the
// year phase: true means the year text is a pre-populated default that should
// be typeover-replaced on the first digit, and cleared by backspace.

// Days in month for a given year (Feb = 29 when leap, else 28).
function daysInMonth(month, year) {
    if (month === 1) { // February
        if (year === null) return 29 // year unknown while typing -> allow 29
        var leap = (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0
        return leap ? 29 : 28
    }
    return [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month]
}

// Parse the current text into {month, day, year} (1-indexed month/day, or null).
function parts(text) {
    var m = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec(text)
    if (!m) return { month: null, day: null, year: null }
    return { month: +m[1], day: +m[2], year: +m[3] }
}

// Is the entered date a real calendar date? (strict leap-year check)
function isValidDate(month, day, year) {
    if (month === null || day === null || year === null) return false
    if (month < 1 || month > 12) return false
    if (day < 1 || day > daysInMonth(month - 1, year)) return false
    return true
}

// Parse text -> { date: JS Date|null, valid: bool }
function parse(text) {
    var p = parts(text)
    if (!isValidDate(p.month, p.day, p.year)) return { date: null, valid: false }
    return { date: new Date(p.year, p.month - 1, p.day), valid: true }
}

// Complete the day -> year transition. `prefix` is the month/day without a
// trailing slash ("MM/DD"). If a currentYear is supplied, preload the year.
function goYear(prefix, currentYear) {
    if (typeof currentYear === "number" && currentYear > 0) {
        var y = String(currentYear).padStart(4, "0")
        return { accepted: true, text: prefix + "/" + y, phase: "year", prefilled: true }
    }
    return { accepted: true, text: prefix + "/", phase: "year", prefilled: false }
}

// Handle one typed character. Returns { accepted, text, phase, prefilled }.
// `ch` is a single char: "0"-"9" or "/".
function type(text, phase, ch, currentYear, prefilled) {
    if (phase === "month") return typeMonth(text, ch)
    if (phase === "day") return typeDay(text, ch, currentYear)
    return typeYear(text, ch, prefilled)
}

function typeMonth(text, ch) {
    if (ch === "/") {
        // '1' alone is January -> canonicalize to 01/. A complete valid month
        // (01-12) can re-commit its slash after a backspace removed it (05 -> 05/).
        if (text === "1") return { accepted: true, text: "01/", phase: "day", prefilled: false }
        if (/^(0[1-9]|1[0-2])$/.test(text)) return { accepted: true, text: text + "/", phase: "day", prefilled: false }
        return { accepted: false, text: text, phase: "month" }
    }
    if (ch < "0" || ch > "9") return { accepted: false, text: text, phase: "month" }

    if (text.length === 0) {
        // First digit.
        if (ch === "0") return { accepted: true, text: "0", phase: "month" } // wait for 1-9
        if (ch === "1") return { accepted: true, text: "1", phase: "month" } // wait for 0-2 or '/'
        // 2-9: pad + slash, go day.
        return { accepted: true, text: "0" + ch + "/", phase: "day", prefilled: false }
    }
    if (text.length === 1) {
        var first = text[0]
        if (first === "0") {
            // 0 then 1-9 -> "0X/", go day. 0 then 0 is invalid (month 00).
            if (ch >= "1" && ch <= "9") return { accepted: true, text: "0" + ch + "/", phase: "day", prefilled: false }
            return { accepted: false, text: text, phase: "month" }
        }
        if (first === "1") {
            // 1 then 0-2 -> "1X/", go day. 1 then 3-9 invalid.
            if (ch >= "0" && ch <= "2") return { accepted: true, text: "1" + ch + "/", phase: "day", prefilled: false }
            return { accepted: false, text: text, phase: "month" }
        }
    }
    return { accepted: false, text: text, phase: "month" }
}

function typeDay(text, ch, currentYear) {
    // text here is "MM/" (month committed). Day is the 2 chars after the slash.
    var dayPart = text.length > 2 ? text.slice(3) : ""
    var month = +text.slice(0, 2)
    var maxDay = daysInMonth(month - 1, null) // year unknown -> Feb allows 29

    if (ch === "/") {
        // Single day digit 1-3 commits as a 0-padded day (05/2/ -> 05/02/).
        // Strip the pending digit from text, then 0-pad it in.
        if (/^[1-3]$/.test(dayPart)) return goYear(text.slice(0, -1) + "0" + dayPart, currentYear)
        // A complete valid day re-commits its slash after a backspace (05/21 -> 05/21/).
        if (/^\d{2}$/.test(dayPart) && +dayPart >= 1 && +dayPart <= maxDay) return goYear(text, currentYear)
        return { accepted: false, text: text, phase: "day" }
    }
    if (ch < "0" || ch > "9") return { accepted: false, text: text, phase: "day" }

    if (dayPart.length === 0) {
        // First day digit 0-9. 4-9 is a single-digit day -> 0-pad + slash, go year.
        if (ch >= "4") return goYear(text + "0" + ch, currentYear)
        if (ch >= "0" && ch <= "3") return { accepted: true, text: text + ch, phase: "day", prefilled: false }
        return { accepted: false, text: text, phase: "day" }
    }
    if (dayPart.length === 1) {
        var first = dayPart[0]
        var limit = first === "3" ? 1 : 9 // 3X only 30/31
        if (first === "0" && ch === "0") return { accepted: false, text: text, phase: "day" } // day 00 invalid
        if (ch <= String(limit) && +("" + first + ch) <= maxDay) return goYear(text + ch, currentYear)
        return { accepted: false, text: text, phase: "day" }
    }
    return { accepted: false, text: text, phase: "day" }
}

function typeYear(text, ch, prefilled) {
    if (ch < "0" || ch > "9") return { accepted: false, text: text, phase: "year", prefilled: prefilled } // '/' discarded
    // Typeover: first digit typed replaces the whole prefilled year.
    if (prefilled) {
        var body = text.slice(0, -4) // strip the 4-digit prefilled year ("MM/DD/")
        return { accepted: true, text: body + ch, phase: "year", prefilled: false }
    }
    if (text.length >= 10) return { accepted: false, text: text, phase: "year", prefilled: false } // max 4 year digits
    return { accepted: true, text: text + ch, phase: "year", prefilled: false }
}

// Handle backspace. Returns { text, phase, prefilled }.
// Any backspace (in any phase) clears the prefilled flag, since the pre-filled
// year is then no longer the pristine default.
function backspace(text, phase) {
    if (text.length === 0) return { text: "", phase: "month", prefilled: false }

    var removed = text[text.length - 1]
    var newText = text.slice(0, -1)

    if (removed === "/") {
        // Un-commit a segment: back to the previous phase.
        if (phase === "year") return { text: newText, phase: "day", prefilled: false }   // "MM/DD/" -> "MM/DD"
        if (phase === "day") return { text: newText, phase: "month", prefilled: false }  // "MM/" -> "MM"
    }
    return { text: newText, phase: phase, prefilled: false }
}

// Export for Node testing (module.exports) and QML (the file is imported as a
// JS module, so these become properties of the module object).
if (typeof module !== "undefined" && module.exports) {
    module.exports = { type: type, backspace: backspace, parse: parse, parts: parts, isValidDate: isValidDate, daysInMonth: daysInMonth }
}