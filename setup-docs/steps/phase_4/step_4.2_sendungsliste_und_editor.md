# Step 4.2 — Sendungsliste, Filter & visueller Editor

> Kontext: `planning.md` lesen. Setzt Step 4.1 voraus.

## Ziel

Das eigentliche Ziel des Projekts: eine Karte, die alle Sendungen mit vielen Details anzeigt,
nach Zieladresse oder Status filtert — ohne dass dafür Fremdkarten oder Templates nötig sind.

## Scope

**Gehört dazu:**
- Sendungsliste aus dem `shipments`-Attribut der in der Konfiguration gewählten Entity aus 2.3.
- Konfigurationsoptionen der Karte: Titel, Quell-Entity, Filter nach Zieladresse, Status und
  Carrier, Gruppierung (keine, nach Zieladresse, nach Status), Sortierung, Obergrenze der
  angezeigten Einträge, Auswahl der dargestellten Felder.
- Darstellung je Sendung: Carrier, Status, erwartete Zustellung als lesbare Angabe
  („heute 14–18 Uhr", „Dienstag"), Absender/Händler, Beschreibung, Zieladresse, Abholort und
  Abholcode, sowie ein Fortschrittsbalken über die Statusstufen des `ShipmentStatus`-Enums.
- Carrier-Kennzeichnung über mitgelieferte Symbole; keine von externen Servern geladenen Bilder.
- Klick auf eine Sendung öffnet die Details; enthält die Sendung eine Tracking-URL, ist sie
  von dort aufrufbar.
- Leerzustand („keine Sendungen") und Fehlerzustand (Entity nicht verfügbar) gestaltet.
- Visueller Editor für alle Konfigurationsoptionen, damit die Karte ohne YAML nutzbar ist.
- Vitest-Tests für Filterung, Gruppierung, Sortierung und die Aufbereitung der Zeitangaben.

**Gehört NICHT dazu:**
- Schreibende Aktionen aus der Karte heraus (Sendung löschen, als zugestellt markieren) —
  bewusst offen; die Actions aus 2.5 sind vorhanden, die Bedienelemente kommen erst, wenn sich
  die Karte im Alltag bewährt hat.

## Hinweise

- Die Karte liest ausschließlich das Attribut und ruft keine Action auf. Das hält sie schnell und
  vermeidet, dass jeder Renderdurchlauf Last erzeugt. Die Detailtiefe des Attributs aus 2.3 ist
  die Obergrenze dessen, was die Karte zeigen kann — passt beides nicht zusammen, wird 2.3
  angepasst, nicht die Karte um Action-Aufrufe erweitert.
- Zeitangaben in der Sprache des Nutzers ausgeben und die HA-Zeitzone verwenden.
- Auf schmalen Bildschirmen muss die Liste lesbar bleiben; die Karte landet auch auf Mobilgeräten.
- Der Fortschrittsbalken braucht eine definierte Abbildung der Statusstufen — Zustände außerhalb
  des normalen Verlaufs (retour, gescheitert) werden abweichend dargestellt, nicht als Fortschritt.
- Der visuelle Editor ist der Grund, warum sich die Karte für andere Nutzer lohnt; er gehört
  in diesen Step und nicht in einen späteren.

## Akzeptanzkriterien

- Mit Testdaten zeigt die Karte alle Sendungen mit allen konfigurierten Feldern.
- Filter nach Zieladresse, Status und Carrier wirken einzeln und kombiniert.
- Gruppierung nach Zieladresse erzeugt beschriftete Abschnitte.
- Zeitangaben werden lesbar und in der richtigen Zeitzone dargestellt; ganztägige und
  Zeitfenster-Angaben unterscheiden sich sichtbar.
- Retour- und Fehlzustellungen sind im Fortschritt klar von einem normalen Verlauf unterscheidbar.
- Leer- und Fehlerzustand sehen gestaltet aus.
- Der visuelle Editor erlaubt jede Konfigurationsoption ohne YAML; die erzeugte Konfiguration
  ist mit der von Hand geschriebenen identisch.
- Die Karte ist auf einem Mobilgerät lesbar bedienbar.

## Checkliste

- [x] Sendungsliste aus dem shipments-Attribut — 2.3 dafür erweitert: `pickup_code` in
  `as_summary()`, dazu die Attribute `carriers` und `destinations` (Name je ID, vom Recorder
  ausgenommen) für Beschriftung und Editor-Auswahl
- [x] Filter, Gruppierung, Sortierung, Feldauswahl, Obergrenze
- [x] Darstellung inklusive Fortschrittsbalken, Abholort und Tracking-Link
- [x] Mitgelieferte Carrier-Symbole, keine externen Ressourcen — Kürzel-Plaketten statt Logos
  (Marken, feste Farben)
- [x] Leer- und Fehlerzustand
- [x] Visueller Editor für alle Optionen — native Formularelemente statt `ha-form`, damit keine
  internen Frontend-Komponenten nötig sind
- [x] Vitest-Tests grün
- [ ] Manuell in echter HA-Instanz auf Desktop und Mobilgerät geprüft
