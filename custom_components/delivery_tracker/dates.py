"""Interpretation deutscher Datums- und Zeitangaben aus Mailtexten.

Zusteller schreiben Liefertermine in allen erdenklichen Formen: „28.07.2026",
„Montag, 28.07.", „28. Juli", „morgen zwischen 10 und 14 Uhr". Dieses Modul
uebersetzt solche Angaben in konkrete, timezone-aware Zeitpunkte.

Alle Rueckgaben sind UTC; gerechnet wird in der HA-Zeitzone, weil Liefertermine
immer lokal gemeint sind.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Final

from homeassistant.util import dt as dt_util

# Ein Datum ohne Jahresangabe wird auf das plausibelste Jahr gelegt. Liefertermine
# liegen in der Zukunft oder allenfalls wenige Tage zurueck (Zustellbestaetigung),
# darum ist das Fenster bewusst unsymmetrisch.
_PAST_TOLERANCE_DAYS: Final = 30
_FUTURE_WINDOW_DAYS: Final = 300

_MONTHS: Final[dict[str, int]] = {
    "januar": 1,
    "jan": 1,
    "februar": 2,
    "feb": 2,
    "maerz": 3,
    "mrz": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "mai": 5,
    "juni": 6,
    "jun": 6,
    "juli": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sept": 9,
    "sep": 9,
    "oktober": 10,
    "okt": 10,
    "november": 11,
    "nov": 11,
    "dezember": 12,
    "dez": 12,
}

_WEEKDAYS: Final[dict[str, int]] = {
    "montag": 0,
    "dienstag": 1,
    "mittwoch": 2,
    "donnerstag": 3,
    "freitag": 4,
    "samstag": 5,
    "sonnabend": 5,
    "sonntag": 6,
}

_RELATIVE_DAYS: Final[dict[str, int]] = {
    "heute": 0,
    "morgen": 1,
    "uebermorgen": 2,
}

_UMLAUTS: Final[dict[int, str]] = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def _alternation(words: dict[str, int]) -> str:
    """Regex-Alternation, laengste Variante zuerst (sonst gewinnt die Abkuerzung)."""
    return "|".join(sorted(words, key=len, reverse=True))


# 28.07.2026 / 28.07.26 / 28.07.
_NUMERIC_DATE: Final = re.compile(r"\b(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4}|\d{2})?(?!\d)")
# 28. Juli 2026 / 28 Juli / 1. Aug.
_TEXTUAL_DATE: Final = re.compile(
    rf"\b(\d{{1,2}})\.?\s*({_alternation(_MONTHS)})\.?\s*(\d{{4}})?(?!\d)"
)
_RELATIVE_DATE: Final = re.compile(rf"\b({_alternation(_RELATIVE_DAYS)})\b")
_WEEKDAY_DATE: Final = re.compile(rf"\b({_alternation(_WEEKDAYS)})\b")
_NEXT_WORKDAY: Final = re.compile(r"\bnaechste[nmr]?\s+werktag")

# „zwischen 10 und 14 Uhr", „10:00 - 14:00 Uhr", „von 8 bis 12 Uhr"
_TIME_RANGE_UHR: Final = re.compile(
    r"(\d{1,2})(?:[:.](\d{2}))?(?:\s*uhr)?\s*(?:[-\u2013\u2014]|bis|und)"
    r"\s*(\d{1,2})(?:[:.](\d{2}))?\s*uhr\b"
)
# Dieselbe Spanne ohne „Uhr", dafuer mit Minuten auf beiden Seiten.
_TIME_RANGE_EXACT: Final = re.compile(
    r"(\d{1,2})[:.](\d{2})\s*(?:[-\u2013\u2014]|bis|und)\s*(\d{1,2})[:.](\d{2})\b"
)
# Einzelner Zeitpunkt: „gegen 14:30 Uhr", „um 9 Uhr"
_SINGLE_TIME: Final = re.compile(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*uhr\b")


def _fold(value: str) -> str:
    """Text vereinheitlichen: Kleinbuchstaben, Umlaute ausgeschrieben."""
    return value.lower().translate(_UMLAUTS)


def _build_date(day: int, month: int, year: int | None, today: date) -> date | None:
    """Aus Tag/Monat/Jahr ein Datum bauen.

    Fehlt das Jahr, gewinnt das erste, das den Tag in ein plausibles Fenster um
    den Bezugstag legt — „05.01." im Juli meint den kommenden Januar.
    """
    if year is not None:
        return _safe_date(year + 2000 if year < 100 else year, month, day)

    for candidate_year in (today.year, today.year + 1, today.year - 1):
        candidate = _safe_date(candidate_year, month, day)
        if candidate is None:
            continue
        offset = (candidate - today).days
        if -_PAST_TOLERANCE_DAYS <= offset <= _FUTURE_WINDOW_DAYS:
            return candidate
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    """Ein Datum bauen; unmoegliche Kombinationen ergeben `None` statt einer Exception."""
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_numeric(text: str, today: date) -> date | None:
    """„28.07.2026", „28.07.26", „28.07."."""
    for match in _NUMERIC_DATE.finditer(text):
        raw_year = match.group(3)
        parsed = _build_date(
            int(match.group(1)),
            int(match.group(2)),
            int(raw_year) if raw_year else None,
            today,
        )
        if parsed is not None:
            return parsed
    return None


