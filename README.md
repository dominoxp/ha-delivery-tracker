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

Das ZIP aus dem neuesten [Release](https://github.com/dominoxp/ha-delivery-tracker/releases)
nach `custom_components/delivery_tracker/` im HA-Konfigurationsverzeichnis entpacken und
Home Assistant neu starten. Das Release enthält die fertig gebaute Lovelace-Karte; wer
stattdessen den Ordner aus einem Git-Checkout kopiert, muss die Karte vorher selbst bauen
(siehe [Entwicklung](#entwicklung)).

## Lovelace-Karte

Die Integration bringt ihre Karte mit und liefert sie selbst aus — weder eine eigene
HACS-Installation noch ein Eintrag unter den Dashboard-Ressourcen ist nötig. Nach dem
Neustart von Home Assistant (und einem Neuladen der Seite im Browser) steht
**Delivery Tracker** in der Kartenauswahl des Dashboards.

```yaml
type: custom:delivery-tracker-card
entity: sensor.delivery_tracker_aktive_sendungen # ein Sensor der Integration
title: Pakete # optional, sonst der Name der Entity
```

`entity` muss ein Sensor der Integration sein, also einer mit dem Attribut `shipments`.
Bei ungültiger Konfiguration zeigt die Karte statt einer leeren Fläche eine Fehlermeldung.

Die Karte wird unter `/delivery_tracker/delivery-tracker-card.js` ausgeliefert. Wer sie
trotzdem zusätzlich als Ressource einträgt, richtet keinen Schaden an — sie registriert
sich nur einmal.

## Entwicklung

```cmd
uv venv --python 3.14
uv pip install -e ".[dev]"
check.cmd
```

`check.cmd` ist das einzige Qualitäts-Gate: `ruff format` → `ruff check` → `pytest` →
Karte (`prettier` → `eslint` → `vitest` → `vite build`). Mit `check.cmd --fix` werden
zusätzlich behebbare Lint-Fehler korrigiert.

**Karte:** Die Node-Toolchain liegt vollständig unter [card/](card/) (TypeScript, Lit,
Vite; Node 24). `check.cmd` installiert die Abhängigkeiten beim ersten Lauf selbst und
baut die Karte nach `custom_components/delivery_tracker/frontend/` — dieser Ordner ist
nicht eingecheckt, das Release baut ihn neu. Ohne installiertes Node überspringt
`check.cmd` die Karte mit einem Hinweis, die Python-Prüfungen laufen trotzdem. Einzeln:

```cmd
cd card
npm ci
npm run lint
npm test
npm run build
```

**Tests laufen im Docker-Container.** Home Assistant importiert seit 2025.10 die
Unix-Module `fcntl` und `resource` und lässt sich deshalb unter Windows nicht mehr
importieren. `check.cmd` baut dafür das Image aus [Dockerfile.test](Dockerfile.test)
(nur beim ersten Mal langsam, danach aus dem Layer-Cache) und startet `pytest` darin —
also auf derselben Plattform wie die GitHub-CI. Docker Desktop muss laufen.
`ruff` läuft weiterhin nativ unter Windows aus dem `.venv`.

## Lizenz

MIT
