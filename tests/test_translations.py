"""Die Uebersetzungsdateien passen zu `strings.json` und zum Code.

Der Abgleich ist die Absicherung gegen die haeufigste Fehlerklasse in
HA-Integrationen: ein Schluessel, der nur in einer Sprache fehlt, zeigt in der
Oberflaeche den rohen Schluessel statt eines Textes. Er bleibt fest im Repo.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from custom_components.delivery_tracker import config_flow, const, sensor, services
from custom_components.delivery_tracker.const import (
    CONF_DESTINATION_RULES,
    SUBENTRY_API_SOURCE,
    SUBENTRY_DESTINATION,
    SUBENTRY_MAILBOX,
)
from custom_components.delivery_tracker.coordinator import SourceState

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


def _key_diff(
    reference: dict[str, Any], translation: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Fehlende und ueberzaehlige Schluessel einer Uebersetzung gegenueber `strings.json`."""
    expected, actual = set(_flatten(reference)), set(_flatten(translation))
    return sorted(expected - actual), sorted(actual - expected)


STRINGS = _flatten(_load(COMPONENT / "strings.json"))


@pytest.mark.parametrize("language", LANGUAGES)
def test_translation_has_same_keys_as_strings(language: str) -> None:
    """Jeder Schluessel aus `strings.json` existiert in der Uebersetzung und umgekehrt."""
    translation = _load(COMPONENT / "translations" / f"{language}.json")

    assert _key_diff(_load(COMPONENT / "strings.json"), translation) == ([], [])


def test_abgleich_findet_entfernten_und_zusaetzlichen_schluessel() -> None:
    """Gegenprobe: Der Abgleich schlaegt bei einer Abweichung tatsaechlich an."""
    reference = _load(COMPONENT / "strings.json")
    translation = _load(COMPONENT / "translations" / "en.json")
    del translation["issues"]["mailbox_auth_failed"]["title"]
    translation["entity"]["sensor"]["problems"]["extra"] = "Extra"

    assert _key_diff(reference, translation) == (
        ["issues.mailbox_auth_failed.title"],
        ["entity.sensor.problems.extra"],
    )


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
        f"{base}.{SUBENTRY_API_SOURCE}.error.{config_flow.ERROR_CANNOT_CONNECT}",
        f"{base}.{SUBENTRY_API_SOURCE}.error.{config_flow.ERROR_UNKNOWN}",
        f"{base}.{SUBENTRY_API_SOURCE}.abort.{config_flow.ABORT_ALREADY_CONFIGURED}",
        f"{base}.{SUBENTRY_API_SOURCE}.abort.reconfigure_successful",
        *(
            f"{base}.{SUBENTRY_DESTINATION}.step.{step}.data.{rule}"
            for step in ("user", "reconfigure")
            for rule in CONF_DESTINATION_RULES
        ),
        "config.abort.single_instance_allowed",
        f"config.abort.{config_flow.ABORT_REAUTH_SUCCESSFUL}",
        f"config.abort.{config_flow.ABORT_REAUTH_UNKNOWN_SOURCE}",
        "config.step.reauth_confirm.data.password",
        "config.step.reauth_confirm.data.api_key",
        *(
            f"config.error.{error}"
            for error in (
                config_flow.ERROR_CANNOT_CONNECT,
                config_flow.ERROR_INVALID_AUTH,
                config_flow.ERROR_FOLDER_NOT_FOUND,
                config_flow.ERROR_INVALID_API_KEY,
                config_flow.ERROR_UNKNOWN,
            )
        ),
    ]

    assert [key for key in expected if key not in STRINGS] == []


SENSOR_KEYS = [
    *(description.translation_key for description in sensor.COUNTERS),
    # HA macht aus `_attr_translation_key` eine Property; an der Klasse ist der Wert nicht lesbar.
    "carrier",
    "destination",
    "last_update",
    "sources",
]


def test_sensor_keys_exist_in_strings_and_icons() -> None:
    """Jeder Sensor hat Namen und Icon, der Quellen-Sensor einen Text je Zustand."""
    icons = _flatten(_load(COMPONENT / "icons.json"))
    expected = [f"entity.sensor.{key}.name" for key in SENSOR_KEYS]
    expected += [f"entity.sensor.sources.state.{state.value}" for state in SourceState]

    assert [key for key in expected if key not in STRINGS] == []
    assert [key for key in SENSOR_KEYS if f"entity.sensor.{key}.default" not in icons] == []


def test_repairs_haben_titel_und_beschreibung() -> None:
    """Jeder Repairs-Eintrag aus `const.ISSUE_*` hat Titel und Text mit der Quelle."""
    issues = [value for name, value in vars(const).items() if name.startswith("ISSUE_")]
    assert len(issues) == 4

    for issue in issues:
        for part in ("title", "description"):
            key = f"issues.{issue}.{part}"
            assert key in STRINGS, key
            assert "{source}" in STRINGS[key], key


def test_actions_und_fehlermeldungen_haben_texte() -> None:
    """Jede Action und jedes Feld aus `services.yaml`, jeder Fehler der Actions."""
    actions = yaml.safe_load((COMPONENT / "services.yaml").read_text(encoding="utf-8"))
    expected = [
        f"services.{action}.{part}" for action in actions for part in ("name", "description")
    ]
    expected += [
        f"services.{action}.fields.{field}.{part}"
        for action, spec in actions.items()
        for field in (spec or {}).get("fields", {})
        for part in ("name", "description")
    ]
    errors = [value for name, value in vars(services).items() if name.startswith("ERROR_")]
    assert errors
    expected += [f"exceptions.{error}.message" for error in errors]

    assert [key for key in expected if key not in STRINGS] == []
