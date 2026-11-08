# Betrieb

## Befehle

```sh
docker compose run --rm pipeline <befehl>
```

| Befehl | Wirkung |
|---|---|
| `validate-config` | Prüft alle Quelldefinitionen |
| `list-sources` | Zeigt Quellen, Status und Zeitplan |
| `migrate` | Legt `pipeline_meta` an oder aktualisiert es |
| `run --source ID` | Importiert eine Quelle sofort |
| `run --all` | Importiert alle aktivierten Quellen nacheinander |
| `schedule` | Führt jede Quelle nach ihrem Zeitplan aus, bis der Prozess beendet wird |

`run` endet mit Exit-Code 0, wenn alle Quellen erfolgreich waren, sonst mit 1. Konfigurationsfehler ergeben 2.

Der Standardbefehl des Containers ist `schedule`. `docker compose up -d pipeline` startet also die Zeitsteuerung. Änderungen an der Konfiguration greifen nach einem Neustart des Dienstes.

## Zeitsteuerung

Jede Quelle hat in `schedule` einen eigenen Cron-Ausdruck, zum Beispiel `"0 3 1 * *"` für den Monatsersten um 03:00 in der Zeitzone `GIP_TIMEZONE`. Ohne `schedule` läuft eine Quelle nur manuell. Fällige Läufe werden nacheinander ausgeführt, damit die Dienste nie parallel abgefragt werden.

## Ablauf je Quelle

1. Eine Datenbanksperre verhindert parallele Läufe derselben Quelle.
2. Die Zieltabellen werden mit der Konfiguration abgeglichen.
3. Die Daten werden geladen. Beim WFS folgen auf eine `hits`-Abfrage die einzelnen Seiten mit Pause dazwischen. Bei Dateien prüft eine `HEAD`-Anfrage, ob ein erneuter Download nötig ist.
4. Die Zeilen werden gefiltert, umgewandelt und per Binär-COPY in eine Arbeitstabelle in `pipeline_meta` geschrieben.
5. Doppelte Schlüssel werden behandelt, Geometrien in PostGIS verarbeitet.
6. Der Abruf wird auf Vollständigkeit geprüft.
7. Der Vergleich mit dem letzten Stand wird berechnet.
8. In **einer** Transaktion werden Zieldaten, Stand und Metadaten ersetzt.
9. Lauf und Bericht werden geschrieben, auch bei Fehlern.

## Was als Fehler gilt

Eine Quelle schlägt fehl und bleibt **unverändert**, wenn einer dieser Fälle eintritt:

- Die Anzahl der WFS-Objekte weicht von `numberMatched` ab, oder der Dienst nennt keine Anzahl.
- Ein Schlüssel kommt beim WFS doppelt vor. Das kann auf instabiles Paging hindeuten.
- Der Download ist kürzer als angekündigt, oder ein ZIP-Archiv ist beschädigt.
- Konfigurierte CSV-Spalten fehlen oder die Datei hat die falsche Zeichenkodierung.
- Es bleiben weniger gültige Zeilen als `min_rows`.
- Die Zieltabelle passt nicht zur Konfiguration, oder die Datenbank meldet einen Fehler.

Fehler einer Quelle halten die übrigen Quellen nicht auf.

Einzelne Datensätze führen dagegen nur zu einer Warnung und werden ausgelassen:

| Warnung | Bedeutung |
|---|---|
| `missing_required_field` | Schlüssel oder Pflichtfeld fehlt |
| `conversion_failed` | Wert ließ sich nicht umwandeln |
| `malformed_row` | CSV-Zeile mit falscher Spaltenzahl |
| `duplicate_key` | Schlüssel doppelt in einer CSV-Quelle, erste Zeile bleibt |
| `geometry_repaired` | Geometrie mit `ST_MakeValid` repariert, Datensatz bleibt |
| `geometry_parse_error`, `geometry_empty`, `geometry_missing`, `geometry_transform_error`, `geometry_invalid`, `geometry_type_mismatch` | Geometrie nicht verwendbar, Datensatz ausgelassen |

## Bericht

Nach jedem Lauf entstehen in `GIP_REPORT_DIR` zwei Dateien: `import_<zeit>Z_<lauf>.json` und daneben eine lesbare `.txt`-Zusammenfassung.

```json
{
  "report_version": 1,
  "run_id": "…",
  "trigger": "manual",
  "status": "partial",
  "started_at": "…", "finished_at": "…", "duration_s": 12.3,
  "failed_sources": [{ "source_id": "…", "error": "…" }],
  "sources": [{
    "source_id": "…", "type": "wfs", "status": "success", "error": null,
    "started_at": "…", "finished_at": "…", "fetched_at": "…", "duration_s": 8.1,
    "source_version": "…",
    "rows": { "read": 0, "filtered_out": 0, "valid": 0, "skipped": 0, "written": 0 },
    "changes": { "new": 0, "changed": 0, "deleted": 0, "baseline": false, "geometries_retained": 0 },
    "change_samples": { "new": [], "changed": [], "deleted": [] },
    "warnings": [{ "code": "…", "message": "…", "count": 1, "samples": [] }],
    "warning_count": 1
  }]
}
```

`status` ist `success`, `partial` oder `failed`. Die Zahlen unter `changes` sind fachliche Änderungen gegenüber dem letzten Stand: `new` bedeutet, der Schlüssel war nicht vorhanden. `changed` heißt, der Schlüssel ist vorhanden, die Inhalte weichen ab. `deleted` heißt, der Schlüssel wird nicht mehr geliefert. Bei einer fehlgeschlagenen Quelle stehen sie auf 0. `baseline: true` kennzeichnet den ersten Import einer Quelle. `change_samples` enthält je Art bis zu 50 Beispielschlüssel.

Bei Geometrie-Quellen werden Objekte, die der Dienst nicht mehr liefert, als `deleted` gezählt, bleiben aber in der Zieltabelle (`geometries_retained`).

Dieselben Kennzahlen stehen je Lauf in `pipeline_meta.runs` und `pipeline_meta.source_runs`.
