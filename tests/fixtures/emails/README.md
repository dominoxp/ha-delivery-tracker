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

## `dpd/`, `gls/`, `hermes/`, `ups/` — die uebrigen Zusteller (Step 1.4)

Echte Vorlagen gab es nur fuer DPD (`unterwegs.eml`) und Hermes
(`paketshop.eml`, `unterwegs_zum_paketshop.eml`); alles andere ist nachgebaut.
DPD, GLS und Hermes kennen kein Pruefverfahren, ihre Nummern sind deshalb frei
erfunden. Die UPS-Nummern haben eine stimmende Pruefziffer.

| Datei                              | Wofuer sie da ist                                                 |
| ---------------------------------- | ----------------------------------------------------------------- |
| `dpd/unterwegs.eml`                | **echt**: Nummer im Betreff ohne Ankuendigung und im Link; `zip=` |
| `dpd/zustellzeitfenster.eml`       | Nummer in Vierergruppen, Fenster fuer heute                        |
| `dpd/zustellung.eml`               | zugestellt mit eigenem Zeitpunkt                                   |
| `dpd/zustellversuch.eml`           | nicht angetroffen, naechster Versuch als Termin                    |
| `dpd/nachbar.eml`                  | beim Nachbarn zugestellt — trotz „nicht angetroffen" im Text       |
| `dpd/paketshop.eml`                | Abholort und Abholcode getrennt                                    |
| `dpd/im_auftrag_des_shops.eml`     | Shop als Absender, DPD nur in Link und Text                        |
| `gls/sendungsankuendigung.eml`     | Paketnummer und Track ID in einer Mail — eine Sendung             |
| `gls/zustellzeitfenster.eml`       | HTML; Nummer nur im Verfolgen-Link (`?match=`)                     |
| `gls/zustellung.eml`               | zugestellt mit eigenem Zeitpunkt                                   |
| `gls/zustellversuch.eml`           | nur die Track ID                                                   |
| `gls/nachbar.eml`                  | beim Nachbarn abgegeben                                            |
| `gls/paketshop.eml`                | abholbereit im PaketShop                                           |
| `hermes/sendungsankuendigung.eml`  | Nummer nur im Link-Fragment (`…#H100…`), wie in den echten Mails   |
| `hermes/unterwegs_zum_paketshop.eml` | **echt**: „liegt in Kuerze zur Abholung bereit" ist noch unterwegs |
| `hermes/paketshop.eml`             | **echt**: Abholort als Block unter „PaketShop", Haendler im Satz   |
| `hermes/zustellzeitfenster.eml`    | 14-stellige Nummer, Fenster fuer heute                             |
| `hermes/zustellung.eml`            | 16-stellige Nummer, zugestellt mit Zeitpunkt                       |
| `hermes/zustellversuch.eml`        | nicht angetroffen, naechster Versuch als Termin                    |
| `hermes/nachbar.eml`               | an den Nachbarn uebergeben                                         |
| `ups/versandetikett.eml`           | Etikett erstellt; „der Versender hat …" ist kein Absendername      |
| `ups/zustellzeitfenster.eml`       | Nummer in Gruppen (`1Z 0E1 234 …`), Fenster fuer heute             |
| `ups/zugestellt_englisch.eml`      | englisch; US-Datum wird nicht geraten                              |
| `ups/unterwegs_englisch.eml`       | englisch, unterwegs                                                |
| `ups/zustellversuch.eml`           | nicht angetroffen, naechster Versuch als Termin                    |
| `ups/nachbar.eml`                  | beim Nachbarn abgegeben                                            |
| `ups/access_point.eml`             | „an den Access Point zugestellt" ist abholbereit, nicht zugestellt |

## `generic/` — der Fallback

| Datei                       | Wofuer sie da ist                                                        |
| --------------------------- | ------------------------------------------------------------------------ |
| `unbekannter_zusteller.eml` | Shop-Mail mit gueltiger S10-Nummer eines Zustellers ohne Plugin          |
| `fremd_beansprucht.eml`     | DHL meldet sich wegen „Ihr Paket", liefert nichts — `generic` uebernimmt  |
| `rechnung.eml`              | gueltige Pruefziffern hinter Rechnungs-/Kundennummer — darf nichts liefern |
| `shop_ohne_nummer.eml`      | **echt**: Statusmail eines Shops ohne Trackingnummer — liefert nichts     |

## `aliexpress/` — Bestellung, Zubringer, Sammelpaket, letzte Meile

Abgeleitet aus echten Mails von 2026; Artikel, Varianten, Bestell- und
Trackingnummern sind erfunden, die DHL-Nummern mit stimmender Pruefziffer. Die
Mails sind reines HTML ohne Text-Teil und auf den Teil vor der Werbung gekuerzt.
Sie bilden eine Kette: vier Bestellungen, der Zubringer `LP…`, das Sammelpaket
`AP…` und dessen DHL-Paket `…185651`; dazu ein Hermes-Paket mit deutschen Mails.
Die Bestellnummern stehen in den Paketmails nur im Verfolgen-Link.

| Datei                               | Wofuer sie da ist                                              |
| ----------------------------------- | -------------------------------------------------------------- |
| `bestellung_bestaetigt.eml`         | Bestellung ohne Trackingnummer, Status bestellt                 |
| `bestellung_versandt.eml`           | versandt, aber noch ohne Paketnummer                           |
| `bewertung.eml`                     | Timer-Mail „Wie war Ihr Einkaufserlebnis?" — aendert nichts     |
| `neuer_lieferstatus.eml`            | Infomail ohne Statusaussage — aendert nichts                    |
| `order_collected.eml`               | englisch, abgeholt, wieder ohne Paketnummer                    |
| `package_ready_to_ship.eml`         | Sammelpaket `AP…` mit vier Bestellungen in den Links            |
| `sammelmail.eml`                    | zwei Pakete, Nummer und Status je Block nur im Link             |
| `package_left_departure.eml`        | DHL-Nummer — Uebergabe an das DHL-Plugin                        |
| `package_delivery_issue.eml`        | Zustellproblem; verlinkt zusaetzlich den Zubringer              |
| `package_ready_for_pickup.eml`      | abholbereit nach dem Zustellproblem                             |
| `package_delivered.eml`             | zugestellt                                                     |
| `packstueck_mit_lokalem_kurier.eml` | deutsch, Hermes-Nummer ohne Pruefziffer                         |
| `paket_zugestellt.eml`              | deutsch, zugestellt                                            |
| `order_closed.eml`                  | Storno-Signal                                                  |
| `werbung.eml`                       | Werbung von derselben Domain — erkannt, keine Sendung           |
