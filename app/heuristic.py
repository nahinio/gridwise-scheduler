"""Deterministic note reader. Pure.

Two jobs, neither of which replaces the language model:

1. Degraded mode - the last hop of the provider chain when every LLM call has failed,
   so the service still answers with a guarded interpretation instead of a 5xx.
2. Cross-check - its confident readings of explicit clock ranges and numbers are compared
   with the LLM's answer; a disagreement triggers one re-ask (the LLM's answer stands).

It understands explicit wording only. Anything ambiguous becomes `no_op` / "not confident".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import pairwise

from app.schemas import (
    Directive,
    MaxGrid,
    MinReserve,
    NoCharge,
    NoDischarge,
    NoOp,
    SolarReduction,
)

_WORD_HOURS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}  # fmt: skip
_BANGLA_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
_UNITS = r"%|percent|per\s?cent|kwh|kw\b|kilowatt|bdt|taka|tk\b"

_TOKEN = re.compile(
    rf"""(?<![\w.:])(?:
        (?P<noon>noon|midday)
      | (?P<midnight>midnight)
      | (?P<hour>\d{{1,2}})(?::(?P<minute>\d{{2}}))?\s*(?P<mer>am|pm)?(?!\s*(?:{_UNITS}))
      | (?P<word>{"|".join(_WORD_HOURS)})(?!-|\s+(?:fifth|third|quarter|half|tenth))
        (?:\s*o'?clock)?\s*(?P<wmer>am|pm)?
    )(?![\w%:])""",
    re.VERBOSE,
)
_SEPARATOR = re.compile(r"^\s*(?:to|until|till|til|through|thru|up\s?to|and|-)\s*$")
_EVENING = re.compile(r"\b(afternoon|evening|tonight|night)\b")
_MORNING = re.compile(r"\bmorning\b")
_ALL_DAY = re.compile(r"\b(all day|entire day|whole day|throughout the day|the full day)\b")
_NOT_TODAY = re.compile(
    r"\b(tomorrow|yesterday|upcoming|last (?:week|month|year|night|semester)|"
    r"next (?:week|month|year|semester|term|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday))\b"
)

_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent|per\s?cent)")
_ENERGY = re.compile(r"(\d+(?:\.\d+)?)\s*(?:kwh|kw\b|kilowatt)")
_FRACTIONS: tuple[tuple[re.Pattern[str], float], ...] = tuple(
    (re.compile(pattern), value)
    for pattern, value in (
        (r"\bthree[- ]quarters?\b", 0.75),
        (r"\btwo[- ]thirds?\b", 2 / 3),
        (r"\b(?:one|a)[- ]tenth\b", 0.1),
        (r"\b(?:one[- ]|a )?fifth\b", 0.2),
        (r"\b(?:one[- ]|a )?quarter\b", 0.25),
        (r"\b(?:one[- ]|a )?third\b", 1 / 3),
        (r"\bhal(?:f|ved|ve|ving)\b", 0.5),
    )
)
_REMAINING_BEFORE = re.compile(
    r"\bto\s+(?:about|around|roughly|approximately|just|only|nearly|at most)?\s*$"
)
_REDUCTION_BEFORE = re.compile(
    r"\b(?:by|down|lose|loses|losing|lost|shed|minus|(?:reduction|cut|drop|decrease|loss) of)\s+"
    r"(?:about|around|roughly|approximately|up to|nearly|an?|some)?\s*$"
)
_REDUCTION_AFTER = re.compile(r"^\s*(?:reduction|cut|drop|decrease|decline|loss|lower|less\b)")

_SOLAR = re.compile(r"\b(solar|pv|photovoltaic|panels?|irradiance|rooftop generation)\b")
_SOLAR_ZERO = re.compile(
    r"\b(no (?:solar|pv|output|generation)|zero (?:solar|pv|output|generation)|offline|"
    r"shut ?down|out of service|switched off|unavailable|disconnected)\b"
)
_BLOCKED = (
    r"(?:not|no|never|cannot|can't|don't|avoid|without|unavailable|disabled|isolated|offline|"
    r"out of service|suspend|prohibit|forbid|block|lock|halt|stop|pause|maintenance|inspect)"
)
_DISCHARGE = re.compile(r"discharg|\b(drain|release energy|supply (?:the )?loads?|battery output)")
_HOLD = re.compile(r"\b(hold (?:its|the) (?:energy|charge)|output disabled)\b")
_CHARGE = re.compile(r"(?<!dis)charg|\b(top[- ]?up|take energy|absorb|refill|store energy)\b")
_RESERVE = re.compile(
    r"\b(reserve|at least|no less than|(?:go|drop|fall|dip)s? below|minimum of|keep|retain|"
    r"remain|maintain|state of charge|soc)\b"
)
_BATTERY = re.compile(r"\b(battery|batteries|storage|bess|state of charge|soc|stored)\b")
_GRID = re.compile(
    r"\b(grid|import|feeder|transformer|substation|utility|intake|mains|interconnect\w*)\b"
)
_CAP = re.compile(
    r"\b(exceed|limit\w*|cap\w*|at or below|no more than|not more than|maximum|max|under|below|"
    r"ceiling|restrict\w*|within)\b"
)


@dataclass(frozen=True)
class HoursReading:
    hours: tuple[int, ...]
    confident: bool  # explicit, unambiguous, whole-hour clock range inside one day


@dataclass(frozen=True)
class ValueReading:
    value: float
    confident: bool


@dataclass(frozen=True)
class HeuristicReading:
    directive: Directive
    explanation: str
    hours_confident: bool = False
    value_confident: bool = False


@dataclass(frozen=True)
class _Time:
    start: int
    end: int
    hour: int
    minute: int
    meridiem: str | None  # "am" | "pm" | None
    fixed: bool  # noon / midnight / HH:MM / >= 13: no meridiem needed


def normalise(note: str) -> str:
    text = note.lower().translate(_BANGLA_DIGITS)
    text = re.sub(r"[‐-―−]", "-", text)
    text = re.sub(r"\b([ap])\.\s?m\.?", r"\1m", text)
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)
    return text


def _tokens(text: str) -> list[_Time]:
    times: list[_Time] = []
    for match in _TOKEN.finditer(text):
        if match["noon"]:
            times.append(_Time(match.start(), match.end(), 12, 0, None, True))
        elif match["midnight"]:
            times.append(_Time(match.start(), match.end(), 0, 0, None, True))
        elif match["hour"]:
            hour, minute = int(match["hour"]), int(match["minute"] or 0)
            meridiem = match["mer"]
            if hour > 24 or minute > 59 or (meridiem and not 1 <= hour <= 12):
                continue
            fixed = meridiem is None and (match["minute"] is not None or hour >= 13 or hour == 0)
            times.append(_Time(match.start(), match.end(), hour, minute, meridiem, fixed))
        else:
            times.append(
                _Time(
                    match.start(), match.end(), _WORD_HOURS[match["word"]], 0, match["wmer"], False
                )
            )
    return times


def _clock(hour: int, meridiem: str) -> int:
    return hour % 12 + (12 if meridiem == "pm" else 0)


def _resolve_range(text: str, a: _Time, b: _Time, solar_context: bool) -> HoursReading | None:
    confident = a.minute == 0 and b.minute == 0

    def value(t: _Time, meridiem: str | None) -> int:
        return t.hour if t.fixed or meridiem is None else _clock(t.hour, meridiem)

    a_mer, b_mer = a.meridiem, b.meridiem
    if not (a.fixed or a_mer) and not (b.fixed or b_mer):
        # Neither side names AM/PM: lean on context words, else it is a guess.
        context = "pm" if _EVENING.search(text) else "am" if _MORNING.search(text) else None
        if context:
            a_mer = b_mer = context
        else:
            confident = False
            if solar_context:  # panels only matter in daylight: 1..6 reads as afternoon
                a_mer = "pm" if a.hour < 7 else "am"
                b_mer = "pm" if b.hour < 8 else "am"
    start = value(a, a_mer or (None if a.fixed else b_mer))
    end = value(b, b_mer or (None if b.fixed else a_mer))

    if not (a.fixed or a.meridiem) and start >= end and (b.fixed or b_mer):
        start = _clock(a.hour, "am" if (b_mer or "pm") == "pm" else "pm")  # "11 to 1 pm"
    if not (b.fixed or b.meridiem) and end <= start and (a.fixed or a_mer):
        end = _clock(b.hour, "pm" if (a_mer or "am") == "am" else "am")  # "10 am to 2"
    if end == 0 or (end <= start and b.hour in (0, 12) and b.meridiem in (None, "am")):
        end = 24  # "... until midnight"
    if b.minute:
        end += 1  # partial final hour still touches that hour

    if start == end or not (0 <= start <= 23 and 0 < end <= 24):
        return None
    if start < end:
        return HoursReading(tuple(range(start, end)), confident)
    wrapped = tuple(h for h in range(24) if h >= start or h < end)
    return HoursReading(wrapped, False)


def parse_hours(note: str, *, solar_context: bool = False) -> HoursReading | None:
    """Whole hours named by the note, start inclusive / end exclusive."""
    text = normalise(note)
    if _ALL_DAY.search(text):
        return HoursReading(tuple(range(24)), True)

    times = _tokens(text)
    for a, b in pairwise(times):
        between = text[a.end : b.start]
        if not _SEPARATOR.match(between):
            continue
        if between.strip() == "and" and "between" not in text[max(0, a.start - 12) : a.start]:
            continue
        reading = _resolve_range(text, a, b, solar_context)
        if reading:
            return reading

    for t in times:  # "at 6 pm", "during the 18:00 hour"
        anchored = re.search(r"\b(at|during|around|for)\s+(the\s+)?$", text[: t.start])
        if (t.fixed or t.meridiem) and (anchored or text[t.end :].lstrip().startswith("hour")):
            hour = t.hour if t.fixed else _clock(t.hour, t.meridiem or "am")
            if 0 <= hour <= 23:
                return HoursReading((hour,), False)
    return None


def parse_solar_factor(note: str) -> ValueReading | None:
    """Usable fraction of solar that REMAINS (an 80% reduction is 0.2)."""
    text = normalise(note)
    found: tuple[int, int, float] | None = None
    percent = _PERCENT.search(text)
    if percent and 0 <= float(percent[1]) <= 100:
        found = (percent.start(), percent.end(), float(percent[1]) / 100)
    else:
        for pattern, value in _FRACTIONS:
            fraction = pattern.search(text)
            if fraction:
                found = (fraction.start(), fraction.end(), value)
                break
    if found is None:
        return ValueReading(0.0, False) if _SOLAR_ZERO.search(text) else None

    start, end, share = found
    before, after = text[max(0, start - 40) : start], text[end : end + 30]
    if _REMAINING_BEFORE.search(before):
        return ValueReading(round(share, 6), True)
    if _REDUCTION_BEFORE.search(before) or _REDUCTION_AFTER.search(after):
        return ValueReading(round(1 - share, 6), True)
    return ValueReading(round(share, 6), share == 0.5)  # half is 0.5 either way


def parse_energy(note: str, capacity_kwh: float, *, allow_percent: bool) -> ValueReading | None:
    """A kWh quantity; with `allow_percent`, a percentage is taken as a share of capacity."""
    text = normalise(note)
    energies = _ENERGY.findall(text)
    if energies:
        return ValueReading(float(energies[0]), len(energies) == 1)
    if allow_percent:
        percents = _PERCENT.findall(text)
        if percents and 0 <= float(percents[0]) <= 100:
            return ValueReading(
                round(float(percents[0]) / 100 * capacity_kwh, 6), len(percents) == 1
            )
        for pattern, value in _FRACTIONS:
            if pattern.search(text):
                return ValueReading(round(value * capacity_kwh, 6), False)
    return None


def interpret(note: str, capacity_kwh: float) -> HeuristicReading:
    """Best explicit reading of one note; `no_op` whenever a required part is missing."""
    text = normalise(note)
    no_op = HeuristicReading(NoOp(), "No explicit limit on today's schedule was recognised.")
    if _NOT_TODAY.search(text):
        return no_op

    solar = bool(_SOLAR.search(text))
    window = parse_hours(note, solar_context=solar)
    if window is None:
        return no_op
    hours, sure = window.hours, window.confident

    if solar:
        factor = parse_solar_factor(note)
        if factor is None:
            return no_op
        return HeuristicReading(
            SolarReduction(hours=hours, factor=factor.value),
            f"Usable solar is limited to {factor.value:g} of forecast in the stated window.",
            sure,
            factor.confident,
        )

    blocked = re.search(_BLOCKED, text) is not None
    if (_DISCHARGE.search(text) and blocked) or _HOLD.search(text):
        return HeuristicReading(
            NoDischarge(hours=hours),
            "Battery discharging is unavailable in the window.",
            sure,
            True,
        )

    if _BATTERY.search(text) and _RESERVE.search(text):
        reserve = parse_energy(note, capacity_kwh, allow_percent=True)
        if reserve is not None and 0 <= reserve.value <= capacity_kwh:
            return HeuristicReading(
                MinReserve(hours=hours, minimum_energy_kwh=reserve.value),
                f"At least {reserve.value:g} kWh must stay in the battery in the window.",
                sure,
                reserve.confident,
            )

    if _CHARGE.search(text) and blocked:
        return HeuristicReading(
            NoCharge(hours=hours), "Battery charging is unavailable in the window.", sure, True
        )

    if _GRID.search(text) and _CAP.search(text):
        cap = parse_energy(note, capacity_kwh, allow_percent=False)
        if cap is not None:
            return HeuristicReading(
                MaxGrid(hours=hours, max_grid_kwh=cap.value),
                f"Grid import is capped at {cap.value:g} kWh per hour in the window.",
                sure,
                cap.confident,
            )
    return no_op
