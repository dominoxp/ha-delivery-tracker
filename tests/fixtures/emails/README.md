# Fixture-Mails

Echte Zustellermails, von Hand anonymisiert: Namen, Adressen, Mailadressen und
Trackingnummern sind durch Platzhalter ersetzt (`example.com`, `example.org`,
Musterstrasse, `EX…DE`). Es darf nie eine echte Sendung in dieses Verzeichnis
geraten — die Dateien liegen oeffentlich im Repository.

Die Mails direkt in diesem Verzeichnis gehoeren zum mitgelieferten
Beispiel-Plugin (`carriers/example/`), damit die Tests der Pipeline mit der
echten Registry laufen koennen. Carrier-eigene Fixtures liegen in einem
Unterverzeichnis je Zusteller.

Trackingnummern sind erfunden, aber formal gueltig: Wo der Zusteller eine
Pruefziffer vorsieht, stimmt sie — sonst wuerden die Plugins die Nummern
zu Recht verwerfen.

Die Fixtures unter `dhl/` und `amazon/` sind aus echten Mails von 2026
abgeleitet: Wortlaut, Zeilenstruktur und Linkaufbau stammen aus dem Original,
gekuerzt um Rechtstexte und eingebettete Bilder. Beide Zusteller verschicken
mehrteilige Mails mit brauchbarem `text/plain`-Teil — den wertet `mail.py`
aus, deshalb sind auch die Fixtures reiner Text. Wo keine echte Mail vorlag,
steht es in der Tabelle dabei.

| Datei                    | Wofuer sie da ist                                            |
| ------------------------ | ------------------------------------------------------------ |
| `utf8_plain.eml`         | UTF-8 mit `quoted-printable`, Umlaute in Betreff und Text     |
| `iso8859_1.eml`          | ISO-8859-1 — der zweithaeufigste Fall bei aelteren Systemen   |
| `broken_charset.eml`     | erfundene Zeichensatzangabe (`x-unknown`, `unknown-8bit`)     |
| `html_only.eml`          | reine HTML-Mail; die Nummer steht auch im Link                |
| `sammelbestaetigung.eml` | mehrteilig, Text-Teil nur Platzhalter, drei Sendungen im HTML |
| `newsletter.eml`         | Werbung ohne Sendung — muss verworfen und gezaehlt werden     |

## `dhl/` — DHL Paket und Deutsche Post

| Datei                      | Wofuer sie da ist                                          |
| -------------------------- | ---------------------------------------------------------- |
| `sendungsankuendigung.eml` | angekuendigt, noch kein Scan; Termin ohne Uhrzeit           |
| `zustellzeitfenster.eml`   | HTML; Nummer nur im Verfolgen-Link, Fenster „14 bis 18 Uhr" |
| `zustellung.eml`           | zugestellt mit eigenem Zeitpunkt                            |
| `nachbar.eml`              | Ablage beim Nachbarn — zugestellt, Abholort ist der Nachbar |
| `packstation.eml`          | HTML; Abholort und Abholcode getrennt                       |
| `filiale.eml`              | Filial-Zustellung mit 12-stelligem Identcode                |
| `zustellversuch.eml`       | nicht angetroffen; naechster Versuch als Termin             |
| `wunschtag.eml`            | Wunschtag-Aenderung verschiebt den Liefertermin             |
| `retoure.eml`              | Retouren-Ankuendigung; die Abgabestelle ist kein Abholort   |
| `einschreiben.eml`         | Deutsche Post: eigener Regelsatz, eigene Sendungsverfolgung |
| `express_kommt_heute.eml`  | echtes Format: Sprachwahl-Block, Text markdown-ausgezeichnet |
| `express_verspaetet.eml`   | neuer Zustelltag eine Zeile unter seiner Beschriftung        |
| `express_filiale.eml`      | unterwegs zur Filiale, ohne konkreten Termin                 |
| `newsletter.eml`           | Werbung des Zustellers — darf nirgends ankommen             |

Die ersten neun Dateien sind nachgebaut (fuer diese Mailtypen lag keine echte
Mail vor), die drei `express_*` stammen aus echten Mails.

## `amazon/` — Bestellung, Versand und Amazon Locker

Amazon vergibt keine Trackingnummern. Anker ist die Bestellnummer plus die
`shipmentId` bzw. `pkgId` aus dem Verfolgen-Link; jede Mail traegt ausserdem den
Fortschrittsbalken „Bestellt / Versendet / In Zustellung / Zugestellt" als
Textzeilen — die Fixtures bilden ihn mit ab, weil er die haeufigste Ursache fuer
Fehltreffer ist.

| Datei                       | Wofuer sie da ist                                              |
| --------------------------- | -------------------------------------------------------------- |
| `bestellbestaetigung.eml`   | zwei Paketbloecke mit eigenem Termin und Artikel                |
| `versandbestaetigung.eml`   | `shipmentId` als Anker, Bestellnummer als Bindeglied fuer 3.2   |
| `versand_sammelpaket.eml`   | zwei Artikel in einem Paket — eine Beschreibung                 |
| `locker_bereit.eml`         | Amazon Locker: Abholort und Abholcode getrennt                  |
| `locker_sammelpaket.eml`    | dieselben Artikel wie die Versandmail, andere Reihenfolge       |
| `locker_abgeholt.eml`       | abgeholt = zugestellt; dieselbe `shipmentId` wie der Versand    |
| `verspaetet.eml`            | Verspaetung mit neuem Liefertermin                              |
| `zugestellt.eml`            | zugestellt mit Uhrzeit, ohne Liefertermin                       |
| `logistics_zeitfenster.eml` | **nachgebaut**: der `TBA`-Pfad, der in den echten Mails fehlt   |
| `werbung.eml`               | „Angebote des Tages" — darf nirgends ankommen                   |
