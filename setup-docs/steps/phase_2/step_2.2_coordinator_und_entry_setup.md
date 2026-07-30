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

- [ ] coordinator.py mit Sammeln, Merging, Adress-Zuordnung und Retention
- [ ] Retention greift auch bei aktiven Sendungen ohne Aktualisierung (`stale_days`)
- [ ] Quellen-Aufbau aus Subentries, typisiertes runtime_data
- [ ] Push-Aktualisierung bei IDLE-Ereignis
- [ ] Fehlerisolierung mit Zustand und letztem Fehler je Quelle
- [ ] Vorberechnete Gruppierungen für die Entities
- [ ] Sauberes Unload ohne verwaiste Aufgaben
- [ ] Statuswechsel im Ergebnis mitgeführt
- [ ] Tests grün
