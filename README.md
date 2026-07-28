# Delivery Tracker

Home-Assistant-Custom-Integration, die Paket-Benachrichtigungen aus einem IMAP-Postfach
ausliest, den Zusteller erkennt und den Sendungsstand in Home Assistant bereitstellt.

> **Status:** in Entwicklung. Aktuell steht nur das Grundgerüst (Step 0.1) — die Integration
> lässt sich anlegen und wieder entfernen, liefert aber noch keine Daten.

## Geplanter Funktionsumfang

- **E-Mail-Quelle:** IMAP mit IDLE-Push (Polling als Fallback), Carrier-Erkennung über
  Absender, Betreff und Trackingnummer.
- **Zusteller:** DHL/Deutsche Post, Amazon, DPD, GLS, Hermes, UPS sowie ein generischer
  Fallback — jeder Zusteller ist ein Plugin.
- **API-Quellen:** modular zuschaltbar (17track als generischer Fallback).
- **In Home Assistant:** feste Zähler-Sensoren mit kompakter Sendungsliste als Attribut,
  eine Action `delivery_tracker.get_shipments` mit vollen Details, eine Kalender-Entity für
  erwartete Lieferungen und eine mitgelieferte Lovelace-Karte.

## Installation über HACS

1. In HACS auf **Integrationen → ⋮ → Benutzerdefinierte Repositories** gehen.
2. `https://github.com/dominoxp/ha-delivery-tracker` als Kategorie _Integration_ eintragen.
3. „Delivery Tracker" installieren und Home Assistant neu starten.
4. Unter **Einstellungen → Geräte & Dienste → Integration hinzufügen** nach „Delivery Tracker" suchen.

## Manuelle Installation

Den Ordner `custom_components/delivery_tracker` in das HA-Konfigurationsverzeichnis unter
`custom_components/` kopieren und Home Assistant neu starten.

## Entwicklung

```cmd
uv venv --python 3.14
uv pip install -e ".[dev]"
check.cmd
```

`check.cmd` ist das einzige Qualitäts-Gate: `ruff format` → `ruff check` → `pytest`.
Mit `check.cmd --fix` werden zusätzlich behebbare Lint-Fehler korrigiert.

**Tests laufen im Docker-Container.** Home Assistant importiert seit 2025.10 die
Unix-Module `fcntl` und `resource` und lässt sich deshalb unter Windows nicht mehr
importieren. `check.cmd` baut dafür das Image aus [Dockerfile.test](Dockerfile.test)
(nur beim ersten Mal langsam, danach aus dem Layer-Cache) und startet `pytest` darin —
also auf derselben Plattform wie die GitHub-CI. Docker Desktop muss laufen.
`ruff` läuft weiterhin nativ unter Windows aus dem `.venv`.

## Lizenz

MIT
