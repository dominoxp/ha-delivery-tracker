# Step 5.1 — Dokumentation, Dashboard-Beispiele & HACS-Veröffentlichung

> Kontext: `planning.md` lesen. Setzt Step 4.2 voraus.

## Ziel

Das Projekt ist für Fremde installierbar und erweiterbar — und die eigene Erweiterung um einen
neuen Zusteller ist in einer halben Stunde machbar, ohne den Code zu durchsuchen.

## Scope

**Gehört dazu:**
- README mit Zweck, Funktionsumfang, unterstützten Zustellern, Installation über HACS,
  Einrichtung (Postfach, Zieladressen, API-Quelle) und einer Übersicht aller Entities,
  Actions und Events.
- Dashboard-Beispiele zum Kopieren:
  - die eigene Karte aus 4.2 in typischen Konfigurationen (Gesamtliste, nach Adresse gefiltert),
  - eine Markdown-Karte über das `shipments`-Attribut für alle ohne die Custom-Karte,
  - ein trigger-basierter Template-Sensor über `get_shipments` mit Filtern,
  - eine Beispiel-Automation auf `delivery_tracker_shipment_updated` („Paket kommt heute").
- Anleitung „Neuen Zusteller hinzufügen": Ablauf von der verworfenen Mail über `test_parser`
  aus 2.5 bis zur fertigen `rules.yaml`, mit dem kommentierten Beispiel-Plugin aus 0.3
  als Ausgangspunkt und einem Hinweis auf die Fixture-Konvention in `tests/`.
- Anleitung „Warum wird meine Mail nicht erkannt?" anhand der Diagnose-Kennzahlen aus 2.6.
- `CONTRIBUTING.md` mit Hinweis auf `check.cmd` als einzigem Qualitäts-Gate und auf die
  Anonymisierungspflicht für beigesteuerte `.eml`-Fixtures.
- Version 1.0.0 taggen, Release-Workflow aus 0.1 durchlaufen lassen und das ZIP prüfen.
- Einreichung im HACS-Standardverzeichnis vorbereiten: HACS-Validierung grün, Repository-Themen
  und -Beschreibung gesetzt, Anforderungen an das Repository abgehakt.

**Gehört NICHT dazu:**
- Neue Funktionen; alles Weitere steht im Backlog von `planning.md`.

## Hinweise

- Die Dashboard-Beispiele sind das, was in einem Forenbeitrag zuerst kopiert wird — sie müssen
  unverändert funktionieren. Jedes Beispiel vor dem Einchecken in einer echten HA-Instanz testen.
- Der Ablauf „neuen Carrier hinzufügen" ist der eigentliche Wert des modularen Aufbaus. Er wird
  am besten geschrieben, indem man ihn einmal selbst durchläuft und dabei mitschreibt — idealer
  Kandidat ist ein Zusteller, der bewusst nicht in v1 enthalten ist.
- Im README steht auch, was die Integration bewusst **nicht** tut (keine Entity pro Sendung) und
  warum — sonst kommt die Frage in jeder Ausgabe erneut.
- Screenshots im README erst nach 4.2 erstellen und dabei auf echte Adressen und
  Trackingnummern achten.

## Akzeptanzkriterien

- Eine frische HA-Instanz lässt sich allein anhand des README einrichten, bis Sendungen auf
  einem Dashboard erscheinen.
- Jedes Dashboard-Beispiel funktioniert unverändert kopiert.
- Ein bislang nicht unterstützter Zusteller lässt sich allein anhand der Anleitung ergänzen.
- Das Release-ZIP enthält die Integration und die gebaute Karte, aber nicht `setup-docs/`,
  `tests/` oder `card/`-Quellen.
- HACS-Validierung und hassfest laufen auf dem Release-Tag grün.
- Alle Checklisten in allen Step-Dateien sind abgehakt.

## Checkliste

- [ ] README mit Einrichtung, Entities, Actions, Events
- [ ] Vier Dashboard-Beispiele, alle in echter HA getestet
- [ ] Anleitung „Neuen Zusteller hinzufügen", einmal selbst durchlaufen
- [ ] Anleitung zur Fehlersuche bei nicht erkannten Mails
- [ ] CONTRIBUTING.md
- [ ] Screenshots ohne echte Adressen und Trackingnummern
- [ ] Version 1.0.0 getaggt, Release-ZIP geprüft
- [ ] HACS-Einreichung vorbereitet