def _parse_textual(text: str, today: date) -> date | None:
    """„28. Juli 2026", „1. Aug."."""
    for match in _TEXTUAL_DATE.finditer(text):
        raw_year = match.group(3)
        parsed = _build_date(
            int(match.group(1)),
            _MONTHS[match.group(2)],
            int(raw_year) if raw_year else None,
            today,
        )
        if parsed is not None:
            return parsed
    return None


def _parse_relative(text: str, today: date) -> date | None:
    """„heute", „morgen", „uebermorgen"."""
    match = _RELATIVE_DATE.search(text)
    if match is None:
        return None
    return today + timedelta(days=_RELATIVE_DAYS[match.group(1)])


def _parse_next_workday(text: str, today: date) -> date | None:
    """„naechster Werktag" — Montag bis Samstag, Feiertage bleiben aussen vor."""
    if _NEXT_WORKDAY.search(text) is None:
        return None
    day = today + timedelta(days=1)
    if day.weekday() == _WEEKDAYS["sonntag"]:
        day += timedelta(days=1)
    return day


def _parse_weekday(text: str, today: date) -> date | None:
    """„Montag" — gemeint ist der naechste, wobei der heutige Tag zaehlt."""
    match = _WEEKDAY_DATE.search(text)
    if match is None:
        return None
    ahead = (_WEEKDAYS[match.group(1)] - today.weekday()) % 7
    return today + timedelta(days=ahead)


def parse_german_date(text: str, *, today: date | None = None) -> date | None:
    """Das erste erkennbare Datum aus einem Text lesen.

    Reihenfolge der Versuche: numerisch, ausgeschriebener Monat, Relativangabe,
    naechster Werktag, Wochentag. Damit gewinnt in „Montag, 28.07." das konkrete Datum.
    """
    if not text:
        return None

    reference = today if today is not None else dt_util.now().date()
    folded = _fold(text)
    for parser in (
        _parse_numeric,
        _parse_textual,
        _parse_relative,
        _parse_next_workday,
        _parse_weekday,
    ):
        parsed = parser(folded, reference)
        if parsed is not None:
            return parsed
    return None


def _to_time(hour: str, minute: str | None) -> time | None:
    """Stunde/Minute in eine `time` ueberfuehren; unsinnige Werte ergeben `None`."""
    try:
        return time(int(hour), int(minute) if minute else 0)
    except ValueError:
        return None


def parse_time_window(text: str) -> tuple[time, time] | None:
    """Ein Zeitfenster aus einem Text lesen.

    Erkannt werden Spannen („zwischen 10 und 14 Uhr", „10:00 - 14:00") sowie
    einzelne Zeitpunkte, die als Fenster der Laenge null zurueckkommen.
    """
    if not text:
        return None

    folded = _fold(text)
    for pattern in (_TIME_RANGE_UHR, _TIME_RANGE_EXACT):
        match = pattern.search(folded)
        if match is None:
            continue
        start = _to_time(match.group(1), match.group(2))
        end = _to_time(match.group(3), match.group(4))
        if start is not None and end is not None:
            return start, end

    match = _SINGLE_TIME.search(folded)
    if match is not None and (single := _to_time(match.group(1), match.group(2))) is not None:
        return single, single
    return None


def whole_local_days(start: datetime, end: datetime | None) -> tuple[date, date] | None:
    """Erster Tag und Tag nach dem Ende, wenn das Fenster ganze lokale Tage umfasst.

    So legt `parse_eta` einen Termin ohne Uhrzeit ab: von Mitternacht bis
    Mitternacht in der HA-Zeitzone. Ein Fenster mit Uhrzeit ergibt `None`.
    """
    start_local = dt_util.as_local(start)
    end_local = dt_util.as_local(end) if end is not None else None
    if (
        end_local is None
        or end_local <= start_local
        or start_local.time() != time.min
        or end_local.time() != time.min
    ):
        return None
    return start_local.date(), end_local.date()


def local_day_range(start: datetime, end: datetime | None) -> tuple[date, date]:
    """Erster und letzter lokaler Tag, die ein Fenster beruehrt (beide inklusive).

    Ein Ganztagsfenster endet um Mitternacht des Folgetags — der gehoert nicht dazu.
    """
    if (days := whole_local_days(start, end)) is not None:
        return days[0], days[1] - timedelta(days=1)
    return dt_util.as_local(start).date(), dt_util.as_local(end or start).date()


def parse_eta(text: str, *, now: datetime | None = None) -> tuple[datetime | None, datetime | None]:
    """Aus einem Textstueck das Lieferfenster als `(start, ende)` in UTC lesen.

    Ohne Zeitangabe umfasst das Fenster den ganzen lokalen Tag. Ist kein Datum
    erkennbar, kommt `(None, None)` zurueck.
    """
    reference = dt_util.as_local(now) if now is not None else dt_util.now()
    day = parse_german_date(text, today=reference.date())
    if day is None:
        return None, None

    window = parse_time_window(text)
    if window is None:
        start_local = dt_util.start_of_local_day(day)
        return dt_util.as_utc(start_local), dt_util.as_utc(start_local + timedelta(days=1))

    start_time, end_time = window
    start_local = datetime.combine(day, start_time, tzinfo=dt_util.DEFAULT_TIME_ZONE)
    end_local = datetime.combine(day, end_time, tzinfo=dt_util.DEFAULT_TIME_ZONE)
    if end_local < start_local:
        # „22 bis 2 Uhr" laeuft ueber Mitternacht.
        end_local += timedelta(days=1)
    return dt_util.as_utc(start_local), dt_util.as_utc(end_local)
