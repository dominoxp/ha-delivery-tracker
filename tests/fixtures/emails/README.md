# Fixture-Mails

Echte Zustellermails, von Hand anonymisiert: Namen, Adressen, Mailadressen und
Trackingnummern sind durch Platzhalter ersetzt (`example.com`, `example.org`,
Musterstrasse, `EX…DE`). Es darf nie eine echte Sendung in dieses Verzeichnis
geraten — die Dateien liegen oeffentlich im Repository.

Alle Mails hier gehoeren zum mitgelieferten Beispiel-Plugin
(`carriers/example/`), damit die Tests der Pipeline mit der echten Registry
laufen koennen. Carrier-eigene Fixtures kommen ab Step 1.3 in Unterverzeichnisse
je Zusteller.

| Datei                    | Wofuer sie da ist                                            |
| ------------------------ | ------------------------------------------------------------ |
| `utf8_plain.eml`         | UTF-8 mit `quoted-printable`, Umlaute in Betreff und Text     |
| `iso8859_1.eml`          | ISO-8859-1 — der zweithaeufigste Fall bei aelteren Systemen   |
| `broken_charset.eml`     | erfundene Zeichensatzangabe (`x-unknown`, `unknown-8bit`)     |
| `html_only.eml`          | reine HTML-Mail; die Nummer steht auch im Link                |
| `sammelbestaetigung.eml` | mehrteilig, Text-Teil nur Platzhalter, drei Sendungen im HTML |
| `newsletter.eml`         | Werbung ohne Sendung — muss verworfen und gezaehlt werden     |
