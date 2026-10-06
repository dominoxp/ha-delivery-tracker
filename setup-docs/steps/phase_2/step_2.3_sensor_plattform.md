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

## Nachtrag aus der Umsetzung (Oktober 2026)

- **Keys englisch:** Die `translation_key`s/`unique_id`-Suffixe folgen der Regel „Identifier
  Englisch": `active_shipments`, `expected_today`, `out_for_delivery`, `ready_for_pickup`,
  `delivered_today`, `problems`. Die deutschen Namen kommen aus `de.json`. Entity-IDs bildet HA
  aus der englischen Übersetzung (`sensor.delivery_tracker_active_shipments`).
- **unique_ids:** `{entry_id}_{key}`, `{entry_id}_carrier_{carrier_id}`,
  `{entry_id}_destination_{destination.id}`, `{entry_id}_last_update`, `{entry_id}_sources`.
  Die Zielort-ID stammt aus dem Subentry und bleibt beim Umbenennen gleich.
- **Devices:** Haupt-Device `(DOMAIN, entry_id)` (`entry.hub_device_identifier`), Zielort-Sensoren
  über `config_subentry_id` am Device des Zielorts. Wird ein Subentry gelöscht, räumt HA Device und
  Sensor ab.
- **Carrier-Sensoren:** je Plugin der gefilterten Registry (neu in `runtime_data.registry`), also
  auch `generic`, solange der Fallback an ist. Name = Plugin-Name über den Platzhalter `{carrier}`.
  Beim Setup entfernt `_async_remove_stale_entities` Hub-Sensoren aus der Registry, die es nicht
  mehr gibt. Ein abgewählter Carrier verschwindet dadurch, statt als „nicht verfügbar"
  stehen zu bleiben.
- **Attribut `shipments`:** `as_summary()` je Sendung. Die Sortierung ist `shipment_sort_key` aus
  2.2: mit Termin nach `eta_start` aufsteigend, danach ohne Termin nach `last_updated`
  absteigend, ID als Tiebreak. `_unrecorded_attributes` der Basisklasse nimmt `shipments` und
  `sources` aus.
- **Diagnose:** `last_update` (Timestamp = `CoordinatorData.updated_at`), `sources` (ENUM mit
  den `SourceState`-Werten, Zustand = schlechteste Quelle, ohne Quellen `unknown`; Attribut
  `sources` mit `SourceStatus.as_dict()` je Quelle).
- **Tageswechsel:** `expected_today` und `delivered_today` hängen an `async_track_time_change`
  um 00:00:00 Ortszeit und schreiben ihren Zustand neu. Der Tag wird bei jedem Schreiben aus
  `dt_util.now()` bestimmt.
- **Icons** in `icons.json`, inklusive Zustands-Icons für den Quellen-Sensor.
- **Recorder-Test** liegt in `tests/test_sensor_recorder.py`: Der Recorder braucht seine DB vor
  `hass`, das autouse-Fixture aus `conftest.py` zieht `hass` aber vorher. Das Modul überschreibt
  das Fixture deshalb mit `recorder_db_url` als erster Abhängigkeit.

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

- [x] Sechs Zähler-Sensoren am Haupt-Device
- [x] Sensor je Carrier und je Zieladresse (an deren Device)
- [x] Diagnose-Sensoren für Aktualisierung und Verbindungszustand
- [x] shipments-Attribut aus as_summary(), definiert sortiert
- [x] _unrecorded_attributes gesetzt und getestet
- [x] Stabile unique_ids über Reload und Umbenennung
- [x] Aktualisierung zum Tageswechsel
- [x] Übersetzungen de/en, Tests grün
