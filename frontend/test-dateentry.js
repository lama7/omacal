// test-dateentry.js — Node unit tests for DateEntryLogic.js
// Run: node test-dateentry.js
const L = require("./DateEntryLogic.js")

let pass = 0, fail = 0
function check(name, got, want) {
    const g = JSON.stringify(got), w = JSON.stringify(want)
    if (g === w) { pass++; console.log("  ok  " + name) }
    else { fail++; console.log("FAIL  " + name + "\n      got  " + g + "\n      want " + w) }
}

// Simulate typing a sequence of chars through the state machine.
function typeSeq(seq, currentYear) {
    let text = "", phase = "month", prefilled = false
    for (const ch of seq) {
        if (ch === "\b") { const r = L.backspace(text, phase); text = r.text; phase = r.phase; prefilled = r.prefilled }
        else { const r = L.type(text, phase, ch, currentYear, prefilled); if (r.accepted) { text = r.text; phase = r.phase; prefilled = r.prefilled } }
    }
    return { text, phase, ...L.parse(text) }
}

console.log("== Month entry ==")
check("5 -> 05/", typeSeq("5").text, "05/")
check("1 waits (1)", typeSeq("1").text, "1")
check("1 then 2 -> 12/", typeSeq("12").text, "12/")
check("1 then / -> 01/ (January)", typeSeq("1/").text, "01/")
check("1 then 5 discarded (1)", typeSeq("15").text, "1")
check("0 waits (0)", typeSeq("0").text, "0")
check("0 then 7 -> 07/", typeSeq("07").text, "07/")
check("0 then 0 discarded (0)", typeSeq("00").text, "0")
check("0 then / discarded (0)", typeSeq("0/").text, "0")
check("9 -> 09/", typeSeq("9").text, "09/")

console.log("== Day entry ==")
check("05/ then 2 -> 05/2", typeSeq("52").text, "05/2")
check("05/ then 21 -> 05/21/", typeSeq("521").text, "05/21/")
check("05/ then 3 -> 05/3", typeSeq("53").text, "05/3")
check("05/ then 32 discarded (May max 31)", typeSeq("532").text, "05/3")
check("05/ then 39 discarded (3 then 9)", typeSeq("539").text, "05/3")
check("02/ then 30 discarded (Feb max 29)", typeSeq("230").text, "02/3")
check("02/ then 29 ok (Feb allows 29 while typing)", typeSeq("229").text, "02/29/")
check("04/ then 31 discarded (Apr max 30)", typeSeq("431").text, "04/3")
check("04/ then 30 ok", typeSeq("430").text, "04/30/")
check("day 1st digit 7 -> 05/07/ (0-pad, go year)", typeSeq("57").text, "05/07/")
check("day 1st digit 4 -> 05/04/", typeSeq("54").text, "05/04/")
check("day 1st digit 9 -> 09/09/ (via 9 month/9 day)", typeSeq("99").text, "09/09/")

console.log("== Year entry ==")
check("05/21/ then 2 -> 05/21/2", typeSeq("5212").text, "05/21/2")
check("full 05/21/2026", typeSeq("5212026").text, "05/21/2026")
check("year '/' discarded", typeSeq("5212/").text, "05/21/2")
check("year max 4 digits (5th discarded)", typeSeq("52120265").text, "05/21/2026")

console.log("== Backspace ==")
check("backspace year digit", typeSeq("5212026\b").text, "05/21/202")
check("backspace to day (removes year slash)", typeSeq("5212026\b\b\b\b").text, "05/21/")
check("backspace day digit (2x)", typeSeq("521\b\b").text, "05/2")
check("backspace to month (removes day slash)", typeSeq("521\b\b\b").text, "05/")
check("backspace month digit (2x)", typeSeq("52\b\b").text, "05")
check("backspace to empty (4x)", typeSeq("52\b\b\b\b").text, "")
check("backspace empty no-op", typeSeq("\b").text, "")

console.log("== Slash re-add & canonicalization ==")
check("re-add month slash (05 -> 05/)", typeSeq("521\b\b\b\b/").text, "05/")
check("re-add day slash (05/21 -> 05/21/)", typeSeq("521\b/").text, "05/21/")
check("'1' month + / canonicalizes to 01/", typeSeq("1/").text, "01/")
check("single day + / canonicalizes (05/2/ -> 05/02/)", typeSeq("52/").text, "05/02/")
check("single day '0' + / discarded (day 0 invalid)", typeSeq("50/").text, "05/0")

console.log("== Year prefill & typeover (currentYear=2026) ==")
check("day completion prefills 2026 (521)", typeSeq("521", 2026).text, "05/21/2026")
check("prefilled date valid immediately", typeSeq("521", 2026).valid, true)
check("typeover: first digit replaces year (5219 -> 05/21/9)", typeSeq("5219", 2026).text, "05/21/9")
check("typeover to a full new year (5211977)", typeSeq("5211977", 2026).text, "05/21/1977")
check("typeover full new year valid", typeSeq("5211977", 2026).valid, true)
check("backspace after prefill clears year, appends normally (521\\b5)", typeSeq("521\b5", 2026).text, "05/21/2025")
check("no prefill when currentYear absent (521)", typeSeq("521").text, "05/21/")

console.log("== Validity ==")
check("05/21/2026 valid", typeSeq("5212026").valid, true)
check("02/29/2024 valid (leap)", typeSeq("2292024").valid, true)
check("02/29/2023 invalid (not leap)", typeSeq("2292023").valid, false)
check("02/30/2026 invalid", typeSeq("2302026").valid, false)
check("13/01/2026 invalid (month 13)", typeSeq("13012026").valid, false)
check("incomplete invalid", typeSeq("521").valid, false)
check("value is Date", typeSeq("5212026").date instanceof Date, true)

console.log("\n" + pass + " passed, " + fail + " failed")
process.exit(fail ? 1 : 0)
