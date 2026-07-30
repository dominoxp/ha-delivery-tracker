# Step 0.2 — Datenmodell, Persistenz & Merging

> Kontext: `planning.md` lesen. Setzt Step 0.1 voraus.

## Ziel

Das zentrale Sendungs-Datenmodell steht, überlebt HA-Neustarts und kann Informationen aus
mehreren Quellen zur selben Sendung widerspruchsfrei zusammenführen.

## Scope

**Gehört dazu:**
- `models.py`: `ShipmentStatus` als Enum in der in `planning.md` festgelegten Reihenfolge
  (die Reihenfolge ist zugleich die Fortschritts-Rangfolge), `ShipmentEvent`
  (Zeitstempel, Status, Text, Ort), `SourceRecord` (Quellen-ID, Zeitpunkt, Rohdaten-Referenz),
  `Shipment` mit allen in `planning.md` genannten Feldern, `Destination`.
- Serialisierung: `to_dict`/`from_dict` je Modell, mit `version`-Feld für spätere Migrationen.
  Zeitstempel konsequent timezone-aware.
- `store.py`: Persistenz über `homeassistant.helpers.storage.Store`, verzögertes Schreiben
  (`async_delay_save`), Zugriffsmethoden (alle Sendungen, nach ID, upsert, löschen) und
  Retention-Bereinigung, die zugestellte Sendungen nach N Tagen entfernt.
- `merge.py`: Bildung der stabilen Sendungs-ID, Zusammenführen zweier `Shipment`-Objekte
  (feldweise, frischere und höherwertige Quelle gewinnt), Zusammenführen und Deduplizieren
  der Event-Listen, Erkennung „hat sich der Status geändert" für die späteren HA-Events.
- Kompakte Dashboard-Repräsentation (`Shipment.as_summary()`) ohne Event-Historie —
  wird in 2.3 zum Sensor-Attribut.

**Gehört NICHT dazu:**
- Trackingnummer-Normalisierung und Prüfziffern (1.2), Adress-Zuordnung (1.5),
  Anbindung an Coordinator und Entities (Phase 2).

## Hinweise

- Der Status darf nicht zurückfallen: Wenn eine veraltete Mail nach einer Live-API-Abfrage
  eintrifft, bleibt der weiter fortgeschrittene Status stehen. Ausnahme sind Rückschritte,
  die fachlich echt sind (`returned`, `delivery_failed`, `delayed`) — diese gewinnen immer.
- Felder, die nur eine Quelle kennt (Händler, Produktbeschreibung aus der Mail), dürfen von
  einer Quelle ohne dieses Feld nie überschrieben werden.
- Sendungen ohne Trackingnummer (z.B. reine Bestellbestätigung) brauchen trotzdem eine stabile
  ID — Ableitung aus Händler + Bestellnummer, damit ein späterer Versand darauf einrastet.
- Store-Größe im Blick behalten: Event-Historie je Sendung deckeln.

## Akzeptanzkriterien

- Roundtrip-Test: `Shipment` → `to_dict` → `from_dict` ergibt ein identisches Objekt,
  inklusive Zeitzonen und leerer Listen.
- Merge-Tests decken ab: API überschreibt Mail-Status, Mail-Kontextfelder überleben,
  Status-Rückschritt wird verworfen, `returned` gewinnt trotzdem, Events werden dedupliziert.
- Retention-Test: zugestellte Sendung älter als N Tage verschwindet, eine aktive bleibt.
- Store-Test gegen die `hass`-Fixture: Schreiben, HA-Neustart simulieren, Lesen.

## Checkliste

- [x] models.py mit Enum, Shipment, Event, Destination, Serialisierung
- [x] store.py mit Store-Anbindung und Retention
- [x] merge.py mit ID-Bildung, Feld- und Event-Merge, Statuswechsel-Erkennung
- [x] as_summary() für die kompakte Attributliste
- [x] Tests für Roundtrip, Merge-Regeln, Retention und Persistenz grün
