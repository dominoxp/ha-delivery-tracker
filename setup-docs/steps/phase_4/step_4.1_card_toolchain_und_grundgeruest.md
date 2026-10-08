# Step 4.1 — Card-Toolchain & Grundgerüst

> Kontext: `planning.md` lesen. Setzt Step 3.2 voraus.

## Ziel

Eine eigene Lovelace-Karte ist gebaut, ausgeliefert und in HA registriert — noch ohne
Funktionsumfang, aber vollständig in Build, Release und Qualitäts-Gate eingebunden.

## Scope

**Gehört dazu:**
- `card/` mit TypeScript, Lit und Vite: `package.json`, `tsconfig.json`, Vite-Konfiguration mit
  Ausgabe als ein einzelnes ES-Modul `delivery-tracker-card.js`, ESLint und Prettier.
- Registrierung als Custom Element inklusive Eintrag in `window.customCards`, damit die Karte
  in der Kartenauswahl der Oberfläche erscheint.
- Grundgerüst der Karte: Konfiguration entgegennehmen und validieren, `hass`-Objekt verarbeiten,
  nur bei relevanten Änderungen neu rendern, Fehlerdarstellung bei ungültiger Konfiguration
  oder fehlender Entity.
- Ausgabe des Builds in ein Verzeichnis, das HACS als Frontend-Ressource ausliefert; `hacs.json`
  entsprechend ergänzen und im `release.yml` aus 0.1 die gebaute Datei ins Release-ZIP aufnehmen.
- Erweiterung von `check.cmd`: läuft zusätzlich Lint und Build der Karte, wenn Node verfügbar ist,
  und meldet das Ergebnis im Sammelergebnis. Fehlendes Node führt zu einem Hinweis, nicht zum
  Fehlschlag der Python-Prüfungen.
- Erster Testlauf der Kartentests (Vitest) mit einem Rendertest des Grundgerüsts.
- README-Abschnitt zur Installation der Karte.

**Gehört NICHT dazu:**
- Sendungsliste, Filter und visueller Editor (4.2).

## Hinweise

- Die Karte darf keine externen Laufzeit-Abhängigkeiten nachladen; alles wird in die eine Datei
  gebündelt. HA-eigene Bausteine wie `ha-card` kommen aus dem Frontend selbst und werden nicht
  mitgebündelt.
- Vom HA-Frontend nur stabile, öffentlich dokumentierte Schnittstellen nutzen — interne
  Komponenten ändern sich zwischen Versionen ohne Vorwarnung.
- Die Karte muss in hellem und dunklem Theme funktionieren; ausschließlich HA-Theme-Variablen
  für Farben verwenden, keine festen Farbwerte.
- Zwei Toolchains im Repo bedeuten zwei Stellen für Versionen und Lint-Regeln — die
  Node-Konfiguration bleibt vollständig unter `card/`, nichts davon wandert ins Repo-Root.

## Akzeptanzkriterien

- `npm run build` unter `card/` erzeugt genau eine JavaScript-Datei ohne externe Abhängigkeiten.
- `check.cmd` läuft grün und umfasst Lint und Build der Karte; ohne installiertes Node läuft es
  weiterhin grün durch, mit Hinweis.
- Die gebaute Karte lässt sich in einer echten HA-Instanz als Ressource einbinden, erscheint in
  der Kartenauswahl und rendert eine leere Karte.
- Eine ungültige Konfiguration erzeugt eine lesbare Fehlermeldung in der Karte statt einer
  leeren Fläche.
- Die Karte sieht in hellem und dunklem Theme korrekt aus.
- Das Release-ZIP enthält die gebaute Kartendatei.

## Checkliste

- [x] card/ mit TypeScript, Lit, Vite, ESLint, Prettier
- [x] Registrierung als Custom Element mit customCards-Eintrag
- [x] Grundgerüst mit Konfigurationsvalidierung und Fehlerdarstellung
- [x] hacs.json und release.yml um die Frontend-Ressource ergänzt — hacs.json bleibt
  unverändert: HACS liefert bei einer Integration keine Frontend-Dateien aus und kennt dafür
  keinen Schlüssel. Stattdessen liefert die Integration die Karte selbst aus (`card.py`,
  statischer Pfad + `add_extra_js_url`); `release.yml` baut sie ins ZIP.
- [x] check.cmd um Card-Lint und -Build erweitert
- [x] Vitest mit Rendertest
- [ ] Manuell in echter HA-Instanz eingebunden, helles und dunkles Theme geprüft
