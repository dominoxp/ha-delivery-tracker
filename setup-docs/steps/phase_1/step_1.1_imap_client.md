# Step 1.1 — IMAP-Client mit IDLE-Push und Polling-Fallback

> Kontext: `planning.md` lesen. Setzt Step 0.3 voraus.

## Ziel

Die erste echte Datenquelle: Ein robuster IMAP-Client, der neue Mails zuverlässig liefert —
per Push, wenn der Server es kann, sonst per Polling — und keine Mail doppelt oder gar nicht
verarbeitet.

## Scope

**Gehört dazu:**
- `sources/imap_source.py` als `TrackingSource`-Implementierung auf Basis von `aioimaplib`
  (dieselbe Bibliothek wie HA Core `imap`).
- Verbindungsaufbau: SSL/STARTTLS/unverschlüsselt, Login, Ordnerauswahl, Capability-Prüfung.
- IDLE-Push, wenn der Server `IDLE` anbietet, mit periodischer Erneuerung (Server trennen
  IDLE nach ca. 29 Minuten). Kein IDLE → Polling in konfigurierbarem Intervall.
- Reconnect mit exponentiellem Backoff und Obergrenze; Verbindungsverlust ist Normalbetrieb,
  kein Fehlerzustand. Aktueller Verbindungsstatus wird nach außen sichtbar gemacht
  (für den Diagnose-Sensor in 2.3).
- Fortschrittsverwaltung: verarbeitete UIDs merken, `UIDVALIDITY` prüfen und bei Änderung den
  Fortschritt zurücksetzen. Verarbeitete Mails werden mit `\Seen` markiert.
- Erststart: Lookback über N Tage (konfigurierbar), danach nur noch neue Mails.
- Mail-Abruf als Rohbytes; das Parsen passiert erst in 1.2.
- `tests/fake_imap.py`: ein echter Fake-IMAP-Server (kein Mock) analog zu `fake_ha.py` im
  Schwester-Projekt, der Login, SELECT, SEARCH, FETCH, STORE und optional IDLE beherrscht und
  Verbindungsabbrüche gezielt auslösen kann.

**Gehört NICHT dazu:**
- MIME-Verarbeitung und Carrier-Erkennung (1.2), Config-Flow-Anbindung (2.1) — die
  Zugangsdaten werden vorerst als einfaches Konfigurations-Objekt übergeben.

## Hinweise

- Nur `\Seen` setzen, nachdem die Mail erfolgreich verarbeitet wurde — sonst geht bei einem
  Absturz mitten in der Verarbeitung eine Sendung verloren.
- Große Postfächer nicht auf einmal laden: Abruf in Blöcken mit Obergrenze pro Durchlauf.
- Gmail und Outlook drosseln bei zu vielen Verbindungen; pro Postfach genau eine Verbindung
  halten und wiederverwenden.
- Zugangsdaten dürfen nie im Log landen, auch nicht auf Debug-Level.
- Verhalten bei fehlendem Ordner, falschem Passwort und Serverabbruch bewusst festlegen und
  im Code dokumentieren — 2.1 und 2.6 bauen darauf auf.

## Akzeptanzkriterien

- Gegen den Fake-Server: Login, Abruf ungelesener Mails, `\Seen`-Markierung, kein zweiter
  Abruf derselben Mail im nächsten Durchlauf.
- IDLE-Pfad: neue Mail auf dem Fake-Server löst ohne Polling einen Abruf aus.
- Server ohne IDLE-Capability wechselt automatisch in den Polling-Modus.
- Verbindungsabbruch mitten im Betrieb führt zu Reconnect mit Backoff, danach läuft der Abruf
  wieder — ohne verlorene oder doppelte Mails.
- Geänderte `UIDVALIDITY` setzt den Fortschritt zurück, ohne Absturz.
- Falsches Passwort ergibt einen klar unterscheidbaren Fehler (kein Reconnect-Dauerlauf).

## Checkliste

- [x] imap_source.py mit Verbindung, Login und Ordnerauswahl
- [x] IDLE-Push mit Erneuerung, Polling-Fallback
- [x] Reconnect mit Backoff und sichtbarem Verbindungsstatus
- [x] UID-Fortschritt, UIDVALIDITY-Behandlung, \Seen nach Erfolg
- [x] Lookback beim Erststart
- [x] fake_imap.py als echter Testserver
- [x] Tests grün
