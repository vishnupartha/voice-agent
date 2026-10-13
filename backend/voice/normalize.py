"""Rewrite text into the words a person would say, before TTS reads it.

TTS models read "10:03" as "ten zero three" and "$5.50" unpredictably, so
times, dates, money, percentages, ordinals, years and plain numbers become
words here. Order matters: specific patterns run before the generic number one.
"""

from __future__ import annotations

import re

from num2words import num2words

_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]  # fmt: skip


def _n(value: str | int | float) -> str:
    return str(num2words(value)).replace("-", " ")


def _hour_minute(h: int, m: int, ampm: str | None) -> str:
    hour = _n(h)
    if m == 0:
        words = hour if ampm else f"{hour} o'clock"
    elif m < 10:
        words = f"{hour} oh {_n(m)}"
    else:
        words = f"{hour} {_n(m)}"
    if ampm:
        words += " A M" if ampm.lower().startswith("a") else " P M"
        if ampm.endswith("."):
            words += "."  # "p.m." may also have ended the sentence
    return words


def _time(m: re.Match[str]) -> str:
    return _hour_minute(int(m["h"]), int(m["m"]), m["ampm"])


def _bare_ampm(m: re.Match[str]) -> str:
    return _hour_minute(int(m["h"]), 0, m["ampm"])


def _money(m: re.Match[str]) -> str:
    dollars = int(m["d"].replace(",", ""))
    cents = int(m["c"]) if m["c"] else 0
    words = f"{_n(dollars)} dollar{'s' if dollars != 1 else ''}"
    if cents:
        words += f" and {_n(cents)} cent{'s' if cents != 1 else ''}"
    return words


def _date(m: re.Match[str]) -> str:
    month, day = int(m["mo"]), int(m["d"])
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return m[0]
    words = f"{_MONTHS[month - 1]} {_ordinal(day)}"
    if m["y"]:
        year = int(m["y"])
        words += f", {_year(year + 2000 if year < 100 else year)}"
    return words


def _ordinal(n: int) -> str:
    return str(num2words(n, to="ordinal")).replace("-", " ")


def _year(y: int) -> str:
    return str(num2words(y, to="year")).replace("-", " ")


def _number(m: re.Match[str]) -> str:
    raw = m[0].replace(",", "")
    return _n(float(raw) if "." in raw else int(raw))


_RULES: list[tuple[re.Pattern[str], object]] = [
    # 10:03, 3:30 PM, 9:00am
    (re.compile(r"\b(?P<h>\d{1,2}):(?P<m>\d{2})\s*(?P<ampm>[AaPp](?:\.\s?[Mm]\.|\s?[Mm]))?(?!\w)"), _time),
    # 3pm, 11 AM
    (re.compile(r"\b(?P<h>\d{1,2})\s*(?P<ampm>[AaPp](?:\.\s?[Mm]\.|\s?[Mm]))(?!\w)"), _bare_ampm),
    # $5, $1,200.50
    (re.compile(r"\$(?P<d>\d[\d,]*)(?:\.(?P<c>\d{2}))?\b"), _money),
    # 45%, 2.5%
    (re.compile(r"(?P<v>\d+(?:\.\d+)?)\s?%"), lambda m: f"{_n(m['v'])} percent"),
    # 10/13, 10/13/2026 (US order)
    (re.compile(r"\b(?P<mo>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d{2}|\d{4}))?\b"), _date),
    # 13th, 1st
    (re.compile(r"\b(?P<v>\d+)(?:st|nd|rd|th)\b"), lambda m: _ordinal(int(m["v"]))),
    # 2-4, 9–5 -> "2 to 4"
    (re.compile(r"\b(\d+)\s?[-–]\s?(\d+)\b"), lambda m: f"{m[1]} to {m[2]}"),
    # years
    (re.compile(r"\b(19|20)\d{2}\b"), lambda m: _year(int(m[0]))),
    # anything else numeric
    (re.compile(r"\b\d[\d,]*(?:\.\d+)?\b"), _number),
    (re.compile(r"\s&\s"), lambda m: " and "),
    (re.compile(r"\be\.g\.", re.I), lambda m: "for example"),
    (re.compile(r"\bi\.e\.", re.I), lambda m: "that is"),
    (re.compile(r"\bvs\.?(?=\s)", re.I), lambda m: "versus"),
]


def spoken_form(text: str) -> str:
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)  # type: ignore[arg-type]
    return text
