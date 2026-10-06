"""Tests fuer die Interpretation deutscher Datums- und Zeitangaben."""

from collections.abc import Generator
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.delivery_tracker.dates import (
    local_day_range,
    parse_eta,
    parse_german_date,
    parse_time_window,
    whole_local_days,
)

BERLIN = ZoneInfo("Europe/Berlin")
# Ein Dienstag, damit Wochentagsangaben eindeutig pruefbar sind.
TODAY = date(2026, 7, 28)


@pytest.fixture(autouse=True)
def berlin_timezone() -> Generator[None]:
    """Liefertermine sind lokal gemeint — hier durchgaengig Europe/Berlin."""
    original = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(BERLIN)
    yield
    dt_util.set_default_time_zone(original)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Zustellung am 30.07.2026", date(2026, 7, 30)),
        ("Zustellung am 30.07.26", date(2026, 7, 30)),
        ("voraussichtlich am 30.07.", date(2026, 7, 30)),
        ("Montag, 03.08.", date(2026, 8, 3)),
        ("am 3. August 2026", date(2026, 8, 3)),
        ("am 3. Aug.", date(2026, 8, 3)),
        ("am 3. März", date(2027, 3, 3)),
        ("heute noch bei Ihnen", date(2026, 7, 28)),
        ("kommt morgen an", date(2026, 7, 29)),
        ("uebermorgen", date(2026, 7, 30)),
        ("übermorgen", date(2026, 7, 30)),
        ("Wir liefern am Freitag", date(2026, 7, 31)),
        ("Wir liefern am Dienstag", date(2026, 7, 28)),
    ],
)
def test_parse_german_date(text: str, expected: date):
    """Die gaengigen Schreibweisen werden erkannt."""
    assert parse_german_date(text, today=TODAY) == expected


def test_konkretes_datum_schlaegt_wochentag():
    """In „Montag, 03.08." gewinnt das ausgeschriebene Datum."""
    assert parse_german_date("Montag, 03.08.", today=TODAY) == date(2026, 8, 3)


def test_datum_ohne_jahr_springt_ins_naechste_jahr():
    """Ein weit zurueckliegender Tag ohne Jahr ist der naechste, nicht der letzte."""
    assert parse_german_date("am 05.01.", today=TODAY) == date(2027, 1, 5)


@pytest.mark.parametrize("text", ["", "kein Datum weit und breit", "am 31.02.2026"])
def test_parse_german_date_ohne_treffer(text: str):
    """Fehlende und unmoegliche Daten ergeben None."""
    assert parse_german_date(text, today=TODAY) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("zwischen 10 und 14 Uhr", (time(10), time(14))),
        ("zwischen 10:00 und 14:30 Uhr", (time(10), time(14, 30))),
        ("10:00 - 14:00", (time(10), time(14))),
        ("von 8 bis 12 Uhr", (time(8), time(12))),
        ("gegen 14:30 Uhr", (time(14, 30), time(14, 30))),
        ("kein Zeitfenster", None),
    ],
)
def test_parse_time_window(text: str, expected: tuple[time, time] | None):
    """Zeitfenster und einzelne Zeitpunkte."""
    assert parse_time_window(text) == expected


def test_parse_eta_ganztags():
    """Ohne Zeitangabe umfasst das Fenster den ganzen lokalen Tag."""
    now = datetime(2026, 7, 28, 9, 0, tzinfo=BERLIN)
    start, end = parse_eta("voraussichtliche Zustellung am 30.07.2026", now=now)

    assert start == datetime(2026, 7, 29, 22, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 30, 22, 0, tzinfo=UTC)


def test_parse_eta_mit_zeitfenster():
    """Mit Zeitfenster wird auf die Stunde genau gerechnet — Ergebnis in UTC."""
    now = datetime(2026, 7, 28, 9, 0, tzinfo=BERLIN)
    start, end = parse_eta("morgen zwischen 10 und 14 Uhr", now=now)

    assert start == datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def test_parse_eta_ueber_mitternacht():
    """Ein Fenster, dessen Ende vor dem Start liegt, laeuft in den Folgetag."""
    now = datetime(2026, 7, 28, 9, 0, tzinfo=BERLIN)
    start, end = parse_eta("heute zwischen 22 und 2 Uhr", now=now)

    assert start is not None
    assert end is not None
    assert end > start
    assert end == datetime(2026, 7, 29, 0, 0, tzinfo=UTC)


def test_parse_eta_ohne_datum():
    """Ohne erkennbares Datum gibt es kein Fenster."""
    now = datetime(2026, 7, 28, 9, 0, tzinfo=BERLIN)
    assert parse_eta("wir melden uns", now=now) == (None, None)


def test_ganze_tage_aus_parse_eta():
    """Ein Termin ohne Uhrzeit umfasst ganze lokale Tage, das Ende exklusiv."""
    now = datetime(2026, 7, 28, 9, 0, tzinfo=BERLIN)
    start, end = parse_eta("Zustellung am 30.07.2026", now=now)
    assert start is not None

    assert whole_local_days(start, end) == (date(2026, 7, 30), date(2026, 7, 31))
    # Der Folgetag ist nicht mehr „erwartet".
    assert local_day_range(start, end) == (date(2026, 7, 30), date(2026, 7, 30))


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (datetime(2026, 7, 30, 10, 0, tzinfo=BERLIN), datetime(2026, 7, 30, 14, 0, tzinfo=BERLIN)),
        (datetime(2026, 7, 30, 0, 0, tzinfo=BERLIN), None),
        (datetime(2026, 7, 30, 0, 0, tzinfo=BERLIN), datetime(2026, 7, 30, 0, 0, tzinfo=BERLIN)),
        (datetime(2026, 7, 30, 0, 0, tzinfo=UTC), datetime(2026, 7, 31, 0, 0, tzinfo=UTC)),
    ],
)
def test_keine_ganzen_tage(start: datetime, end: datetime | None):
    """Uhrzeit, fehlendes Ende, Nulldauer oder Mitternacht einer fremden Zone."""
    assert whole_local_days(start, end) is None


def test_tagesspanne_mit_zeitfenster():
    """Ein Fenster ueber Mitternacht beruehrt beide Tage."""
    start = datetime(2026, 7, 30, 22, 0, tzinfo=BERLIN)
    end = datetime(2026, 7, 31, 2, 0, tzinfo=BERLIN)
    assert local_day_range(start, end) == (date(2026, 7, 30), date(2026, 7, 31))
    assert local_day_range(start, None) == (date(2026, 7, 30), date(2026, 7, 30))
