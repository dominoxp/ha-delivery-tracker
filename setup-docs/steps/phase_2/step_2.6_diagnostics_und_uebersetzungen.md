# Step 2.6 — Diagnostics, Repairs & Übersetzungen

> Kontext: `planning.md` lesen. Setzt Step 2.5 voraus.

## Ziel

Probleme sind ohne Log-Wühlen auffindbar, und die Integration ist in Deutsch und Englisch
vollständig übersetzt — der Stand, ab dem sie sinnvoll benutzbar ist.

## Scope

**Gehört dazu:**
- `diagnostics.py`: Download auf Entry- und Device-Ebene mit Konfiguration (ohne Geheimnisse),
  Zustand und letztem Fehler je Quelle, geladenen Carrier-Plugins, Anzahl Sendungen je Status
  und Carrier, Anzahl seit dem Start verworfener Mails, Zeitpunkt der letzten Aktualisierung.
- Konsequente Maskierung über `async_redact_data`: Passwörter, API-Schlüssel, Benutzernamen,
  Mailadressen, Trackingnummern, Adressdaten, Abholcodes.
- Repairs-Einträge für die Fälle, die der Nutzer selbst beheben muss: dauerhaft fehlgeschlagene
  IMAP-Anmeldung, verschwundener Ordner, abgelaufener oder aufgebrauchter API-Schlüssel.
  Jeder Eintrag verweist auf den passenden Reconfigure-Schritt aus 2.1.
- Zähler für nicht zugeordnete Mails und für Sendungen ohne Adresszuordnung, sichtbar in den
  Diagnostics — damit erkennbar wird, dass ein Carrier-Regelsatz oder eine Adressregel fehlt.
- Vervollständigung von `strings.json`, `de.json` und `en.json` über alle Flows, Entities,
  Actions, Fehler und Repairs.
- Ein Test, der `strings.json` gegen beide Übersetzungsdateien abgleicht und bei fehlenden oder
  überzähligen Schlüsseln fehlschlägt.

**Gehört NICHT dazu:**
- 17track-spezifische Diagnose (kommt in 3.1 dazu), Karte (Phase 4).

## Hinweise

- Diagnostics landen in Fehlerberichten auf GitHub. Im Zweifel mehr maskieren als weniger;
  für die Fehlersuche reicht meist Länge und Format statt des echten Werts.
- Repairs nur für Dinge anlegen, die der Nutzer tatsächlich beheben kann — ein kurzzeitig nicht
  erreichbarer Mailserver ist kein Repair, sondern Normalbetrieb mit Reconnect.
- Der Zähler „verworfene Mails" ist die wichtigste Kennzahl beim Erweitern um neue Carrier;
  zusammen mit `test_parser` aus 2.5 ergibt er den Arbeitsablauf für neue Regelsätze.
- Der Übersetzungs-Abgleichtest verhindert die häufigste Fehlerklasse in HA-Integrationen —
  er gehört fest ins Repo, nicht nur in diesen Step.

## Akzeptanzkriterien

- Der Diagnostics-Download enthält alle genannten Abschnitte und **kein** Passwort, keinen
  API-Schlüssel, keine vollständige Adresse und keine vollständige Trackingnummer
  (Test prüft gezielt auf die Testwerte).
- Dauerhaft fehlgeschlagene IMAP-Anmeldung erzeugt einen Repair-Eintrag, der nach erfolgreicher
  Neukonfiguration wieder verschwindet.
- Der Zähler verworfener Mails steigt bei einer nicht zuordenbaren Fixture-Mail.
- Der Übersetzungs-Abgleichtest ist grün und schlägt bei einem absichtlich entfernten Schlüssel
  fehl.
- Die Integration lässt sich in einer echten HA-Instanz auf Deutsch und Englisch vollständig
  ohne Platzhaltertexte bedienen.

## Checkliste

- [x] diagnostics.py mit allen Abschnitten
- [x] Maskierung aller personenbezogenen und geheimen Werte
- [x] Repairs für Anmeldung, Ordner und API-Schlüssel
- [x] Kennzahlen für verworfene Mails und fehlende Adresszuordnung
- [x] strings.json, de.json, en.json vollständig
- [x] Übersetzungs-Abgleichtest
- [ ] Tests grün, manuell in echter HA-Instanz geprüft
