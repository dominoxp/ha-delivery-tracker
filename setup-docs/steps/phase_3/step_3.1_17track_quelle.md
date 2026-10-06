# Step 3.1 — 17track als Quelle

> Kontext: `planning.md` lesen. Setzt Step 2.6 voraus.

## Ziel

Die erste API-Quelle steht und beweist, dass das Quellen-Interface trägt: Trackingnummern aus
E-Mails bekommen einen echten Live-Status, auch wenn keine weitere Mail mehr kommt.

## Scope

**Gehört dazu:**
- `sources/seventeentrack_source.py` als `TrackingSource`-Implementierung mit dem
  Quellen-Vorrang aus 0.2 (API vor E-Mail).
- API-Client über die HA-eigene aiohttp-Session: Registrieren einer Trackingnummer,
  Statusabfrage, Abmelden einer abgeschlossenen Sendung.
- Abgleich beider Richtungen: Neue Sendungen aus E-Mails werden bei 17track registriert;
  Sendungen, die dort bereits liegen, werden übernommen, auch wenn nie eine Mail kam.
- Abbildung der 17track-Zustände und Carrier-Kennungen auf den eigenen `ShipmentStatus`-Enum
  und die eigenen Carrier-IDs, inklusive Event-Historie.
- Kontingent-Schonung: nur aktive Sendungen abfragen, konfigurierbares Mindestintervall,
  Abfragen bündeln, abgeschlossene Sendungen abmelden.
- Fehlerbehandlung: erschöpftes Kontingent, ungültiger Schlüssel und Drosselung sind
  unterscheidbar; Backoff bei Drosselung, Repair-Eintrag aus 2.6 bei Schlüsselproblemen.
- Aktivierung des `api_source`-Subentry-Gerüsts aus 2.1: Schlüsselprüfung beim Anlegen,
  Anzeige des verbleibenden Kontingents in den Diagnostics.

**Gehört NICHT dazu:**
- Feinschliff der Quellen-Priorisierung (3.2), weitere APIs (Backlog).

## Hinweise

- Das kostenlose Kontingent ist knapp. Jede vermeidbare Abfrage kostet echte Funktionalität —
  Nummern, die 17track als zugestellt meldet, sofort abmelden.
- Die Registrierung ist der teure Aufruf: nur registrieren, wenn die Nummer die Validierung aus
  1.2 besteht, damit Fehltreffer des generischen Fallbacks kein Kontingent verbrennen.
- 17track kennt eigene Carrier-Nummern; die Zuordnungstabelle gehört als Datei neben das Modul,
  nicht in den Code.
- Die API ist der autoritative Status, aber die Mail kennt Händler, Produkt und Zieladresse.
  Diese Felder darf die API nie überschreiben — das Merging aus 0.2 regelt das bereits.
- API-Antworten in den Tests aus abgelegten Fixtures bedienen, nie gegen den echten Dienst
  testen.

## Akzeptanzkriterien

- Eine per E-Mail gefundene Sendung wird registriert, und ihr Status wird beim nächsten Durchlauf
  von der API übernommen.
- Eine nur bei 17track vorhandene Sendung erscheint in HA.
- Händler, Produktbeschreibung und Zieladresse aus der Mail überleben die API-Aktualisierung.
- Eine zugestellte Sendung wird abgemeldet und nicht weiter abgefragt.
- Erschöpftes Kontingent, ungültiger Schlüssel und Drosselung führen zu drei unterscheidbaren
  Reaktionen; nur die Schlüsselprobleme erzeugen einen Repair-Eintrag.
- Ein Ausfall der API lässt die E-Mail-Quelle unberührt.

## Checkliste

- [x] seventeentrack_source.py mit Registrieren, Abfragen, Abmelden
- [x] Status- und Carrier-Zuordnung als eigene Datei
- [x] Kontingent-Schonung: nur aktive Sendungen, Bündelung, Mindestintervall
- [x] Unterscheidbare Fehlerbehandlung mit Backoff und Repair
- [x] api_source-Subentry aktiviert, Kontingent in den Diagnostics
- [x] Tests gegen Fixtures grün
