# Step 2.2 — Coordinator & Entry-Setup

> Kontext: `planning.md` lesen. Setzt Step 2.1 voraus.

## Ziel

Alle Teile greifen ineinander: Beim Laden des Entrys starten die konfigurierten Quellen,
liefern Sendungen in den Store und benachrichtigen die Entities über Änderungen.

## Scope

**Gehört dazu:**
- `coordinator.py`: `DataUpdateCoordinator`, der die Ergebnisse aller Quellen einsammelt,
  über 0.2 mit dem Store zusammenführt, die Adress-Zuordnung aus 1.5 anwendet, die Retention
  bereinigt und die aufbereiteten Daten für die Entities bereitstellt.
- Aufbau der Quellen aus den `mailbox`- und `api_source`-Subentries beim Entry-Setup;
  Ablage in `ConfigEntry.runtime_data` als typisiertes Dataclass.
- Push-Betrieb: Meldet eine Quelle per IDLE eine neue Mail, wird sofort aktualisiert, statt auf
  das nächste Intervall zu warten.
- Fehlerisolierung: Eine ausgefallene Quelle darf die anderen nicht mitreißen. Pro Quelle
  Zustand und letzter Fehler merken (Grundlage für Diagnose-Sensor und Repairs).
- Retention auch für Sendungen, die nie abschließen: `async_purge` aus 0.2 räumt nur
  `is_closed`-Sendungen ab, eine Bestellbestätigung ohne Versandmail bliebe also für immer
  aktiv. Zusätzliche Regel: nicht abgeschlossen und `last_updated` älter als `stale_days` →
  ebenfalls entfernen. Standard 60 Tage, `0` schaltet ab; der Wert gehört in die Options aus 2.1.
- Sauberes `async_unload_entry`: Quellen schließen, Aufgaben abbrechen, Store final schreiben.
- Statuswechsel erkennen und im Coordinator-Ergebnis mitführen — 2.5 feuert daraus die Events.

**Gehört NICHT dazu:**
- Entities (2.3, 2.4), Actions und Events (2.5).

## Hinweise

- Der Coordinator darf nicht bei jedem Durchlauf alle Mails neu parsen; die Quellen liefern nur
  Neues, der Store hält den Bestand.
- Reload und Neustart dürfen keine Sendung verlieren und keine doppelt anlegen — der Store wird
  vor dem ersten Abruf geladen.
- Erster Durchlauf nach dem Start kann durch das Lookback aus 1.1 lange dauern: Setup darf davon
  nicht blockiert werden, der erste Abruf läuft im Hintergrund.
- Bei dauerhaft falschen IMAP-Zugangsdaten gehört `ConfigEntryAuthFailed` geworfen, damit HA
  einen Reauth-Hinweis anzeigt, statt endlos zu wiederholen.
- Alle Hintergrundaufgaben über die HA-eigenen Helfer anlegen, damit sie beim Unload
  mit abgeräumt werden.

## Nachtrag aus der Umsetzung (Oktober 2026)

- **`coordinator.py`:** `DeliveryTrackerCoordinator` baut je `mailbox`-Subentry eine `ImapSource`
  mit eigener `MailPipeline` (Kennzahlen je Postfach) und je `api_source` eine Quelle über
  `sources.API_SOURCE_FACTORIES` — die Tabelle ist leer, 3.1 trägt 17track ein. Ein Typ ohne
  Fabrik wird als `FAILED` an der Quelle vermerkt, das Setup läuft weiter.
- **Quellenzustand:** `SourceHandle` je Subentry (`coordinator.sources`, auch über
  `runtime_data.sources`) mit `SourceState` (`pending/ok/error/auth_failed/failed`), letztem
  Fehler, Zeitpunkten und Fehlschlägen in Folge. Im Ergebnis als `SourceStatus` (mit
  `connection` = `ImapState`), Grundlage für Diagnose-Sensor (2.3) und Diagnostics (2.6).
- **Fehlerklassen:** `sources.base` hat jetzt `SourceError` → `SourcePermanentError` →
  `SourceAuthError`; die IMAP-Fehler erben davon. API-Quellen in 3.1 sollten dieselben werfen.
- **Reauth statt `ConfigEntryAuthFailed`:** Die Exception aus `_async_update_data` markiert in HA
  den *ganzen* Coordinator als fehlgeschlagen (Entities unavailable, Takt steht). Das widerspricht
  der Fehlerisolierung. Der Coordinator ruft deshalb selbst `entry.async_start_reauth(...)` mit
  der Subentry-ID auf (das tut HA bei der Exception auch) und liefert die übrigen Quellen weiter.
  Eine Quelle mit dauerhaftem Fehler wird nicht mehr abgerufen, es gibt also keine
  Wiederholschleife. Neu im Config-Flow: `reauth` / `reauth_confirm` fragt Passwort bzw.
  API-Schlüssel ab, prüft es und aktualisiert den Subentry, der Update-Listener lädt neu.
