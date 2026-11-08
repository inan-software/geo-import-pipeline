# Installation

Voraussetzung ist Docker mit Compose. Python muss auf dem Rechner nicht installiert sein.

## Eigenständig starten

```sh
cp .env.example .env        # POSTGRES_PASSWORD setzen
mkdir -p reports
docker compose up -d db
docker compose run --rm --build pipeline run --source geobasis-bb-gemeinden
```

Compose startet PostGIS und legt beim ersten Start die Beispiel-Zieltabellen aus [`examples/target_schema.sql`](../examples/target_schema.sql) an. Berichte landen in `reports/`, Downloads im Volume `geo_import_cache`.

Der Container läuft als Benutzer mit der UID 10001. Auf Linux-Hosts braucht `reports/` deshalb Schreibrechte für diese UID, zum Beispiel `sudo chown 10001 reports`.

> **Achtung:** `docker compose down -v` löscht auch `geo_import_cache`. Die Archive müssten dann neu geladen werden. Um nur die Datenbank zurückzusetzen: `docker compose down` und `docker volume rm geo-import-pipeline_db_data`.

## In ein bestehendes Compose-Setup einbinden

Der Dienst braucht nur Zugang zu einer PostGIS-Datenbank. Die Zieltabellen müssen dort bereits vorhanden sein.

```yaml
services:
  geo-import-pipeline:
    build: ./geo-import-pipeline
    environment:
      POSTGRES_HOST: mein_db_dienst
      POSTGRES_PORT: "5432"
      POSTGRES_DB: ${POSTGRES_DB}
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - ./pipeline-config:/app/config:ro
      - ./pipeline-reports:/data/reports
      - pipeline_cache:/data/cache
    depends_on:
      mein_db_dienst:
        condition: service_healthy

volumes:
  pipeline_cache:
```

Getestet ist das bisher nur mit dem mitgelieferten Compose-Setup.

Beim ersten Lauf legt das Tool das Schema `pipeline_meta` an. Dafür muss der Datenbankbenutzer Schemas anlegen dürfen. Ist die Erweiterung `postgis` noch nicht installiert, sind auch dafür Rechte nötig.

## Umgebungsvariablen

| Variable | Bedeutung | Standard |
|---|---|---|
| `POSTGRES_HOST`, `POSTGRES_PORT` | Datenbankdienst | `db`, `5432` |
| `POSTGRES_DB`, `POSTGRES_USER` | Datenbank, Benutzer | `geodata` |
| `POSTGRES_PASSWORD` | Passwort, Pflicht | |
| `POSTGIS_IMAGE` | PostGIS-Image im mitgelieferten Compose | `postgis/postgis` |
| `GIP_CONFIG_DIR` | Ordner mit `sources/*.yaml` | `/app/config` |
| `GIP_REPORT_DIR` | Ablage der Berichte | `/data/reports` |
| `GIP_CACHE_DIR` | Ablage der Downloads | `/data/cache` |
| `GIP_TIMEZONE` | Zeitzone der Zeitsteuerung | `UTC` |
| `GIP_USER_AGENT` | User-Agent der Abrufe | `geo-import-pipeline/0.1` |
| `GIP_LOG_LEVEL` | Protokollstufe | `INFO` |
| `GIP_PLUGIN_MODULES` | Zusätzliche Adapter-Module, kommagetrennt | |

Zugangsdaten gehören nur in die Umgebung. In YAML-Dateien lassen sich Werte mit `${VARIABLE}` aus der Umgebung einsetzen.

## Tests

```sh
docker compose --profile test run --rm --build tests
```

Die Integrationstests legen je Test eine eigene Wegwerf-Datenbank an und rufen keine externen Dienste auf.

Die Lizenzübersicht wird so erzeugt und geprüft:

```sh
docker compose --profile test run --rm --entrypoint python tests scripts/generate_licenses.py --output /repo/THIRD_PARTY_LICENSES.md
docker compose --profile test run --rm --entrypoint python tests scripts/generate_licenses.py --check --output /repo/THIRD_PARTY_LICENSES.md
```
