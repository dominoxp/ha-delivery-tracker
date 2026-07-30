# HA Delivery Tracker — Planungsübersicht

Home-Assistant-Custom-Integration (über HACS installierbar), die Paket-Benachrichtigungen
aus einem IMAP-Postfach ausliest, den Zusteller erkennt und den Sendungsstand in HA
bereitstellt:

- **E-Mail-Quelle:** IMAP mit IDLE-Push (Polling als Fallback), Carrier-Erkennung über
  Absender/Betreff/Trackingnummer, Extraktion von Status, Zeitfenster, Absender/Händler,
  Abholort und Zieladresse.
- **API-Quellen:** modular zuschaltbar (17track als generischer Fallback, später weitere).
  Trackingnummer + Carrier sind der Schlüssel, über den E-Mail- und API-Daten zu einer
  Sendung zusammengeführt werden.
- **HA-Oberfläche:** feste Zähler-Sensoren mit kompakter Sendungsliste als Attribut, eine
  Action `get_shipments` mit vollen Details, eine Kalender-Entity für erwartete Lieferungen
  und eine mitgelieferte Lovelace-Karte.

Ziel ist ein **modularer Aufbau**: Jeder Zusteller ist ein Plugin, jede Datenquelle
implementiert dasselbe Interface — neue Carrier und APIs kommen ohne Eingriff in den Kern dazu.

## Getroffene Design-Entscheidungen (nicht neu diskutieren)

| Thema             | Entscheidung                                                                                                                                         |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| Dashboard-Modell  | Feste Zähler-Sensoren mit `shipments`-Attributliste + Action `get_shipments` + Kalender-Entity. **Keine dynamischen Entities pro Sendung**            |
| Eigene Karte      | Lovelace-Card im selben Repo (TypeScript + Lit + Vite) — keine Fremdkarten wie auto-entities nötig                                                    |
| Zusteller v1      | DHL/Deutsche Post, Amazon (Bestellung + Logistics), DPD, GLS, Hermes, UPS, generischer Fallback                                                       |
| IMAP-Handling     | Verarbeitete Mails werden mit `\Seen` markiert; IDLE-Push mit Polling-Fallback; `aioimaplib`                                                          |
| Zieladressen      | Manuell konfiguriert mit Match-Regeln (Name, Straße, PLZ, Packstation-Nr., Empfänger-Mailadresse)                                                     |
| Quellen-Merging   | Normalisierte Trackingnummer + Carrier = ID. E-Mail liefert Kontext, API den autoritativen Status; frischere Quelle gewinnt, beide bleiben gespeichert |
| Retention         | Konfigurierbar: 14 Tage nach Zustellung, 60 Tage ohne Aktualisierung (sonst bleiben nie abgeschlossene Sendungen für immer aktiv)                     |
| Parser-Aufbau     | Python-Plugins mit Auto-Discovery + `rules.yaml` je Carrier für die deklarativen Anteile                                                              |
| Konfiguration     | Config-Entry mit Subentries: `mailbox`, `destination`, `api_source` (Add/Edit/Delete-UI und Devices gratis)                                           |
| Tests             | `pytest-homeassistant-custom-component` (echte `hass`-Fixture), Fake-IMAP-Server, `.eml`-Fixtures                                                     |
| Sprache           | Doku und Kommentare Deutsch, Identifier und Dateinamen Englisch                                                                                      |

## Warum keine Entity pro Sendung

HA Core hat die Einzel-Sensoren pro Paket in der `seventeentrack`-Integration **abgeschafft**
und durch eine Action mit Response-Daten ersetzt. Dynamische Entities erzeugen Müll in der
Entity-Registry, blähen den Recorder auf, und Dashboard-Karten brechen, sobald eine Sendung
verschwindet. Stattdessen:

```
Feste Entities (stabil)                    Detailabruf (bei Bedarf)
  sensor.*_aktive_sendungen  ── 4            action: delivery_tracker.get_shipments
    attributes.shipments: [ {...}, ... ]       data: { destination: buero }
  sensor.*_heute_erwartet    ── 2             response: volle Sendungen inkl. Event-Historie
  sensor.*_zuhause           ── 3
  calendar.erwartete_lieferungen
```

Die `shipments`-Attributliste bleibt bewusst kompakt und wird über
`_unrecorded_attributes` vom Recorder ausgenommen — sonst schreibt jede Statusänderung die
komplette Liste in die Datenbank. Die volle Event-Historie gibt es nur über die Action.

## Datenfluss

```
IMAP-Postfach ──▶ ImapSource ──┐
                               ├─▶ Merge (Trackingnr. + Carrier) ──▶ Store ──▶ Coordinator
17track-API  ──▶ ApiSource  ───┘                                                    │
                                                                                    ▼
Carrier-Plugins (dhl, amazon, dpd, gls, hermes, ups, generic)          Sensoren · Kalender
  je Plugin: rules.yaml (Domains, Regex, Status-Map) + Python-Logik     Actions · Events
```

- Jede Sendung bekommt die stabile ID `{carrier}_{normalisierte_trackingnummer}`.
- E-Mail liefert Kontext (Händler, Produkt, Zieladresse), die API den autoritativen Status.
- Statuswechsel feuern HA-Events, auf die Automationen reagieren können.

## Architektur in Kürze

```
ha-delivery-tracker/
├── check.cmd                    # einziges Qualitäts-Gate
├── pyproject.toml  hacs.json
├── custom_components/delivery_tracker/
│   ├── __init__.py              # Setup/Unload, runtime_data, Subentries
│   ├── config_flow.py           # Entry + Subentries: mailbox / destination / api_source
│   ├── coordinator.py           # DataUpdateCoordinator über alle Quellen
│   ├── models.py                # Shipment, ShipmentEvent, Destination, ShipmentStatus
│   ├── store.py  merge.py       # Persistenz + Retention, Dedup + Quellen-Priorisierung
│   ├── mail.py  addresses.py    # MIME/HTML-Aufbereitung, Zuordnung Sendung → Adresse
│   ├── sources/                 # TrackingSource-ABC: imap_source, seventeentrack_source
│   ├── carriers/                # CarrierPlugin-ABC + Registry (Auto-Discovery)
│   │   └── <carrier>/           # __init__.py (Logik) + rules.yaml (deklarative Regeln)
│   ├── sensor.py  calendar.py   # Plattformen
│   └── services.yaml  diagnostics.py  strings.json  translations/
├── card/                        # TypeScript + Lit + Vite → delivery-tracker-card.js
├── tests/                       # conftest.py, fake_imap.py, fixtures/emails/<carrier>/*.eml
└── .github/workflows/           # hassfest, hacs, tests, release
```

**`Shipment`** ist das Herz des Entwurfs: `id`, `tracking_number`, `carrier`, `status`
(normalisierter Enum: `unknown, ordered, label_created, in_transit, delayed, customs,
out_for_delivery, ready_for_pickup, delivered, delivery_failed, returned`), `status_text`
(Originaltext des Zustellers), `eta_start`/`eta_end`, `delivered_at`, `destination_id` +
`destination_raw`, `sender`, `description`, `items`, `order_number`,
`pickup_location`/`pickup_code`, `tracking_url`, `events`, `sources`, `first_seen`,
`last_updated`.

`items` ist die vollständige Artikelliste, `description` nur der gekürzte Anzeigetext dazu.
Der Unterschied ist kein Luxus: Amazon zählt dieselben Artikel je Mail in anderer Reihenfolge
auf und vergibt gar keine Trackingnummer — erst die vollständige Menge macht die Sendungen
aus Bestell-, Versand- und Abholmail in 3.2 vergleichbar.

**Interfaces:** `TrackingSource` (`async_setup` / `async_fetch` / `async_close`) für Quellen,
`CarrierPlugin` (`matches(mail)` / `parse(mail)` / `tracking_url(nr)`) für Zusteller.
Fehler einer Quelle oder eines Plugins dürfen nie die gesamte Integration lahmlegen.

