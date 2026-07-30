# Step 0.3 — Quellen- und Carrier-Plugin-Registry

> Kontext: `planning.md` lesen. Setzt Step 0.2 voraus.

## Ziel

Der modulare Kern steht: Neue Zusteller und neue Datenquellen lassen sich hinzufügen, ohne
irgendeine bestehende Datei anzufassen.

## Scope

**Gehört dazu:**
- `sources/base.py`: `TrackingSource`-ABC mit `async_setup()`, `async_fetch()` (liefert eine
  Liste von `Shipment`-Fragmenten), `async_close()`, dazu Quellen-ID und Priorität für das
  Merging aus 0.2.
- `carriers/base.py`: `CarrierPlugin`-ABC mit `matches(mail) -> int` (Trefferbewertung statt
  nur ja/nein, damit bei mehreren Kandidaten der beste gewinnt), `parse(mail) -> Shipment | None`,
  `tracking_url(nummer)`, Carrier-ID und Anzeigename.
- `carriers/__init__.py`: Registry mit Auto-Discovery über das Paketverzeichnis — jedes
  Unterverzeichnis mit `__init__.py` wird beim Start geladen und registriert sich selbst.
  Import- oder Ladefehler eines einzelnen Plugins werden geloggt und übersprungen.
- `rules.yaml`-Loader: Laden neben dem Plugin-Modul, Validierung gegen ein voluptuous-Schema
  (Absender-Domains, Betreff-Muster, Trackingnummer-Muster, Status-Zuordnung Text → Enum,
  Datums-Muster, Tracking-URL-Vorlage), Regex werden einmalig vorkompiliert.
- Basis-Hilfen, die alle Plugins teilen: Regelanwendung auf eine Mail, Status-Mapping,
  Datumsinterpretation (deutsche Formate, „morgen", „Montag, 28.07.").
- Ein Beispiel-Plugin `carriers/example/` mit `rules.yaml` als lebende Vorlage.

**Gehört NICHT dazu:**
- Echte Carrier-Regeln (1.3/1.4), IMAP (1.1), 17track (3.1).

## Hinweise

- Die Registry muss auch dann funktionieren, wenn HA die Integration mehrfach lädt —
  Discovery genau einmal ausführen und das Ergebnis cachen.
- Auto-Discovery über das Dateisystem läuft blockierend; im Executor ausführen, damit der
  Event-Loop nicht blockiert.
- Das `rules.yaml`-Schema ist die Nutzerschnittstelle zum Erweitern — jedes Feld bekommt einen
  Kommentar in der Beispieldatei. Ungültige Regeln müssen mit einer klaren Fehlermeldung
  scheitern, nicht still ignoriert werden.
- `matches()` bewertet, `parse()` extrahiert: Ein Plugin darf in `parse()` davon ausgehen,
  dass die Mail zu ihm gehört.

## Akzeptanzkriterien

- Ein Test legt ein Plugin in einem temporären Verzeichnis an; die Registry findet und lädt es.
- Ein Plugin mit kaputtem Import bringt die Registry nicht zum Absturz, wird aber geloggt.
- Der Loader lehnt eine `rules.yaml` mit unbekanntem Feld und mit ungültigem Regex jeweils mit
  aussagekräftiger Fehlermeldung ab.
- Bei zwei passenden Plugins gewinnt das mit der höheren Bewertung.
- Status-Mapping und deutsche Datumsinterpretation sind eigenständig getestet.

## Checkliste

- [x] TrackingSource-ABC mit Priorität
- [x] CarrierPlugin-ABC mit bewertendem matches()
- [x] Registry mit Auto-Discovery und Fehlerisolierung
- [x] rules.yaml-Schema, Loader und kommentierte Beispieldatei
- [x] Geteilte Hilfen für Regelanwendung, Status-Mapping und Datumsinterpretation
- [x] Tests grün
