# geo-import-pipeline

Importiert WFS- und CSV-Quellen per Konfiguration in bestehende PostGIS-Tabellen. Das Tool ersetzt die Daten einer Quelle atomar, zählt die fachlichen Änderungen und schreibt nach jedem Lauf einen maschinenlesbaren Bericht.

**Status:** Eigenprojekt der Inan Software OG, entstanden 11/2026, Open Source unter Apache-2.0. **Bisher kein produktiver Einsatz.** Der Lauf gegen die echten Dienste fand bisher nur in einer lokalen Docker-Umgebung statt. Die automatischen Tests laufen zusätzlich bei jedem Push über GitHub Actions.

## Was das Tool tut

- **WFS 2.0.0 und 1.1.0**, GML-Antworten, seitenweiser Abruf mit Pause und Wiederholung bei Fehlern
- **CSV** per URL, jährlichem URL-Muster oder abgelegter Datei, auch aus ZIP-Archiven einschließlich Deflate64
- **Feldzuordnung, Typumwandlung, Filter** in YAML, je Quelle eine Datei. Sonderformate über eigene Adapter
- **Geometrie in PostGIS:** Umrechnung nach EPSG:4326, 2D, Reparatur mit `ST_MakeValid`, Anpassung an den Zieltyp
- **Atomarer Ersatz je Quelle** in einer Transaktion. Attribute werden ersetzt, Geometrien nur aktualisiert oder ergänzt, nie gelöscht
- **Keine Strukturänderung an Zieltabellen.** Eigene Steuerdaten liegen nur im Schema `pipeline_meta`
- **Löschen nur nach nachweislich vollständigem Abruf.** Sonst schlägt die Quelle fehl und bleibt unverändert
- **Fachlicher Vergleich vor dem Ersatz:** neu, geändert, gelöscht je Schlüssel
- **Bericht** als JSON und lesbare Textdatei nach jedem Lauf, auch bei Fehlern
- **Fehlerisolierung:** Eine ausfallende Quelle hält die übrigen nicht auf
- **Zeitsteuerung** je Quelle per Cron-Ausdruck, außerdem manueller Start

## Schnellstart

Voraussetzung ist Docker mit Compose.

```sh
cp .env.example .env        # POSTGRES_PASSWORD setzen
mkdir -p reports
docker compose up -d db
docker compose run --rm --build pipeline run --source geobasis-bb-gemeinden
```

Die Ergebnisse stehen in den Beispieltabellen `example.*`, der Bericht in `reports/`.

Die mitgelieferte INKAR-Quelle lädt ein Archiv von rund 434 MB und liest 63 Millionen Zeilen. Sie braucht mehrere Minuten.

## Nachweis

End-to-End-Lauf vom 08.11.2026 auf einer frischen Datenbank in der lokalen Compose-Umgebung, PostgreSQL 18.6, PostGIS 3.7.0, gegen die echten Dienste.

| Lauf | Quelle | Gelesen | Übernommen | Neu | Geändert | Gelöscht | Dauer |
|---|---|---:|---:|---:|---:|---:|---:|
| 1, frische Datenbank | Gemeinden Brandenburg (WFS `app:gem`) | 413 | 413 | 413 | 0 | 0 | 8,6 s |
| 1, frische Datenbank | INKAR 2025, Gemeinden Brandenburg | 63.426.864 | 1.285.774 | 1.285.774 | 0 | 0 | 199,4 s |
| 2, unveränderte Quellen | Gemeinden Brandenburg | 413 | 413 | 0 | 0 | 0 | 10,2 s |
| 2, unveränderte Quellen | INKAR 2025 | 63.426.864 | 1.285.774 | 0 | 0 | 0 | 216,3 s |

Alle 413 Geometrien sind gültig und liegen als `MULTIPOLYGON` in EPSG:4326 vor. Alle 413 Gemeindeschlüssel aus INKAR finden ihre Geometrie. Es gab weder Warnungen noch ausgelassene Zeilen. Der Ablauf umfasst außerdem 89 automatische Tests mit selbst geschriebenen Testdaten.

## Grenzen

- Kein produktiver Einsatz, keine Erfahrungswerte aus dem Dauerbetrieb.
- Das Laden großer Archive über den Adapter ist nur mit kleinen Testdaten geprüft. Das INKAR-Archiv lag beim Nachweis bereits im Cache. Die Prüfung auf Änderungen per `HEAD` lief gegen den echten Server.
- WFS-Antworten müssen GML sein. GeoJSON wird nicht gelesen.
- Der WFS-Pfad ist gegen einen deegree-Dienst geprüft (Objektarten `app:gem` über 2.0.0, `app:krs` über 1.1.0). Andere WFS-Server sind nicht getestet.
- Geprüft wurde nur gegen die Beispieltabellen in `examples/`. Die Einbindung in ein bestehendes Compose-Setup ist beschrieben, aber nicht erprobt.
- Ein Lauf über ganz Deutschland ist nicht getestet. Die Gesamtdatei wird zwar vollständig gelesen, übernommen wurde aber nur Brandenburg.
- Der Bericht nennt je Änderungsart die Anzahl und bis zu 50 Beispielschlüssel, keine vollständige Liste.
- Das Tool bewahrt keine früheren Datenstände auf.
- Die automatischen Tests über GitHub Actions nutzen selbst geschriebene Testdaten und rufen die echten Dienste nicht ab.

## Dokumentation

- [Installation](docs/INSTALLATION.md)
- [Betrieb, Fehlerverhalten und Bericht](docs/BETRIEB.md)
- [Neue Quelle anbinden](docs/NEUE-QUELLE.md)
- [Datenmodell](docs/DATENMODELL.md)
- [Datenquellen und Namensnennung](docs/DATENQUELLEN.md)

## Technik

Python 3.12 im Linux-Container, `httpx`, `lxml`, `asyncpg`, `pydantic`, `PyYAML`, `APScheduler`. Die Geometriearbeit übernimmt PostGIS. Alle Komponenten und Lizenzen stehen in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).

## Lizenz und Daten

Der Code steht unter der [Apache-2.0-Lizenz](LICENSE), siehe [NOTICE](NOTICE). Copyright Inan Software OG.

Das Repository enthält keine Quelldaten. Abgerufene Daten unterliegen den Bedingungen der Anbieter, zum Beispiel:

- Verwaltungsgrenzen: © GeoBasis-DE/LGB, dl-de/by-2-0, (Daten geändert)
- INKAR: Datenbasis INKAR, BBSR Bonn, dl-de/by-2-0

Details stehen in [docs/DATENQUELLEN.md](docs/DATENQUELLEN.md).
