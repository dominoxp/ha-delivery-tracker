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

- [ ] Haupt-Entry mit Einmaligkeitsprüfung
- [ ] Subentry mailbox mit Verbindungstest und Ordnerauswahl
- [ ] Subentry destination mit Match-Regeln und eigenem Device
- [ ] Subentry api_source als Gerüst
- [ ] reconfigure für alle drei Typen
- [ ] Options-Flow: Retention, Carrier, generischer Fallback
- [ ] Reload bei Änderungen
- [ ] Übersetzungen de/en vollständig
- [ ] Tests grün
