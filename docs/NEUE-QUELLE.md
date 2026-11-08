# Eine neue Quelle anbinden

Eine WFS- oder CSV-Quelle braucht nur eine neue Datei in `config/sources/`. Der Code bleibt unverändert. Jede `*.yaml` beschreibt genau eine Quelle.

## Vorgehen

1. Felder an den echten Daten prüfen, beim WFS mit `DescribeFeatureType`, bei CSV über die Kopfzeile.
2. YAML anlegen, `docker compose run --rm pipeline validate-config` ausführen.
3. Einen Lauf starten und den Bericht lesen.

## Gemeinsame Einstellungen

| Schlüssel | Bedeutung |
|---|---|
| `id` | Eindeutiger Name aus Kleinbuchstaben, Ziffern, Bindestrichen |
| `type` | `wfs`, `csv` oder ein eigener Adapter |
| `enabled` | `false` schließt die Quelle von `run --all` und `schedule` aus |
| `schedule` | Cron-Ausdruck, ohne Angabe nur manuell |
| `key` | Felder, die zusammen einen Datensatz eindeutig machen |
| `fields` | Feldzuordnung, siehe unten |
| `filters` | Nur passende Datensätze übernehmen |
| `min_rows` | Mindestanzahl gültiger Zeilen, darunter gilt der Abruf als unvollständig |
| `duplicate_keys` | `fail` oder `warn`. Standard: `fail` beim WFS, `warn` bei CSV |
| `target` | Zieltabelle und Spaltenzuordnung |

### Felder

```yaml
fields:
  ags:   { path: ags, type: text, pad: 8, required: true }
  wert:  { path: Wert, type: float, required: true }
  stand: { path: Datum, type: date, date_format: "%d.%m.%Y" }
  quelle: { const: mein-dienst }
```

`path` ist der Spaltenname, beim WFS der lokale Elementname. Verschachtelte Elemente schreibt man `a/b`, Attribute `@id`. `type` ist `text`, `integer`, `float` oder `date`. `pad` füllt Text links mit Nullen auf. Schlüsselfelder sind immer Pflichtfelder.

Kennziffern immer als `text` führen, damit führende Nullen erhalten bleiben.

### Filter

```yaml
filters:
  - { field: raumbezug, equals: Gemeinden }
  - { field: kennziffer, prefix: "12" }
  - { field: kuerzel, in: [a, b] }
```

Mehrere Filter müssen alle zutreffen. Sie sehen den aufgefüllten Wert.

## WFS

```yaml
type: wfs
wfs:
  url: https://example.org/wfs
  version: 2.0.0          # oder 1.1.0
  typename: app:gebiet
  source_crs: EPSG:25833
  page_size: 100
  page_delay_s: 1.0       # Pause zwischen den Seiten
  geometry_path: geom
  # paging: true          # Standard: an bei 2.0.0, aus bei 1.1.0
  # sort_by: id           # für Dienste mit instabiler Reihenfolge
  # timeout_s, retries, backoff_s
```

WFS 1.1.0 kennt im Standard kein Paging. Dort lädt das Tool deshalb alles in einer Anfrage. Der Dienst aus dem Beispiel ignoriert `STARTINDEX` unter 1.1.0 tatsächlich. Wenn ein Dienst nur 1.1.0 anbietet und Paging beherrscht, lässt es sich mit `paging: true` einschalten.

Geometrien werden in PostGIS eingelesen, nach `target.srid` (Standard EPSG:4326) umgerechnet, auf 2D gebracht, bei Bedarf mit `ST_MakeValid` repariert und an `geom_type` angepasst. Unterstützt sind `POINT`, `LINESTRING`, `POLYGON` und die `MULTI`-Varianten. Die Antwort muss GML sein.

## CSV

```yaml
type: csv
csv:
  # genau eine der drei Angaben:
  url: https://example.org/daten.csv
  # url_template: https://example.org/daten_{year}.zip
  # path: /data/abgelegt/daten.csv
  year_lookback: 3        # nur bei url_template
  archive: zip            # oder none
  member: "daten*.csv"    # genau ein Treffer im Archiv
  delimiter: ";"
  decimal: ","
  # thousands: "."
  encoding: utf-8         # bei Datei mit BOM: utf-8-sig
  null_values: ["", "."]
```

Bei `url_template` nimmt das Tool das neueste vorhandene Jahr, ausgehend vom aktuellen. Eine unveränderte Datei (`ETag`, `Last-Modified`, Länge) wird nicht erneut geladen. Sie wird aber neu verarbeitet.

## Ziel

```yaml
# Attribute: alle Zeilen dieser Quelle werden ersetzt
target:
  kind: attributes
  table: schema.tabelle
  source_column: quelle
  source_value: MEINE-QUELLE
  columns: { region: ags, wert: wert }   # Zielspalte: Feld
```

```yaml
# Geometrien: nach Schlüssel aktualisieren oder einfügen, nie löschen
target:
  kind: geometry
  table: schema.gebiete
  geom_column: geom
  geom_type: MULTIPOLYGON
  columns: { ags: ags, name: name }      # Schlüsselfelder müssen zugeordnet sein
```

Optional schreibt `metadata` je Lauf eine beschreibende Zeile in derselben Transaktion:

```yaml
  metadata:
    table: schema.katalog
    key: { datensatz: meine-quelle }
    values: { herausgeber: Beispielamt, titel: "Daten ({version})" }
```

In `values` stehen `{source_id}`, `{run_date}` und `{version}` zur Verfügung. Hat die Tabelle eine Spalte `modified`, wird dort der Zeitpunkt des Laufs eingetragen.

Das Tool ändert die Struktur der Zieltabellen nie. Passt eine Spalte nicht, schlägt die Quelle fehl, bevor etwas abgerufen wird.

## Eigener Adapter für Sonderformate

Für Formate, die weder WFS noch CSV sind, genügt ein eigenes Modul. Die Kernlogik bleibt unberührt.

```python
from geo_import_pipeline.model import RawRecord
from geo_import_pipeline.sources import SourceAdapter, register

@register("mein-format")
class MeinAdapter(SourceAdapter):
    async def records(self):
        # self.source.options enthält den Abschnitt "options" der YAML-Datei.
        yield RawRecord(values={"spalte": "wert"}, ref="zeile 1")
```

Das Modul muss importierbar sein und wird über `GIP_PLUGIN_MODULES=mein_modul` geladen. Ein Abruf, der nicht nachweislich vollständig ist, muss `IncompleteFetchError` auslösen. Nur so bleiben die bisherigen Daten erhalten.

Geometrien werden derzeit nur für den Typ `wfs` verarbeitet.
