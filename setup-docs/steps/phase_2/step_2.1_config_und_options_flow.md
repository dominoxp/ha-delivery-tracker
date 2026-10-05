# Step 2.1 — Config-Flow, Subentries & Options

> Kontext: `planning.md` lesen. Setzt Step 1.5 voraus.

## Ziel

Die Integration ist vollständig über die HA-Oberfläche einrichtbar: Postfächer, Zieladressen und
API-Quellen lassen sich anlegen, ändern und löschen, ohne YAML anzufassen.

## Scope

**Gehört dazu:**
- Ausbau von `config_flow.py` vom Minimal-Flow aus 0.1 zum vollständigen Flow.
- Haupt-Entry: nur ein Name; die eigentliche Konfiguration liegt in Subentries. Nur ein Entry
  pro Installation (`async_abort` bei einem zweiten).
- Subentry-Typ `mailbox`: Host, Port, Verschlüsselung, Benutzer, Passwort, Ordner, IDLE ja/nein,
  Polling-Intervall, Lookback-Tage. **Verbindungstest im Flow**, mit unterscheidbaren
  Fehlermeldungen für falsche Zugangsdaten, unerreichbaren Host und fehlenden Ordner
  (die Fehlerfälle aus 1.1). Ordnerauswahl nach erfolgreichem Login als Auswahlliste.
- Subentry-Typ `destination`: Anzeigename plus die Match-Regeln aus 1.5. Jede Adresse bekommt
  ein eigenes HA-Device, unter dem später ihre Sensoren hängen.
- Subentry-Typ `api_source`: Quellentyp (vorerst nur 17track), API-Schlüssel, Abfrageintervall.
  Der Typ füllt sich in 3.1 mit Leben; hier reicht das Gerüst inklusive Schlüssel-Prüfung.
- `reconfigure`-Schritt für alle drei Subentry-Typen.
- Options-Flow auf Entry-Ebene für die globalen Einstellungen: Retention-Tage,
  aktivierte Carrier, generischen Fallback ein/aus.
- Reload des Entrys bei jeder Änderung an Subentries oder Options.
- Übersetzungen `strings.json`, `de.json`, `en.json` für alle Schritte, Felder und Fehler.

**Gehört NICHT dazu:**
- Coordinator und Entities (2.2, 2.3), 17track-Implementierung (3.1).

## Hinweise

- Subentries sind der Grund, warum es keine selbstgebaute Listenverwaltung braucht — HA liefert
  Tabelle, Hinzufügen, Bearbeiten und Löschen mit. Nicht durch einen eigenen Menü-Flow ersetzen.
- Der Verbindungstest darf den Flow nicht blockieren: Zeitlimit setzen, damit ein nicht
  erreichbarer Server nicht in einen hängenden Dialog mündet.
- Passwörter gehören in `data` des Subentrys und dürfen in keiner Fehlermeldung auftauchen.
- Beim Löschen einer `destination` müssen die zugehörigen Sendungen auf „Unbekannt" umziehen,
  nicht verschwinden.
- Ein Postfach ohne konfigurierte Adressen muss funktionieren — alles landet dann bei „Unbekannt".

## Nachtrag aus der Umsetzung (Oktober 2026)

- **`entry.py`** liest die Konfiguration: `GlobalOptions.from_entry` (mit Standardwerten),
  `destinations_from_entry`, `mailbox_configs_from_entry` (→ `ImapConfig.from_mapping`).
  Dort liegt auch `DeliveryTrackerData` für `entry.runtime_data` (bisher Store und Zielorte);
  2.2 ergänzt Coordinator und Quellen dort, nicht in `__init__.py`.
- **Options:** `retention_days`, `stale_days` (für 2.2 schon angelegt, Standard 60),
  `generic_fallback` und `disabled_carriers`. Das Formular zeigt „aktive Zusteller“, gespeichert
  wird die **Negativliste**, damit ein neues Plugin nach einem Update automatisch aktiv ist.
  `GlobalOptions.carrier_enabled(id)` fasst beides zusammen; das Filtern der Registry ist 2.2.
- **Reload:** Der bestehende Update-Listener feuert in HA 2026.7 bei Options *und* bei
  Hinzufügen/Ändern/Löschen von Subentries — deshalb kein `OptionsFlowWithReload`
  (HA verbietet die Kombination mit Update-Listenern).
- **Zielorte:** Die ID entsteht beim Anlegen per `slugify(name)` („Büro“ → `buro`) und bleibt bei
  Umbenennung stabil. `unknown` und „Unbekannt“ sind gesperrt, mindestens eine Regel ist Pflicht.
  Devices legt `async_setup_entry` an; beim Löschen des Subentrys räumt HA sie selbst ab.
- **Umzug nach „Unbekannt“:** `async_setup_entry` lädt den Store und ruft
  `async_assign_destinations` mit den aktuellen Zielorten — der Reload nach dem Löschen erledigt
  den Umzug. Der Coordinator aus 2.2 übernimmt den Store aus `runtime_data`.
- **Postfach:** zwei Schritte (Zugangsdaten → Ordner). `ImapSource.async_list_folders` meldet
  sich an und liest `LIST` (ohne `\Noselect`, Namen aus modified UTF-7 lesbar gemacht); die
  Auswahlliste erlaubt freie Eingabe, ein falscher Ordner fällt beim abschließenden `SELECT` als
  `folder_not_found` auf. Neues Feld `ImapConfig.use_idle`. Gesamtzeitlimit
  `CONNECTION_TEST_TIMEOUT`. Beim Bearbeiten bleibt ein leeres Passwortfeld beim alten Wert.
- **API-Quelle:** Die Schlüsselprüfung für 17track ist vorerst nur formal
  (`API_KEY_VALIDATORS` in `config_flow.py`); 3.1 ersetzt sie durch eine echte Anfrage.

## Akzeptanzkriterien

- Vollständiger Flow-Test gegen die `hass`-Fixture: Entry anlegen, je ein Subentry aller drei
  Typen hinzufügen, eines bearbeiten, eines löschen.
- Der Verbindungstest gegen den Fake-IMAP-Server aus 1.1 ist erfolgreich; falsche Zugangsdaten,
  unerreichbarer Host und fehlender Ordner erzeugen je eine eigene Fehlermeldung.
- Ein zweiter Entry wird abgewiesen.
- Änderung an Options oder Subentry löst genau einen Reload aus.
- Löschen einer Adresse ordnet deren Sendungen „Unbekannt" zu.
- Alle im Flow verwendeten Übersetzungsschlüssel existieren in `de.json` und `en.json`
  (Test, der `strings.json` gegen beide Übersetzungsdateien abgleicht).

## Checkliste

- [x] Haupt-Entry mit Einmaligkeitsprüfung
- [x] Subentry mailbox mit Verbindungstest und Ordnerauswahl
- [x] Subentry destination mit Match-Regeln und eigenem Device
- [x] Subentry api_source als Gerüst
- [x] reconfigure für alle drei Typen
- [x] Options-Flow: Retention, Carrier, generischer Fallback
- [x] Reload bei Änderungen
- [x] Übersetzungen de/en vollständig
- [x] Tests grün
