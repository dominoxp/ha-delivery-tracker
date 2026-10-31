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

## Sendungen verwalten

Unter **Einstellungen → Geräte & Dienste → Delivery Tracker → Konfigurieren** gibt es neben
den Einstellungen zwei weitere Punkte:

- **Sendungen ansehen und verwalten:** alle gespeicherten Sendungen, die mit der ältesten
  Mail zuerst. Jede Zeile nennt Zusteller, Status, Artikel, Zielort, wann die letzte Mail kam
  und wann die Sendung von selbst verschwindet. Markierte Sendungen lassen sich löschen oder
  als zugestellt markieren. Die Details einer Sendung zeigen alles Bekannte, warum sie noch da
  ist, ihren Statusverlauf und die Mails (Betreff, Absender), aus denen sie stammt.
  Aus der erkannten Adresse oder dem Abholort lässt sich direkt ein Zielort anlegen oder ein
  bestehender ergänzen — vorbefüllt, änderbar, mit Vorschau, welche Sendungen er bekommt.
  Ausgewählte Sendungen werden ihm fest zugeordnet.
- **Letzte Mails prüfen:** die letzten 100 verarbeiteten Mails je Postfach mit Ergebnis
  (Sendung erkannt, Zusteller erkannt aber keine Sendung, kein Zusteller, Fehler). Damit
  fallen Mails auf, die nicht erkannt wurden, etwa eine Zustellmail in neuem Format.

Betreffs und Absender bleiben lokal im Store; die Diagnose-Datei enthält sie nicht.

## Lovelace-Karte

Die Integration bringt ihre Karte mit, liefert sie selbst aus und trägt sie beim Start als
Dashboard-Ressource ein — eine eigene HACS-Installation ist nicht nötig. Nach dem Neustart
von Home Assistant (und einem Neuladen der Seite im Browser) steht **Delivery Tracker** in
der Kartenauswahl des Dashboards. Wird die Integration gelöscht, entfernt sie auch die
Ressource wieder.

```yaml
type: custom:delivery-tracker-card
entity: sensor.delivery_tracker_aktive_sendungen # ein Sensor der Integration
title: Pakete # optional, sonst der Name der Entity
mark_delivered: true # optional: Button „Als zugestellt / abgeholt markieren"
```

`entity` muss ein Sensor der Integration sein, also einer mit dem Attribut `shipments`.
Bei ungültiger Konfiguration zeigt die Karte statt einer leeren Fläche eine Fehlermeldung.

Mit `mark_delivered: true` zeigt die Karte in den aufgeklappten Details jeder noch nicht
zugestellten Sendung neben „Sendungsverfolgung öffnen" einen Button, der die Sendung nach
einer Rückfrage über die Action `delivery_tracker.mark_delivered` als zugestellt markiert —
etwa wenn ein Paketshop die Abholung nicht meldet. Bei abholbereiten Sendungen heißt er
„Als abgeholt markieren".

Die Karte wird unter `/delivery_tracker/delivery-tracker-card.js` ausgeliefert. Ein von Hand
angelegter Ressourcen-Eintrag für diese Datei wird übernommen statt verdoppelt.

Werden die Ressourcen per YAML verwaltet (`lovelace: resource_mode: yaml`), kann die
Integration nichts eintragen. Dann die Karte selbst eintragen:

```yaml
lovelace:
  resources:
    - url: /delivery_tracker/delivery-tracker-card.js
      type: module
```

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

Zum Entwickeln startet `npm run dev` eine Vorschau mit Beispieldaten im Browser
([card/dev/](card/dev/)): alle Varianten der Karte, Leer- und Fehlerzustände, der
visuelle Editor und ein Umschalter für helles und dunkles Theme. Sie bindet die Quellen
direkt ein und lädt bei jeder Änderung neu; zum Build gehört sie nicht.

**Tests laufen im Docker-Container.** Home Assistant importiert seit 2025.10 die
Unix-Module `fcntl` und `resource` und lässt sich deshalb unter Windows nicht mehr
importieren. `check.cmd` baut dafür das Image aus [Dockerfile.test](Dockerfile.test)
(nur beim ersten Mal langsam, danach aus dem Layer-Cache) und startet `pytest` darin —
also auf derselben Plattform wie die GitHub-CI. Docker Desktop muss laufen.
`ruff` läuft weiterhin nativ unter Windows aus dem `.venv`.

## Lizenz

MIT
