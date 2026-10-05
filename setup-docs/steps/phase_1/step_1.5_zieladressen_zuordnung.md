# Step 1.5 — Zieladressen-Zuordnung

> Kontext: `planning.md` lesen. Setzt Step 1.4 voraus.

## Ziel

Jede Sendung wird einer benannten Zieladresse zugeordnet, damit das Dashboard später nach
„Zuhause", „Büro" oder „Packstation" filtern kann.

## Scope

**Gehört dazu:**
- `addresses.py`: Bewertung einer Sendung gegen die konfigurierten `Destination`-Objekte aus 0.2.
- Unterstützte Match-Regeln je Adresse, alle optional und kombinierbar:
  Empfängername, Straße mit Hausnummer, Postleitzahl, Ort, Packstation- oder Filialnummer,
  Postnummer, Empfänger-Mailadresse (auch Alias-Adressen), freies Schlüsselwort.
- Bewertung statt starrem Ja/Nein: Die Adresse mit der höchsten Übereinstimmung gewinnt,
  bei Gleichstand entscheidet die konfigurierte Reihenfolge. Unter einem Mindestwert bleibt die
  Sendung bei der eingebauten Adresse „Unbekannt".
- Toleranter Textvergleich: Groß-/Kleinschreibung, Umlaute und deren Umschreibungen,
  Abkürzungen wie „Str." und „Straße", Leerzeichen in Postleitzahlen.
- `destination_raw`: die im Original gefundene Adresszeile bleibt immer erhalten, auch wenn
  keine Zuordnung gelingt — sie ist die Grundlage, um im Options-Flow eine neue Regel anzulegen.
- Extraktion der Adressbestandteile aus der Mail als Teil dieses Steps, sofern das Carrier-Plugin
  keine geliefert hat.

**Gehört NICHT dazu:**
- Verwaltung der Adressen im Config-Flow (2.1), Anzeige und Filterung (2.3, 4.2).

## Hinweise

- Packstation-Zustellungen tragen keine Straße, sondern Automatennummer und Postnummer —
  diese Regel muss allein ausreichen.
- Viele Zusteller kürzen die Adresse in der Mail (nur Ort und Postleitzahl). Die Bewertung muss
  auch mit einem einzigen Treffer noch zu einer Zuordnung kommen können.
- Sendungen an Nachbarn oder in einen Paketshop gehören trotzdem zur ursprünglichen Zieladresse;
  der Abholort ist ein eigenes Feld und darf die Zuordnung nicht verfälschen.
- Die Zuordnung wird bei jeder Aktualisierung neu bewertet: Ändert der Nutzer eine Regel, sollen
  bestehende Sendungen beim nächsten Durchlauf umziehen.
- Adressdaten sind personenbezogen — in den Diagnostics aus 2.6 konsequent maskieren.

## Nachtrag aus der Umsetzung (Oktober 2026)

- **Bewertet wird nur, was an der Sendung gespeichert ist:** `destination_raw` und das neue
  Feld `Shipment.recipient_emails` (Empfänger aus `To`, `Delivered-To` usw., beim Merge
  vereinigt). Die Mail selbst ist bei der Neubewertung längst verarbeitet; ohne gespeicherte
  Empfängeradressen könnte eine Mailadress-Regel nie nachträglich greifen.
- **`Destination` hat drei neue Regelarten:** `cities`, `post_numbers`, `keywords`.
  `packstations` nimmt Packstation- und Filialnummern („123" oder „Packstation 123"); eine reine
  Zahl trifft nur hinter einem passenden Wort (…station, Filiale, Locker), sonst wäre jede
  Hausnummer 123 eine Packstation. `emails` versteht Plus-Aliase (`max+shop@…` = `max@…`) und
  `@domain` für eine ganze Domain.
- **Gewichte** (`addresses.py`): Automaten- und Postnummer je 40, Straße und Mailadresse 30,
  Name, PLZ und Schlüsselwort 20, Ort 10; Mindestwert 15. Jeder Einzeltreffer außer dem Ort
  reicht damit für eine Zuordnung. Je Regelart zählt nur der beste Treffer; bei Name, Straße,
  Ort und Schlüsselwort gibt jedes getroffene Wort einen Punkt extra, damit „Neue
  Musterstraße 1" die „Musterstraße 1" schlägt.
- **Eine fremde Postleitzahl schließt aus:** Trägt die Adresszeile eine PLZ, aber keine des
  Zielorts, scheidet er aus. Sonst gewönne bei „Max Mustermann, 99999 Irgendwo" der erste
  Zielort allein über den Namen, den alle teilen.
- **Eingebauter Zielort** `DESTINATION_UNKNOWN = "unknown"` in `const.py`. Ein konfigurierter
  Zielort mit dieser ID wird ignoriert — der Config-Flow in 2.1 muss sie sperren.
- **Neubewertung:** `ShipmentStore.async_assign_destinations(AddressMatcher(...))` ordnet alle
  gespeicherten Sendungen neu zu, ohne `last_updated` anzufassen, und liefert die umgezogenen
  IDs. Der Coordinator (2.2) ruft das bei jedem Durchlauf nach dem Merge; das erledigt auch das
  Umziehen auf „Unbekannt" beim Löschen eines Zielorts (2.1). Die Pipeline ordnet bereits beim
  Mailempfang zu (`MailPipeline.set_destinations`), das ist aber nur der erste Stand.
- **Adressextraktion als Rückfallebene** sucht nur hinter Überschriften wie „Lieferadresse:",
  „Empfänger:", „Zustellung an:", „Ship to:" — auch als Block unter einer alleinstehenden
  Überschrift. Eine freie Suche nach „PLZ Ort" fände vor allem das Impressum. Werte mit
  Nachbar, Paketshop oder Abholung werden übersprungen; eine Postnummer wird angehängt.
- **Für 2.6:** `recipient_emails` ist wie `destination_raw` personenbezogen und gehört in die
  Maskierung. `AddressMatcher.evaluate` liefert je Zielort Bewertung und getroffene Regelarten
  (`hits`) — brauchbar für Diagnostics, ohne Adressdaten preiszugeben.

## Akzeptanzkriterien

- Eine Sendung mit vollständiger Adresse wird der richtigen von drei konfigurierten Adressen
  zugeordnet.
- Eine Sendung nur mit Postleitzahl und Ort wird ebenfalls zugeordnet.
- Eine Packstation-Sendung wird allein über Automaten- und Postnummer zugeordnet.
- Umlaut- und Abkürzungsvarianten derselben Straße führen zur selben Adresse.
- Eine Sendung ohne erkennbare Adresse landet bei „Unbekannt", behält aber `destination_raw`.
- Zwei ähnlich benannte Adressen im selben Ort werden nicht verwechselt.
- Eine geänderte Regel ordnet eine bestehende Sendung beim nächsten Durchlauf neu zu.

## Checkliste

- [x] addresses.py mit allen Match-Regeln
- [x] Bewertung mit Mindestwert und Fallback „Unbekannt"
- [x] Toleranter Textvergleich (Umlaute, Abkürzungen, Schreibweisen)
- [x] Adressextraktion aus der Mail als Rückfallebene
- [x] destination_raw bleibt immer erhalten
- [x] Neubewertung bei Regeländerung
- [x] Tests grün
