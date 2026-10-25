# Step 4.3 — Sendungsverwaltung in der Integration

> Kontext: `planning.md` lesen. Setzt Step 4.2 voraus.

## Ziel

Ohne Dashboard und ohne Entwicklerwerkzeuge sehen, welche Sendungen gespeichert sind, warum
eine noch da ist und woher sie kommt — und sie löschen, abhaken oder einem Zielort zuordnen
können. Außerdem erkennen, welche Mails nicht als Sendung erkannt wurden.

## Scope

**Gehört dazu:**
- Options-Flow als Menü: Einstellungen (wie bisher), Sendungen, Letzte Mails.
- Sendungsliste (älteste letzte Mail zuerst) mit Zusteller, Status, Artikel, Zielort, Alter
  und Verfallsdatum; Mehrfachauswahl zum Löschen (mit Bestätigung) und Abhaken.
- Detailansicht: alle Felder, Begründung des Verfalls (gleiche Logik wie die Retention),
  Statusverlauf, Herkunft je Mail mit Betreff und Absender.
- Zielort aus erkannter Adresse oder Abholort anlegen bzw. bestehenden ergänzen: vorbefüllt,
  änderbar, Vorschau der passenden Sendungen, Auswahl wird per `destination_override` fest
  zugeordnet (die Zuordnung wertet den Abholort bewusst nicht aus).
- Protokoll der letzten 100 Mails je Postfach mit Ergebnis, im Store gespeichert.

**Gehört NICHT dazu:**
- Änderungen an den Retention-Regeln.
- Schreibende Aktionen in der Lovelace-Karte.
- Betreffs und Absender in den Diagnostics.

## Hinweise

- Die Schritte der Verwaltung enden mit `async_abort`, nicht `async_create_entry`: Die
  Optionen bleiben unberührt, es gibt keinen Reload. Nur das Anlegen oder Ändern eines
  Zielorts lädt den Entry neu (Subentry-Änderung).
- Freitexte für die Markdown-Beschreibungen stehen unter `selector.manage_text`, weil HA
  frei benannte Texte einer Integration nur dort zulässt.
- `SourceRecord` trägt jetzt Betreff und Absender; `merge_sources` hält einen Nachweis je
  Mail (gedeckelt auf 20) statt einen je Quelle.

## Checkliste

- [x] Menü, Liste, Details, Löschen, Abhaken
- [x] Zielort anlegen/ergänzen mit Vorschau und fester Zuordnung
- [x] Mail-Protokoll mit Filter
- [x] Tests (`tests/test_manage.py`)
- [ ] Manuell in echter HA-Instanz geprüft
