# Step 0.1 — Repo-Skeleton, HACS-Metadaten & Qualitäts-Gate

> Kontext: zuerst `planning.md` in `setup-docs/` lesen.

## Ziel

Ein von HA ladbares, leeres Integrations-Gerüst mit funktionierendem Qualitäts-Gate und CI,
auf dem alle weiteren Steps aufbauen.

## Scope

**Gehört dazu:**
- `pyproject.toml`: Projekt-Metadaten, dev-Dependencies (`pytest`, `pytest-asyncio`,
  `pytest-homeassistant-custom-component`, `ruff`), `[tool.ruff]` (line-length 100,
  Regel-Auswahl analog `ha-computer-control`, Ignores mit kurzer deutscher Begründung),
  `[tool.pytest.ini_options]` mit `asyncio_mode = "auto"`.
- `check.cmd` im Repo-Root: `ruff format` → `ruff check` (mit `--fix`, wenn Parameter übergeben)
  → `pytest -q`; Abschlussmeldung `ERGEBNIS: alles gruen` bzw. `FEHLGESCHLAGEN`.
- `custom_components/delivery_tracker/` mit `manifest.json` (Domain `delivery_tracker`,
  `config_flow: true`, `iot_class: cloud_polling`, `version: "0.0.0-dev"`, Requirements,
  `documentation`/`issue_tracker`), `const.py` (DOMAIN, Plattform-Liste, Storage-Key),
  `__init__.py` mit leerem `async_setup_entry`/`async_unload_entry`.
- `config_flow.py` als Minimal-Flow: nur ein Namensfeld, legt einen Entry an
  (die echten Subentries kommen in 2.1).
- `strings.json` + `translations/de.json` und `translations/en.json` passend zum Minimal-Flow.
- `hacs.json` (Name, `zip_release`, `filename`, Mindest-HA-Version).
- `.github/workflows/`: `hassfest.yml`, `hacs.yml` (Validierung), `tests.yml` (ruff + pytest),
  `release.yml` (Version aus dem Tag in `manifest.json` schreiben, ZIP bauen und anhängen).
- `tests/conftest.py` mit `enable_custom_integrations`-Fixture, ein Rauchtest, der den Entry
  auf- und wieder abbaut.
- `README.md` (Kurzbeschreibung, Installationshinweis über HACS-Custom-Repository),
  `.gitignore` ergänzen, Git-Repo initialisieren, erster Commit.

**Gehört NICHT dazu:**
- Datenmodell (0.2), Plugins (0.3), IMAP (1.1), echte Entities (Phase 2).

## Hinweise

- `pytest-homeassistant-custom-component` ist an eine HA-Version gepinnt — die Version bewusst
  wählen und in `hacs.json`/`manifest.json` konsistent halten.
- `setup-docs/` gehört nicht ins Release-ZIP; im `release.yml` entsprechend ausschließen.
- Die Referenz-Integration unter `setup-docs/Home-Assistant-Mail-And-Packages/` darf weder
  von ruff noch von pytest erfasst werden — in beiden Konfigurationen ausschließen.

## Akzeptanzkriterien

- `uv pip install -e ".[dev]"` läuft fehlerfrei durch.
- `check.cmd` läuft grün durch (Format, Lint, Tests).
- Die Integration lässt sich in einer HA-Instanz unter „Integration hinzufügen" finden,
  anlegen und wieder entfernen, ohne Fehler im Log.
- Alle vier GitHub-Workflows laufen auf einem Push grün.

## Checkliste

- [x] pyproject mit ruff- und pytest-Konfiguration
- [x] check.cmd mit Sammelergebnis
- [x] manifest.json, const.py, minimaler Config-Flow, Übersetzungen de/en
- [x] hacs.json und vier GitHub-Workflows
- [x] Rauchtest Setup/Unload grün
- [x] README, .gitignore, Git-Repo mit erstem Commit

**Abweichung vom Scope:** `pytest` läuft nicht nativ unter Windows. Home Assistant importiert
seit 2025.10 in `runner.py` die Unix-Module `fcntl` und `resource`; `pytest-homeassistant-custom-component`
0.13.348 pinnt HA 2026.7.4. `check.cmd` führt `ruff` deshalb nativ aus und startet `pytest` im
Container aus `Dockerfile.test` (Python 3.14-slim, Repo als Bind-Mount) — dieselbe Plattform wie die
GitHub-CI, die weiterhin nativ auf Ubuntu läuft. Erster Build ~2 min, danach ~6 s pro Lauf.
Python-Mindestversion dadurch 3.14 statt 3.13.

**Noch zu prüfen:** GitHub-Handle in `manifest.json` (`codeowners`) und die
`documentation`/`issue_tracker`-URLs sind geraten (`dominoxp`) — vor dem ersten Push anpassen.
Die vier Workflows sind mangels Remote noch nicht auf GitHub gelaufen.
