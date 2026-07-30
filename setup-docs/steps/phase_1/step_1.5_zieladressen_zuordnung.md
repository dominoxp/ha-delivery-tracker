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

- [ ] addresses.py mit allen Match-Regeln
- [ ] Bewertung mit Mindestwert und Fallback „Unbekannt"
- [ ] Toleranter Textvergleich (Umlaute, Abkürzungen, Schreibweisen)
- [ ] Adressextraktion aus der Mail als Rückfallebene
- [ ] destination_raw bleibt immer erhalten
- [ ] Neubewertung bei Regeländerung
- [ ] Tests grün
