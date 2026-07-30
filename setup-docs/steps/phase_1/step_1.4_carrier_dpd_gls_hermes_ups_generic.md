# Step 1.4 — Carrier-Plugins DPD, GLS, Hermes, UPS & generischer Fallback

> Kontext: `planning.md` lesen. Setzt Step 1.3 voraus.

## Ziel

Die restlichen Zusteller der ersten Ausbaustufe sind abgedeckt, und Mails unbekannter Absender
liefern wenigstens noch eine Trackingnummer für die spätere API-Anreicherung.

## Scope

**Gehört dazu:**
- `carriers/dpd/`, `carriers/gls/`, `carriers/hermes/`, `carriers/ups/` — jeweils `rules.yaml`
  plus Plugin-Logik, nach dem in 1.3 etablierten Muster. Abzudecken sind je Zusteller
  mindestens: Sendungsankündigung, Zustellzeitfenster, Zustellung, gescheiterte Zustellung,
  Ablage bei Nachbar oder Paketshop.
- `carriers/generic/`: greift nur, wenn kein spezifisches Plugin passt. Sucht nach
  Trackingnummern anhand bekannter Nummernformate und der Prüfziffern aus 1.2, leitet den
  Carrier aus dem Nummernformat oder der Absender-Domain ab und setzt den Status auf `unknown`,
  wenn er sich nicht erschließt. Bewusst niedrigste Plugin-Priorität.
- Bewertungslogik so justieren, dass `generic` nie ein spezifisches Plugin verdrängt.
- `.eml`-Fixtures je Zusteller und Mailtyp.

**Gehört NICHT dazu:**
- Anreicherung der `generic`-Treffer über eine API (3.1), Adress-Zuordnung (1.5).

## Hinweise

- DPD und GLS versenden ihre Benachrichtigungen häufig im Auftrag des Händlers — die
  Absender-Domain ist dann die des Shops. Erkennung deshalb stärker über Betreff, Nummernformat
  und Tracking-Link als über den Absender.
- Hermes-Mails enthalten oft nur einen Link ohne sichtbare Nummer; die Extraktion aus
  URL-Parametern aus 1.2 ist hier der Hauptpfad.
- UPS verschickt teils englischsprachige Mails an deutsche Empfänger — Status-Keywords in
  beiden Sprachen aufnehmen.
- Der generische Fallback ist der gefährlichste Teil: Ohne Prüfziffernvalidierung erzeugt er
  Geistersendungen aus Rechnungs- und Kundennummern. Im Zweifel nichts liefern.
- Wiederholungen zwischen den `rules.yaml`-Dateien sind in Ordnung — eine gemeinsame
  Abstraktion lohnt erst, wenn dasselbe Muster in vier Dateien identisch auftaucht.

## Nachtrag aus Step 1.3 (echte Mails, Juli 2026)

Beim Prüfen der Plugins an einem echten Postfach ist ein Fall aufgefallen, der diesen Step
betrifft: **Ein spezifisches Plugin kann eine fremde Mail beanspruchen, ohne etwas zu
liefern.** Eine DPD-Mail bekam vom DHL-Plugin 20 Punkte (Betreff „Ihr Paket"), eine
Shop-Bestellbestätigung vom Amazon-Plugin ebenfalls 20 — beide lieferten null Sendungen.

Das ist mehr als ein Schönheitsfehler: `CarrierRegistry.parse_all` wertet nur das
bestbewertete Plugin aus und fällt **nicht** auf das nächstbeste zurück. Ein `generic` mit
`score_bonus: -100` landet bei Bewertung 1 und kommt damit nie zum Zug, solange irgendein
spezifisches Plugin 20 Punkte auf einen weiten Betreff sammelt. Die Shop-Bestellung würde also
nie erfasst — obwohl `generic` genau dafür da ist.

Zwei Wege, einer davon ist zu wählen:
- Die Betreffs-Muster der spezifischen Plugins so eng fassen, dass sie ohne Absender-Domain
  nicht mehr anspringen (bei DPD löst sich das ohnehin, sobald das DPD-Plugin mit seiner
  Domain 50 Punkte holt), **oder**
- `parse_all` auf das nächstbeste Plugin zurückfallen lassen, wenn das beste nichts liefert.
  Das ist die robustere Variante und der Grund, warum `matches` von Anfang an eine Bewertung
  liefert statt eines Ja/Nein.

Als Testmaterial liegen eine echte DPD- und eine echte Shop-Mail vor (lokal unter
`tools/emails/`, nicht im Repository — sie müssen vor der Verwendung anonymisiert werden).

## Akzeptanzkriterien

- Je Zusteller und Mailtyp existiert eine Fixture mit Test auf die erwarteten Felder.
- Eine Mail eines nicht unterstützten Zustellers mit gültiger Trackingnummer wird von `generic`
  mit Status `unknown` erfasst.
- Eine Mail mit Rechnungsnummer, aber ohne gültige Trackingnummer, wird von `generic`
  **nicht** erfasst.
- Eine DHL-Mail wird weiterhin vom DHL-Plugin verarbeitet, nicht von `generic`.
- Eine Mail, für die sich ein spezifisches Plugin zwar zuständig fühlt, aus der es aber keine
  Sendung zieht, landet trotzdem bei `generic` — siehe Nachtrag oben.
- Eine Hermes-Mail ohne sichtbare Nummer liefert die Nummer aus dem Link.

## Checkliste

- [ ] carriers/dpd/, gls/, hermes/, ups/ mit rules.yaml und Logik
- [ ] carriers/generic/ mit Nummernformat-Erkennung und niedrigster Priorität
- [ ] Bewertungslogik: generic verdrängt nie ein spezifisches Plugin
- [ ] Fixtures je Zusteller und Mailtyp
- [ ] Negativtests gegen Geistersendungen
- [ ] Tests grün
