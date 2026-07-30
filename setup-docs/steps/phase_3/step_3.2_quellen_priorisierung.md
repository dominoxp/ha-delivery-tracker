# Step 3.2 — Quellen-Priorisierung & Konfliktauflösung

> Kontext: `planning.md` lesen. Setzt Step 3.1 voraus.

## Ziel

Mit zwei aktiven Quellen zeigt sich, wo das Merging aus 0.2 in der Praxis danebenliegt. Dieser
Step härtet es und macht nachvollziehbar, woher jede Angabe stammt.

## Scope

**Gehört dazu:**
- Überarbeitung von `merge.py` anhand der in 3.1 aufgetretenen realen Konflikte, insbesondere:
  widersprüchliche Zustellzeitfenster, unterschiedliche Carrier-Zuordnung derselben Nummer,
  Zustellmeldung der API vor der Zustellmail (und umgekehrt).
- Herkunft je Feld festhalten, damit in den Diagnostics und über `get_shipments` erkennbar ist,
  welche Quelle welchen Wert geliefert hat.
- Zusammenführen bisher getrennt geführter Sendungen, wenn sich herausstellt, dass es dieselbe
  ist. Die zusammengeführte Sendung behält die ältere ID, damit Automationen nicht brechen.
  Zwei Fälle sind abzudecken:
  - **Aus 1.3 übernommen — Amazon.** Nach 1.3 führt Amazon *drei* getrennte Sendungsfamilien
    zur selben Bestellung, weil jede Mailfamilie eine andere Kennung nennt:

    | Mailtyp                                   | ID nach 1.3                                 |
    | ----------------------------------------- | ------------------------------------------- |
    | Bestellbestätigung, ein Block je Artikel  | `amazon_order_<bestellnr>_<blockindex>`     |
    | Versandbestätigung und „Abgeholt"         | `amazon_order_<bestellnr>_<shipmentId>`     |
    | Locker „zur Abholung bereit"/„Erinnerung" | `amazon_order_<bestellnr>_<pkgId>`          |

    Versand und „Abgeholt" rasten schon in 1.3 über die `shipmentId` aufeinander ein; die
    beiden anderen Familien nicht. Eine Bestellung über vier Artikel in drei Paketen ergibt
    damit **zehn** Sendungen statt drei. Hier zusammenzuführen ist deshalb kein Kosmetikpunkt.
  - Bestellung ohne Nummer trifft auf die Trackingnummer aus 17track.
- Umgang mit einer Sendung, die eine Quelle nicht mehr kennt: nicht löschen, sondern die
  Quelle als „nicht mehr vorhanden" markieren — die Retention aus 0.2 räumt später auf.
- Konfigurierbare Quellen-Reihenfolge in den Options, falls jemand die E-Mail bevorzugt.
- Nachvollziehbarkeit im Log: Auf Debug-Level protokollieren, welche Quelle welches Feld
  gesetzt oder überschrieben hat.

**Gehört NICHT dazu:**
- Weitere APIs, Karte (Phase 4).

## Hinweise

- Dieser Step ist ausdrücklich ein Härtungs-Step: Er wird erst dann sinnvoll bearbeitet, wenn
  3.1 mit echten Daten gelaufen ist. Die dabei beobachteten Konflikte gehören als Testfälle ins
  Repo, damit sie nicht wiederkehren.
- Das Zusammenführen zweier Sendungen ist nicht umkehrbar — es braucht ein klares Kriterium,
  keine Ähnlichkeitsschätzung. Die Trackingnummer allein reicht als Kriterium, die
  **Bestellnummer allein nicht**: Bei Amazon tragen alle Pakete einer Bestellung dieselbe, ein
  Merge darüber würde vier Pakete zu einem verschmelzen. Das tragfähige Kriterium ist das Paar
  **Bestellnummer + `items`** (die vollständige Artikelliste, in 1.3 genau dafür eingeführt):
  - Die Artikelmenge ist über alle drei Mailfamilien identisch, auch wenn Amazon sie je Mail in
    anderer Reihenfolge aufzählt — deshalb als **Menge** vergleichen, nicht als Liste.
  - Ein Sammelpaket vereinigt mehrere Blöcke der Bestellbestätigung. Die Zuordnung ist also
    **n:1**: Eine Sendung, deren Artikelmenge Teilmenge einer anderen mit gleicher Bestellnummer
    ist, gehört in diese hinein.
  - Die „Abgeholt"-Mail nennt gar keinen Artikel. Sie braucht das Kriterium nicht, weil sie
    schon über die `shipmentId` eingerastet ist — aber der Fall zeigt, dass eine leere
    Artikelliste nie ein Ausschlusskriterium sein darf.
- Zeitfenster-Konflikte nicht mitteln: Die frischere Angabe gewinnt vollständig, sonst entstehen
  Fenster, die keine Quelle je gemeldet hat.
- Die Herkunftsinformation darf die Attributliste aus 2.3 nicht aufblähen — sie gehört in die
  vollständige Darstellung, nicht in die kompakte.

## Akzeptanzkriterien

- Widersprüchliche Zeitfenster ergeben genau das Fenster der frischeren Quelle.
- Zustellmeldung der API vor der Zustellmail führt nicht dazu, dass die Sendung wieder auf
  „unterwegs" zurückfällt — und umgekehrt genauso.
- Zwei getrennt geführte Sendungen, die sich als dieselbe erweisen, werden zusammengeführt und
  behalten die ältere ID.
- Die Amazon-Fixtures aus 1.3 ergeben nach diesem Step so viele Sendungen, wie es Pakete gibt:
  `bestellbestaetigung.eml` + `versandbestaetigung.eml` + `locker_bereit.eml` +
  `locker_abgeholt.eml` ergeben **eine** Sendung mit Bestellnummer, Artikel, Abholort und
  Status `delivered` — nicht vier. Eine Trackingnummer hat sie nicht; Amazon vergibt keine.
- `versand_sammelpaket.eml` und `locker_sammelpaket.eml` nennen dieselben zwei Artikel in
  umgekehrter Reihenfolge und ergeben ebenfalls genau eine Sendung.
- Eine Sendung, die 17track nicht mehr kennt, verschwindet nicht sofort, sondern über die
  Retention.
- `get_shipments` gibt für ein Beispielfeld die liefernde Quelle mit aus.
- Umgestellte Quellen-Reihenfolge in den Options kehrt das Ergebnis eines Konflikts um.
- Die in 3.1 real beobachteten Konflikte sind als Testfälle abgelegt.

## Checkliste

- [ ] merge.py anhand realer Konflikte überarbeitet
- [ ] Feld-Herkunft gespeichert und über get_shipments abrufbar
- [ ] Zusammenführen mehrerer Sendungen mit klarem Kriterium, ältere ID gewinnt
- [ ] Amazon: die drei Sendungsfamilien einer Bestellung fallen zusammen (n:1 über `items`)
- [ ] Verschwundene Quelle markiert statt gelöscht
- [ ] Quellen-Reihenfolge in den Options konfigurierbar
- [ ] Debug-Protokollierung der Feldherkunft
- [ ] Tests grün, reale Konfliktfälle als Testfälle abgelegt
