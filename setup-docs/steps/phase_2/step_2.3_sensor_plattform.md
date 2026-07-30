# Step 2.3 — Sensor-Plattform

> Kontext: `planning.md` lesen. Setzt Step 2.2 voraus.

## Ziel

Die Sendungen sind in HA sichtbar und auf einem Dashboard nutzbar — über feste Entities, die
nie verschwinden, und eine Attributliste, die sich direkt in Karten einsetzen lässt.

## Scope

**Gehört dazu:**
- `sensor.py` mit den in `planning.md` festgelegten Zähler-Sensoren am Haupt-Device:
  `aktive_sendungen`, `heute_erwartet`, `in_zustellung`, `abholbereit`, `heute_zugestellt`,
  `problem` (verspätet, gescheitert, retour zusammengefasst).
- Je aktiviertem Carrier ein Sensor mit der Anzahl aktiver Sendungen dieses Zustellers.
- Je `destination`-Subentry ein Sensor, angelegt am Device der jeweiligen Adresse.
- Diagnose-Sensor `letzte_aktualisierung` (Zeitstempel) sowie ein Sensor mit dem
  Verbindungszustand der Quellen aus 2.2, beide als `EntityCategory.DIAGNOSTIC`.
- Jeder Zähler trägt seine zugehörigen Sendungen als Attribut `shipments` — die kompakte
  Repräsentation aus `as_summary()` (0.2), sortiert nach erwarteter Zustellung.
- `_unrecorded_attributes = frozenset({"shipments"})` auf der Entity-Basisklasse, damit die
  Liste nicht bei jeder Änderung in die Recorder-Datenbank geschrieben wird.
- Stabile `unique_id`s, die einen Reload und das Umbenennen einer Adresse überstehen.
- Icons und `translation_key` je Sensor, Übersetzungen in `de.json` und `en.json`.

**Gehört NICHT dazu:**
- Kalender (2.4), Actions und Events (2.5), eigene Karte (Phase 4).

## Hinweise

- Der Verzicht auf Entities pro Sendung ist eine bewusste Entscheidung aus `planning.md` und
  wird hier nicht aufgeweicht — auch nicht „nur für die wichtigen Sendungen".
- Die Attributliste muss kompakt bleiben: keine Event-Historie, keine Rohtexte. HA warnt bei
  sehr großen Attributen, und jede Karte muss die Liste durchreichen.
- Wird ein Carrier in den Options abgewählt, soll sein Sensor verschwinden statt auf 0 stehen
  zu bleiben; entsprechend beim Wiedereinschalten zurückkehren.
- Zeitbezogene Zähler („heute") müssen zum Tageswechsel aktualisieren, auch ohne neue Mail.
- Sortierung im Attribut festlegen und dokumentieren — die Karte in 4.2 verlässt sich darauf.

## Akzeptanzkriterien

- Mit Testdaten stehen alle Zähler auf den erwarteten Werten.
- Das Attribut `shipments` enthält genau die zum Zähler passenden Sendungen, in definierter
  Sortierung, ohne Event-Historie.
- Ein Recorder-Test belegt, dass `shipments` nicht in die Datenbank geschrieben wird.
- Anlegen einer neuen Adresse erzeugt einen Sensor am zugehörigen Device; Löschen entfernt ihn.
- Abwählen eines Carriers entfernt dessen Sensor, Wiedereinschalten stellt ihn mit derselben
  `unique_id` wieder her.
- Tageswechsel aktualisiert `heute_erwartet` und `heute_zugestellt`.
- Alle `translation_key`s sind in beiden Sprachdateien vorhanden.

## Checkliste

- [ ] Sechs Zähler-Sensoren am Haupt-Device
- [ ] Sensor je Carrier und je Zieladresse (an deren Device)
- [ ] Diagnose-Sensoren für Aktualisierung und Verbindungszustand
- [ ] shipments-Attribut aus as_summary(), definiert sortiert
- [ ] _unrecorded_attributes gesetzt und getestet
- [ ] Stabile unique_ids über Reload und Umbenennung
- [ ] Aktualisierung zum Tageswechsel
- [ ] Übersetzungen de/en, Tests grün