Tech-Stack: aioimaplib, aiohttp (HA-eigen), voluptuous, PyYAML,
pytest + pytest-homeassistant-custom-component, ruff. Python ≥ 3.13 (HA-Minimum).

## Arbeitsweise mit den Step-Dateien

- Die Umsetzung erfolgt Step für Step in der Reihenfolge unten; jede Datei in `setup-docs/steps/`
  ist zusammen mit dieser `planning.md` eigenständig bearbeitbar.
- Steps setzen die davor liegenden Steps als erledigt voraus.
- Die Dateien geben **Leitplanken, keinen fertigen Code** — Umsetzungsdetails sind Spielraum
  des Bearbeiters, solange Scope und Akzeptanzkriterien eingehalten werden.
- **Jeder Step liefert seine Unit-Tests mit.** Ein Step gilt erst als fertig, wenn die neuen
  Tests grün sind und die bestehenden es bleiben.
- Nach Abschluss eines Steps: Checkliste am Dateiende abhaken (`[x]`), damit der Fortschritt
  zwischen Sessions nachvollziehbar bleibt.
- Vor dem Abschluss eines Steps immer `check.cmd` im Projekt-Root ausführen — das ist der
  einzige Weg, Qualität zu prüfen (`ruff format` → `ruff check` → `pytest`, mit `--fix`
  zusätzlich Lint-Autofixes). Keine einzeln zusammengesetzten Befehle.
- `setup-docs/Home-Assistant-Mail-And-Packages/` ist eine eingecheckte Referenz-Integration.
  Sie dient als **fachliche** Vorlage (welche Mails es gibt, welche Felder interessant sind),
  **nicht** als Code-Vorlage — sie zielt auf HA 0.109 / Python 3.8 und ist technisch veraltet.

## Phasen & Steps

**Phase 0 — Fundament** (`steps/phase_0/`)

- 0.1 Repo-Skeleton, HACS-Metadaten & Qualitäts-Gate
- 0.2 Datenmodell, Persistenz & Merging
- 0.3 Quellen- und Carrier-Plugin-Registry

**Phase 1 — E-Mail-Pipeline** (`steps/phase_1/`)

- 1.1 IMAP-Client mit IDLE-Push und Polling-Fallback
- 1.2 Mail-Vorverarbeitung, Carrier-Erkennung & Trackingnummern
- 1.3 Carrier-Plugins DHL/Deutsche Post & Amazon
- 1.4 Carrier-Plugins DPD, GLS, Hermes, UPS & generischer Fallback
- 1.5 Zieladressen-Zuordnung

**Phase 2 — HA-Integration** (`steps/phase_2/`)

- 2.1 Config-Flow, Subentries & Options
- 2.2 Coordinator & Entry-Setup
- 2.3 Sensor-Plattform
- 2.4 Kalender-Plattform
- 2.5 Actions & Events
- 2.6 Diagnostics & Übersetzungen

**Phase 3 — API-Quellen** (`steps/phase_3/`)

- 3.1 17track als Quelle
- 3.2 Quellen-Priorisierung & Konfliktauflösung

**Phase 4 — Lovelace-Karte** (`steps/phase_4/`)

- 4.1 Card-Toolchain & Grundgerüst
- 4.2 Sendungsliste, Filter & visueller Editor

**Phase 5 — Abschluss** (`steps/phase_5/`)

- 5.1 Dokumentation, Dashboard-Beispiele & HACS-Veröffentlichung

**Später (bewusst offen):** Produkt- und Zustellfotos (Amazon-Bilder, DHL-Zustellnachweis),
weitere Carrier (Post AT/CH, FedEx, PostNL), weitere APIs (DHL Developer, UPS, Parcel App),
Rückversand-/Retouren-Verfolgung, `todo`-Entity als native Listenansicht.

Beim commiten kein Coauthor annotieren
