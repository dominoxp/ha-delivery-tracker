"""Die Uebersetzungsdateien passen zu `strings.json` und zum Config-Flow."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from custom_components.delivery_tracker import config_flow
from custom_components.delivery_tracker.const import (
    CONF_DESTINATION_RULES,
    SUBENTRY_API_SOURCE,
    SUBENTRY_DESTINATION,
    SUBENTRY_MAILBOX,
)

COMPONENT = Path(__file__).parent.parent / "custom_components" / "delivery_tracker"
LANGUAGES = ("de", "en")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Verschachtelte Texte als `a.b.c` -> Text."""
    flat: dict[str, str] = {}
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{path}."))
        else:
            flat[path] = value
    return flat


STRINGS = _flatten(_load(COMPONENT / "strings.json"))


@pytest.mark.parametrize("language", LANGUAGES)
def test_translation_has_same_keys_as_strings(language: str) -> None:
    """Jeder Schluessel aus `strings.json` existiert in der Uebersetzung und umgekehrt."""
    translation = _flatten(_load(COMPONENT / "translations" / f"{language}.json"))

    assert sorted(set(STRINGS) - set(translation)) == []
    assert sorted(set(translation) - set(STRINGS)) == []


@pytest.mark.parametrize("language", LANGUAGES)
def test_translation_keeps_placeholders(language: str) -> None:
    """Platzhalter wie `{server}` stehen in jeder Sprache gleich drin."""
    translation = _flatten(_load(COMPONENT / "translations" / f"{language}.json"))

    for key, text in STRINGS.items():
        assert set(_PLACEHOLDER.findall(translation[key])) == set(_PLACEHOLDER.findall(text)), key
        assert translation[key].strip(), key


def test_flow_keys_exist_in_strings() -> None:
    """Fehler, Abbruchgruende und Felder aus dem Flow haben einen Text."""
    base = "config_subentries"
    expected = [
        f"{base}.{SUBENTRY_MAILBOX}.error.{config_flow.ERROR_CANNOT_CONNECT}",
        f"{base}.{SUBENTRY_MAILBOX}.error.{config_flow.ERROR_INVALID_AUTH}",
        f"{base}.{SUBENTRY_MAILBOX}.error.{config_flow.ERROR_FOLDER_NOT_FOUND}",
        f"{base}.{SUBENTRY_MAILBOX}.error.{config_flow.ERROR_UNKNOWN}",
        f"{base}.{SUBENTRY_MAILBOX}.abort.{config_flow.ABORT_ALREADY_CONFIGURED}",
        f"{base}.{SUBENTRY_MAILBOX}.abort.reconfigure_successful",
        f"{base}.{SUBENTRY_DESTINATION}.error.{config_flow.ERROR_INVALID_NAME}",
        f"{base}.{SUBENTRY_DESTINATION}.error.{config_flow.ERROR_NAME_EXISTS}",
        f"{base}.{SUBENTRY_DESTINATION}.error.{config_flow.ERROR_NO_RULES}",
        f"{base}.{SUBENTRY_DESTINATION}.abort.reconfigure_successful",
        f"{base}.{SUBENTRY_API_SOURCE}.error.{config_flow.ERROR_INVALID_API_KEY}",
        f"{base}.{SUBENTRY_API_SOURCE}.error.{config_flow.ERROR_UNKNOWN}",
        f"{base}.{SUBENTRY_API_SOURCE}.abort.{config_flow.ABORT_ALREADY_CONFIGURED}",
        f"{base}.{SUBENTRY_API_SOURCE}.abort.reconfigure_successful",
        *(
            f"{base}.{SUBENTRY_DESTINATION}.step.{step}.data.{rule}"
            for step in ("user", "reconfigure")
            for rule in CONF_DESTINATION_RULES
        ),
        "config.abort.single_instance_allowed",
    ]

    assert [key for key in expected if key not in STRINGS] == []