- **Push:** `ImapSource` ruft `on_new_data` nach neuen Sendungen *und* wenn ihr Loop an einem
  dauerhaften Fehler endet. Der Coordinator startet daraus `async_request_refresh` als
  Entry-Hintergrundtask. Grundtakt `COORDINATOR_UPDATE_INTERVAL` = 300 s (bzw. kürzestes
  API-Intervall), API-Quellen werden nur alle `scan_interval` abgerufen.
- **Start:** Store laden → Coordinator (`data` sofort aus dem Store) → Postfach-Loops starten →
  erster Abruf als Hintergrundtask. Ein eigener Listener hält den Takt am Laufen, solange es
  noch keine Entities gibt; 2.5 hängt dort die Events an (`data.status_changes`).
- **Ergebnis `CoordinatorData`:** `shipments`, `active`, `by_carrier` / `by_destination` (nur
  aktive, ohne Zielort unter `unknown`), `by_status` (alle), `problems`, `with_status(...)`,
  `expected_on(day)` / `delivered_on(day)` (Methoden, damit der Tageswechsel ohne Abruf greift),
  `status_changes` (`StatusChange` mit `previous=None` für neue Sendungen), `removed`, `sources`.
  **Sortierung** (`shipment_sort_key`): mit Termin nach `eta_start` aufsteigend, danach ohne
  Termin nach `last_updated` absteigend, ID als Tiebreak. 2.3 und 4.2 verlassen sich darauf.
- **Retention:** `ShipmentStore(stale_days=...)`; `async_purge` entfernt zusätzlich aktive
  Sendungen mit `last_updated` älter als `stale_days` (0 = aus).
- **IMAP-Fortschritt** (`ImapProgress`) liegt je Subentry im Store (`source_states`), Einträge
  gelöschter Postfächer werden beim Setup verworfen.
- **Carrier-Filter:** `GlobalOptions.filter_registry` baut eine eigene Registry nur mit den
  aktiven Plugins; die globale, geteilte Registry bleibt unverändert.
- **Unload / Stopp:** `async_unload_entry` → `coordinator.async_shutdown()` (Takt stoppen, alle
  Quellen schließen, Fortschritt sichern) → `store.async_save()`. Beim HA-Stopp schließt ein
  `EVENT_HOMEASSISTANT_STOP`-Listener die Quellen ebenfalls, weil HA Entries dabei nicht entlädt.
- **Tests:** Fixture-Mails tragen Daten aus dem Juli 2026 und fielen unter `stale_days`. Die
  Coordinator-Tests setzen deshalb `stale_days: 0`, außer im Retention-Test.

## Akzeptanzkriterien

- Entry-Setup mit zwei Postfächern und einer API-Quelle startet alle drei; Unload beendet sie
  vollständig, es bleiben keine laufenden Aufgaben zurück.
- Eine Quelle, die eine Exception wirft, verhindert weder Setup noch die Ergebnisse der anderen;
  ihr Fehler ist abrufbar hinterlegt.
- Neue Mail per IDLE löst eine sofortige Aktualisierung aus.
- Neustart-Test: Sendungen aus dem Store sind nach dem Reload unverändert vorhanden.
- Falsche Zugangsdaten führen zu `ConfigEntryAuthFailed`, nicht zu einer Wiederholschleife.
- Ein Statuswechsel taucht als solcher im Coordinator-Ergebnis auf.
- Eine aktive Sendung ohne Aktualisierung seit `stale_days` verschwindet, eine mit frischer
  Mail bleibt — auch wenn sie schon länger läuft.

## Checkliste

- [x] coordinator.py mit Sammeln, Merging, Adress-Zuordnung und Retention
- [x] Retention greift auch bei aktiven Sendungen ohne Aktualisierung (`stale_days`)
- [x] Quellen-Aufbau aus Subentries, typisiertes runtime_data
- [x] Push-Aktualisierung bei IDLE-Ereignis
- [x] Fehlerisolierung mit Zustand und letztem Fehler je Quelle
- [x] Vorberechnete Gruppierungen für die Entities
- [x] Sauberes Unload ohne verwaiste Aufgaben
- [x] Statuswechsel im Ergebnis mitgeführt
- [x] Tests grün
