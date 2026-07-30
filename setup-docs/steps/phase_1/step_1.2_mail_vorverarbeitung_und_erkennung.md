# Step 1.2 — Mail-Vorverarbeitung, Carrier-Erkennung & Trackingnummern

> Kontext: `planning.md` lesen. Setzt Step 1.1 voraus.

## Ziel

Aus einer Roh-Mail wird ein sauber aufbereitetes Objekt, dem ein Carrier-Plugin zugeordnet ist
und aus dem verlässliche Trackingnummern extrahiert werden — ohne Fehltreffer.

## Scope

**Gehört dazu:**
- `mail.py`: `ParsedMail` mit dekodierten Headern (Absender, Anzeigename, Empfänger, Betreff,
  Datum, `List-Unsubscribe`) und aufbereitetem Textkörper.
- MIME-Verarbeitung: mehrteilige Mails, Zeichensatz-Erkennung inklusive fehlerhafter Angaben,
  `quoted-printable`/`base64`, Auswahl des besten Teils (Text bevorzugt, sonst HTML).
- HTML → Text: Tags entfernen, Links als `Text (URL)` erhalten (Tracking-Links sind oft die
  einzige Fundstelle der Nummer), Whitespace normalisieren, Tracking-Pixel verwerfen.
- `tracking.py`: Normalisierung von Trackingnummern (Leerzeichen und Bindestriche entfernen,
  Großschreibung) und Validierung über Prüfziffern, wo der Zusteller welche vorsieht —
  mindestens UPS `1Z` und der DHL/Deutsche-Post-Leitcode. Ohne Prüfziffernvalidierung
  produziert der generische Fallback in 1.4 zu viele Fehltreffer.
- Extraktion aus URL-Parametern zusätzlich zum Fließtext.
- Erkennungs-Pipeline: alle Plugins bewerten die Mail, das bestbewertete gewinnt,
  bei Gleichstand entscheidet die Plugin-Priorität. Keine Übereinstimmung → Mail wird
  verworfen und gezählt (Kennzahl für die Diagnose in 2.6).
- Eine Mail kann mehrere Sendungen enthalten (Sammelbestätigung); die Pipeline gibt eine Liste
  zurück, keinen Einzelwert.

**Gehört NICHT dazu:**
- Konkrete Carrier-Regeln (1.3/1.4), Adress-Zuordnung (1.5).

## Hinweise

- Newsletter und Werbung desselben Absenders sind die häufigste Fehlerquelle: Ein Plugin darf
  erst zuschlagen, wenn zusätzlich zur Absender-Domain ein inhaltliches Signal passt.
- Mails mit fehlerhaftem oder fehlendem Zeichensatz dürfen nie eine Exception auslösen —
  im Zweifel `errors="replace"` und weiterarbeiten.
- HTML-Aufbereitung ohne zusätzliche Abhängigkeit lösen (Standardbibliothek reicht);
  jede neue Anforderung im `manifest.json` muss durch HACS und hassfest.
- Bestellnummern, Kundennummern und Rechnungsnummern sehen Trackingnummern zum Verwechseln
  ähnlich — Kontextwörter im Umfeld der Fundstelle mitbewerten.
- Die Fixture-Mails in `tests/fixtures/emails/` sind anonymisiert abzulegen: echte Namen,
  Adressen und Trackingnummern durch Platzhalter ersetzen.

## Akzeptanzkriterien

- Fixture-Mails in UTF-8, ISO-8859-1 und mit defekter Zeichensatzangabe werden korrekt
  dekodiert, keine wirft eine Exception.
- Eine reine HTML-Mail liefert lesbaren Text mit erhaltenen Links.
- Prüfziffern-Tests: gültige und gezielt verfälschte UPS- und DHL-Nummern werden richtig
  angenommen bzw. abgelehnt.
- Eine Bestellnummer im Fließtext wird nicht als Trackingnummer erkannt.
- Eine Sammelbestätigung mit drei Sendungen ergibt drei Ergebnisse.
- Eine nicht zuordenbare Mail wird verworfen und in der Kennzahl gezählt.

## Checkliste

- [x] mail.py mit ParsedMail, MIME-Dekodierung und Zeichensatz-Fallbacks
- [x] HTML→Text mit Linkerhalt
- [x] tracking.py mit Normalisierung und Prüfziffern (UPS, DHL)
- [x] Extraktion aus URL-Parametern
- [x] Erkennungs-Pipeline mit Bewertung und Mehrfach-Ergebnissen
- [x] Anonymisierte .eml-Fixtures angelegt
- [x] Tests grün

## Umsetzungsnotizen

- Der Container heißt weiterhin `MailMessage` (nicht `ParsedMail`) — der Name
  steht seit Step 0.3 in der Plugin-Schnittstelle; ein Umbenennen hätte nur
  Aufrufer angefasst, ohne etwas zu verbessern.
- `tracking.py` enthält die Rechenvorschriften der Prüfziffern, **nicht** die
  Zuordnung „Format X gehört Zusteller Y". Welches Verfahren gilt, nennt jedes
  Plugin über `tracking.checksum` in seiner `rules.yaml`; ohne Angabe wird nicht
  geprüft. Grund: Zwölf- und vierzehnstellige Ziffernfolgen nutzen DHL, DPD und
  GLS mit *verschiedenen* Verfahren — ein geratenes Verfahren würde ab Step 1.4
  gültige Nummern verwerfen. `checksum` ist eine Liste, damit der generische
  Fallback aus 1.4 alle Verfahren zugleich anbieten kann; das Raten ist dort
  seine Aufgabe. Die Algorithmen müssen deshalb geteilt bleiben und dürfen nicht
  in die einzelnen Plugins wandern.
- Gegen Newsletter wirken drei Dinge zusammen: Die Absender-Domain allein
  ergibt nie einen Treffer, `match.newsletter_patterns` setzt hart auf 0, und
  `List-Unsubscribe` ohne Trackingnummer kostet 25 Punkte.
- Der Kontext wird ab der Position der **Nummer** gemessen, nicht ab dem
  Trefferanfang — sonst fällt bei `sendungsnummer:\s*(?P<value>…)` genau das
  Kontextwort aus dem Fenster, das den Treffer belegt.
