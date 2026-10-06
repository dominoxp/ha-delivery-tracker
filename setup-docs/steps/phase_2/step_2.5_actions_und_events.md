# Step 2.5 — Actions & Events

> Kontext: `planning.md` lesen. Setzt Step 2.4 voraus.

## Ziel

Der Detailzugriff und die Automationsschnittstelle stehen: Sendungen lassen sich gefiltert
abfragen, manuell pflegen, und jede Statusänderung ist als Ereignis auslösbar.

## Scope

**Gehört dazu:**
- `services.yaml` mit Feldbeschreibungen und Selektoren für alle Actions, dazu Übersetzungen.
- `delivery_tracker.get_shipments` — **mit Response-Daten**, der zentrale Weg zu allen Details.
  Filter: Status (Mehrfachauswahl), Carrier, Zieladresse, `include_events`. Ohne Filter alle
  aktiven Sendungen. Antwort enthält die vollständigen Sendungen inklusive Event-Historie,
  wenn angefordert.
- `delivery_tracker.add_shipment` — Trackingnummer, optional Carrier (sonst aus dem
  Nummernformat abgeleitet), Zieladresse, Beschreibung. Legt eine Sendung an, die von einer
  später eintreffenden Mail über das Merging aus 0.2 ergänzt wird.
- `delivery_tracker.remove_shipment` und `delivery_tracker.mark_delivered` über die Sendungs-ID.
- `delivery_tracker.refresh` — sofortige Aktualisierung, optional auf eine Quelle beschränkt.
- `delivery_tracker.test_parser` — **mit Response-Daten**: nimmt Betreff, Absender und Mailtext
  entgegen und liefert zurück, welche Plugins wie bewertet haben, welches gewonnen hätte und
  welche Felder extrahiert wurden. Das Werkzeug zum Bauen neuer Carrier-Regeln.
- Events auf dem HA-Bus: `delivery_tracker_shipment_added`,
  `delivery_tracker_shipment_updated` (mit `old_status` und `new_status`),
  `delivery_tracker_shipment_delivered`. Payload enthält die kompakte Sendungsdarstellung.
- Registrierung der Actions genau einmal pro HA-Start, nicht pro Entry.

**Gehört NICHT dazu:**
- 17track (3.1), Karte (Phase 4).

## Hinweise

- `get_shipments` ist der Ersatz für die bewusst nicht gebauten Einzel-Entities. Sie muss ohne
  Vorwissen bedienbar sein und in einem trigger-basierten Template-Sensor funktionieren —
  genau dieses Beispiel kommt in 5.1 ins README.
- Response-liefernde Actions müssen mit `supports_response` registriert werden, sonst weist HA
  den Aufruf mit `response_variable` ab.
- `add_shipment` darf keine Sendung mit ungültiger Trackingnummer anlegen — Validierung aus 1.2
  wiederverwenden und bei Ablehnung eine verständliche Fehlermeldung werfen.
- Events feuern nur bei echten Änderungen, nicht bei jedem Abrufzyklus, sonst wird jede
  Automation unbrauchbar. Die Statuswechsel-Erkennung aus 2.2 ist die Quelle.
- `test_parser` bekommt einen Rohtext, keine Datei — Zugriff auf das Dateisystem ist hier
  weder nötig noch erwünscht.

## Akzeptanzkriterien

- `get_shipments` ohne Filter liefert alle aktiven Sendungen; mit Filter auf Adresse und Status
  genau die passenden. `include_events` steuert die Historie sichtbar.
- Ein trigger-basierter Template-Sensor, der `get_shipments` aufruft, funktioniert im Test
  gegen die `hass`-Fixture.
- `add_shipment` mit gültiger Nummer legt an, mit ungültiger wird abgelehnt. Eine später
  eintreffende Mail ergänzt dieselbe Sendung statt eine zweite zu erzeugen.
- `mark_delivered` und `remove_shipment` wirken sofort auf die Sensoren aus 2.3.
- `test_parser` liefert für eine DHL-Fixture das DHL-Plugin als Gewinner samt extrahierter Felder.
- Statuswechsel feuert genau ein `_updated`-Event mit korrektem `old_status`/`new_status`;
  ein Abruf ohne Änderung feuert keines.
- Zwei Entries oder ein Reload führen nicht zu doppelt registrierten Actions.

## Checkliste

- [x] services.yaml mit Selektoren und Übersetzungen
- [x] get_shipments mit Filtern und Response
- [x] add_shipment mit Validierung, remove_shipment, mark_delivered, refresh
- [x] test_parser mit Bewertungs- und Feldausgabe
- [x] Drei Events, nur bei echten Änderungen
- [x] Einmalige Action-Registrierung
- [x] Tests grün, inklusive Template-Sensor-Beispiel
