# Step 2.4 — Kalender-Plattform

> Kontext: `planning.md` lesen. Setzt Step 2.3 voraus.

## Ziel

Erwartete Lieferungen erscheinen in der nativen Kalender-Karte und lassen sich als Auslöser für
Automationen verwenden — ohne eine einzige Zeile Template.

## Scope

**Gehört dazu:**
- `calendar.py` mit einer Entity `erwartete_lieferungen` am Haupt-Device.
- Abbildung einer Sendung auf ein Kalenderereignis: bekanntes Zeitfenster → Termin mit
  `eta_start`/`eta_end`, nur bekanntes Datum → ganztägiger Termin, kein Datum → kein Termin.
- Ereignis-Titel nach einem festen Muster aus Carrier, Absender/Händler und Zieladresse;
  Beschreibung mit Trackingnummer, Statustext und Abholort; `location` aus der Zieladresse.
- Umsetzung sowohl der Eigenschaft „aktuelles/nächstes Ereignis" als auch der Abfrage über einen
  Zeitraum, damit die Kalender-Karte historische und künftige Tage anzeigen kann.
- Zugestellte Sendungen bleiben als vergangene Termine sichtbar, solange die Retention läuft.
- Stabile Ereignis-IDs, damit ein neuer Zustelltermin denselben Termin verschiebt statt einen
  zweiten anzulegen.
- Übersetzungen und Icon.

**Gehört NICHT dazu:**
- Actions und Events (2.5), Wunschtermin-Änderung beim Zusteller (nicht geplant).

## Hinweise

- Zeitzonen konsequent über die HA-Zeitzone auflösen; ganztägige Termine brauchen Datumswerte
  ohne Uhrzeit, sonst rutschen sie in der Anzeige um einen Tag.
- Ein Zeitfenster wie „14 bis 18 Uhr" ist ein echter Termin, „voraussichtlich Dienstag" ein
  ganztägiger — die Unterscheidung ist für die Karte sichtbar und lohnt die Sorgfalt.
- Die Kalender-Abfrage über einen Zeitraum wird auch vom `calendar.get_events`-Dienst genutzt;
  sie muss unabhängig vom aktuellen Zustand der Entity korrekt antworten.
- Der Titel landet in Automationen und Benachrichtigungen — kurz und ohne Trackingnummer halten,
  Details gehören in die Beschreibung.

## Akzeptanzkriterien

- Eine Sendung mit Zeitfenster erzeugt einen Termin mit exakt diesen Zeiten.
- Eine Sendung nur mit Datum erzeugt einen ganztägigen Termin am richtigen Tag, auch bei
  abweichender HA-Zeitzone.
- Eine Sendung ohne Zustellprognose erzeugt keinen Termin.
- `calendar.get_events` über einen Zeitraum liefert alle Termine darin, inklusive bereits
  zugestellter innerhalb der Retention.
- Ein neuer Zustelltermin verschiebt den bestehenden Termin, statt zu duplizieren.
- Die Entity meldet das nächste anstehende Ereignis korrekt.

## Checkliste

- [x] calendar.py mit erwartete_lieferungen
- [x] Zeitfenster- und Ganztags-Abbildung inklusive Zeitzonen
- [x] Titel, Beschreibung und location nach festem Muster
- [x] Zeitraum-Abfrage für die Kalender-Karte und get_events
- [x] Stabile Ereignis-IDs
- [x] Übersetzungen, Tests grün
