"""Repairs-Eintraege fuer Fehler, die nur der Nutzer beheben kann.

Ein Eintrag entsteht, wenn eine Quelle an einem dauerhaften Fehler ruht:
abgelehnte Anmeldung am Postfach, verschwundener Ordner, ungueltiger oder
aufgebrauchter API-Schluessel. Ein kurz nicht erreichbarer Server ist dagegen
Normalbetrieb mit Reconnect und bekommt keinen Eintrag.

Der Coordinator gleicht die Eintraege nach jedem Durchlauf ab
(`async_sync_source_issues`). Ein Eintrag verschwindet erst, wenn seine Quelle
wieder erfolgreich abgerufen hat oder es sie nicht mehr gibt — nach einer
Neukonfiguration also mit dem ersten erfolgreichen Abruf, nicht schon beim
Reload, solange der Ausgang noch offen ist.

Bewusst nicht fixable: Die Behebung ist der Reconfigure-Schritt des Subentrys
aus Step 2.1, auf den der Text verweist. Ein eigener Repair-Flow wuerde ihn nur
nachbauen.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, ISSUE_API_QUOTA_EXHAUSTED

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .coordinator import SourceHandle

# Schluessel in `IssueEntry.data`, ueber die ein Eintrag seiner Quelle zugeordnet wird.
_DATA_ENTRY_ID = "entry_id"
_DATA_SUBENTRY_ID = "subentry_id"


def source_issue_id(subentry_id: str, translation_key: str) -> str:
    """ID eines Eintrags: je Quelle und Fehlerart einer."""
    return f"{translation_key}_{subentry_id}"


@callback
def async_sync_source_issues(
    hass: HomeAssistant, entry: ConfigEntry, sources: Mapping[str, SourceHandle]
) -> None:
    """Eintraege anlegen, wo eine Quelle ruht, und erledigte entfernen."""
    for key, handle in sources.items():
        if handle.issue is None:
            continue
        ir.async_create_issue(
            hass,
            DOMAIN,
            source_issue_id(key, handle.issue),
            data={_DATA_ENTRY_ID: entry.entry_id, _DATA_SUBENTRY_ID: key},
            is_fixable=False,
            severity=(
                ir.IssueSeverity.WARNING
                if handle.issue == ISSUE_API_QUOTA_EXHAUSTED
                else ir.IssueSeverity.ERROR
            ),
            translation_key=handle.issue,
            translation_placeholders={"source": handle.title},
        )

    for issue in _entry_issues(hass, entry):
        handle = sources.get(str((issue.data or {}).get(_DATA_SUBENTRY_ID)))
        if handle is None or (handle.issue != issue.translation_key and handle.settled):
            ir.async_delete_issue(hass, DOMAIN, issue.issue_id)


@callback
def async_clear_issues(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Alle Eintraege eines Entries entfernen — beim Loeschen des Entries."""
    for issue in _entry_issues(hass, entry):
        ir.async_delete_issue(hass, DOMAIN, issue.issue_id)


def _entry_issues(hass: HomeAssistant, entry: ConfigEntry) -> list[ir.IssueEntry]:
    """Die Eintraege dieser Integration, die zu diesem Entry gehoeren."""
    return [
        issue
        for (domain, _issue_id), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN and (issue.data or {}).get(_DATA_ENTRY_ID) == entry.entry_id
    ]
