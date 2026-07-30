# Step 1.3 — Carrier-Plugins DHL/Deutsche Post & Amazon

> Kontext: `planning.md` lesen. Setzt Step 1.2 voraus.

## Ziel

Die beiden wichtigsten Quellen liefern echte Sendungen mit möglichst vielen Details — und
setzen den Qualitätsmaßstab für alle weiteren Carrier-Plugins.

## Scope

**Gehört dazu:**
- `carriers/dhl/` mit `rules.yaml` und Plugin-Logik: DHL Paket und Deutsche Post,
  Sendungsankündigung, Zustellversuch, Zustellung, Ablage beim Nachbarn, Filial- und
  Packstation-Zustellung (Abholort und Abholcode als eigene Felder), Zustellzeitfenster,
  Wunschtag-Änderungen, Retouren-Ankündigung.
- `carriers/amazon/` mit `rules.yaml` und Plugin-Logik: Bestellbestätigung (noch ohne
  Trackingnummer, ID aus Händler + Bestellnummer wie in 0.2 vorgesehen), Versandbestätigung,
  Amazon Logistics mit eigenem Zeitfenster, Lieferung verspätet, Lieferung zugestellt.
  Produktbeschreibung und Bestellnummer werden mitgenommen.
- Die Bestellnummer wird auch dann mitgeführt, wenn die Mail bereits eine Trackingnummer hat —
  sie ist das Bindeglied, über das Step 3.2 Bestellung und Sendung zusammenführt.
- Status-Zuordnung der Originaltexte auf den `ShipmentStatus`-Enum; der Originaltext bleibt in
  `status_text` erhalten.
- Tracking-URLs je Carrier.
- `.eml`-Fixtures je abgedecktem Mailtyp unter `tests/fixtures/emails/dhl/` und `.../amazon/`.

**Gehört NICHT dazu:**
- Übrige Carrier (1.4), Adress-Zuordnung (1.5), DHL-Developer-API (Backlog).
- Das Zusammenführen von Bestellbestätigung und späterer Versandbestätigung zu **einer**
  Sendung. Beide bekommen unterschiedliche IDs (`amazon_order_…` bzw. `amazon_<nummer>`), und
  `merge_shipments` setzt gleiche IDs voraus — nach diesem Step erscheint eine Amazon-Bestellung
  daher vorübergehend zweimal. Das Zusammenführen zweier getrennt geführter Sendungen ist in
  3.2 vorgesehen, weil es dort ohnehin allgemein für alle Quellen gebaut wird.

## Nachtrag aus der Umsetzung (echte Mails, Juli 2026)

Die Annahmen über die Amazon-Mails haben sich an echten Mails nicht gehalten; die Regeln sind
danach neu gebaut worden:

- **Amazon vergibt keine Trackingnummer.** Weder im Text noch im Betreff. Anker ist die
  Bestellnummer plus die `shipmentId` aus dem „Lieferung verfolgen"-Link — sie ist stabil und
  verbindet Versand- mit Zustellmail. `build_shipment_id` hat dafür einen `package`-Zusatz
  bekommen; ohne ihn verschmelzen die vier Pakete einer Bestellung zu einer Sendung.
- **Die Locker-Mails** (`pickup-point@amazon.de`: „Paket zur Abholung bereit", „Erinnerung",
  „Abgeholt") sind eine eigene Mailfamilie. Sie nennen `pkgId` statt `shipmentId` und führen
  deshalb vorerst eine eigene Sendung — dieselbe Trennung wie oben, dieselbe Lösung in 3.2.
  Der Abholcode steht nur im Ziel des „Abholung starten"-Links.
- **Jede Amazon-Mail trägt den Fortschrittsbalken** „Bestellt / Versendet / In Zustellung /
  Zugestellt" als vier Textzeilen, unabhängig vom Stand. Muster, die auf eines dieser Wörter
  allein anspringen, machen aus einer Bestellbestätigung eine Sendung in Zustellung.
- **`Shipment.items` ist neu** (Erweiterung des Datenmodells aus 0.2). Ein Paket mit mehreren
  Artikeln wird in Versand- und Abholmail in *unterschiedlicher* Reihenfolge aufgezählt, und
  die Zustellmail nennt gar keinen Artikel. Der gekürzte Anzeigetext in `description` taugt
  deshalb nicht als Bindeglied — die vollständige Artikelmenge schon. Zusammen mit der
  Bestellnummer hat 3.2 damit alles, was es zum Zusammenführen braucht.
- **DHL zeichnet den Text-Teil markdown-artig aus** („wird Ihnen **heute** … zugestellt") und
  stellt 35 Zeilen Sprachumschalter voran. Das Plugin entfernt die Sternchen vor der
  Regelauswertung.

## Hinweise

- Amazon ist der schwierigste Fall: reine HTML-Mails, wechselnde Layouts, Marketing-Blöcke im
  selben Dokument. Erkennungssignale eng fassen und die Robustheit über die Fixtures absichern.
- Deutsche Post und DHL nutzen teils dieselben Absender-Domains, aber unterschiedliche
  Nummernformate — sie bleiben trotzdem ein Plugin mit zwei Regelsätzen.
- Zeitfenster („heute zwischen 14 und 18 Uhr") in `eta_start`/`eta_end` abbilden, reine
  Datumsangaben als ganztägig. Zeitzone ist die HA-Zeitzone.
- Amazon-Bestellungen mit mehreren Paketen erzeugen mehrere Sendungen zur selben Bestellnummer.
- Was ein Plugin nicht sicher erkennt, bleibt leer — lieber ein fehlendes Feld als ein falsches.

## Akzeptanzkriterien

- Für jeden abgedeckten Mailtyp existiert eine Fixture und ein Test, der die erwarteten Felder
  prüft (Status, Trackingnummer, Zeitfenster, Absender, Abholort).
- Eine Packstation-Zustellung liefert Abholort und Abholcode getrennt.
- Die Amazon-Bestellbestätigung ergibt eine Sendung mit Bestellnummer und Produktbeschreibung,
  die spätere Versandbestätigung eine mit derselben Bestellnummer plus Trackingnummer. Dass
  beide (noch) getrennt geführt werden, ist erwartet — zusammengeführt wird in 3.2.
- Eine Amazon-Werbemail und ein DHL-Newsletter werden von keinem Plugin angenommen.
- Alle erzeugten Tracking-URLs sind aufrufbar aufgebaut.

## Checkliste

- [x] carriers/dhl/ mit rules.yaml, Logik und allen genannten Mailtypen
- [x] carriers/amazon/ mit rules.yaml, Logik und allen genannten Mailtypen
- [x] Bestellnummer wird auch bei vorhandener Trackingnummer mitgeführt (Bindeglied für 3.2)
- [x] Zeitfenster, Abholort und Abholcode korrekt befüllt
- [x] Anonymisierte Fixtures je Mailtyp
- [x] Negativtests gegen Werbemails
- [x] Tests grün
