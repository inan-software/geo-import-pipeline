# Datenmodell

## Zieltabellen

Die Zieltabellen gehören der jeweiligen Anwendung. Das Tool liest ihre Struktur und schreibt nur Daten hinein. Welche Tabellen und Spalten gemeint sind, steht in der Konfiguration.

Zum Ausprobieren legt [`examples/target_schema.sql`](../examples/target_schema.sql) drei Beispieltabellen an:

| Tabelle | Inhalt |
|---|---|
| `example.regions` | Geometrien, Schlüssel `region_key` |
| `example.indicator_values` | Kennzahlen je Region, Zeitbezug und Indikator, Spalte `source` |
| `example.dataset_metadata` | Eine Zeile je Datensatz |

Kennzahlen und Geometrien sind nur über den fachlichen Schlüssel verbunden, nicht über Fremdschlüssel. So bleibt jede Quelle einzeln austauschbar.

## Schema `pipeline_meta`

Das ist das einzige Schema, das das Tool anlegt und verändert. Die Definition steht in [`migrations/`](../migrations/).

| Objekt | Inhalt |
|---|---|
| `schema_migrations` | Ausgeführte Migrationen |
| `runs` | Ein Eintrag je Lauf: Auslöser, Zeiten, Status, Pfad des Berichts |
| `source_runs` | Ein Eintrag je Quelle und Lauf: Status, Zeilen, neu/geändert/gelöscht, Warnungen, Fehler |
| `source_state` | Letzter importierter Stand je Quelle: Hash des Schlüssels, Klartext des Schlüssels, Hash des Inhalts |
| `stg_*` | Arbeitstabellen, die nur während eines Laufs bestehen |
| `process_gml(...)` | Geometrieverarbeitung in PL/pgSQL. Löst bei fehlerhaften Einzelobjekten keinen Abbruch aus, sondern meldet einen Status |

## Vergleich

Der Vergleich läuft gegen `source_state` und nicht gegen die Zieltabellen, weil Schlüsselteile dort nicht als Spalte vorhanden sein müssen.

- Der **Schlüssel-Hash** entsteht aus den `key`-Feldern nach Umwandlung und Auffüllung.
- Der **Inhalts-Hash** entsteht aus allen übrigen Feldern. Bei Geometrie-Quellen fließen die Quellkoordinaten ein. Ein Update der Umrechnungsbibliothek oder wechselnde `gml:id`-Werte erzeugen daher keine scheinbaren Änderungen.

Frühere Datenstände werden nicht aufbewahrt. Nachvollziehbar sind die Änderungen über den Bericht und `source_runs` je Lauf.

## Ersetzen der Daten

| Zieltyp | Verhalten |
|---|---|
| `attributes` | Alle Zeilen mit `source_column = source_value` werden gelöscht und neu eingefügt. Andere Quellen bleiben unberührt |
| `geometry` | Zeilen werden nach Schlüssel aktualisiert oder eingefügt. Es wird nichts gelöscht, weil Geometrien quellenübergreifend genutzt werden können |

Zieldaten, `source_state` und Metadaten ändern sich in einer gemeinsamen Transaktion. Lesende Zugriffe sehen entweder den alten oder den neuen Stand.
