# Datenquellen der Beispielkonfiguration

Das Repository enthält keine Quelldaten. Die Dateien in `config/sources/` beschreiben nur, wo die Daten abgerufen werden und wie sie zuzuordnen sind. Für die abgerufenen Daten gelten die Bedingungen der jeweiligen Anbieter. Wer sie weiterverwendet, ist selbst dafür verantwortlich, diese einzuhalten.

## Verwaltungsgrenzen Brandenburg

- Dienst: `https://isk.geobasis-bb.de/ows/vg_wfs`, Objektart `app:gem`
- Lizenz: Datenlizenz Deutschland – Namensnennung – Version 2.0 (dl-de/by-2-0)
- Namensnennung: **© GeoBasis-DE/LGB, dl-de/by-2-0, (Daten geändert)**

## INKAR

- Anbieter: Bundesinstitut für Bau-, Stadt- und Raumforschung (BBSR)
- Abruf: `https://www.bbr-server.de/imagemap/inkar/download/inkar_{year}.zip`, Gesamtdatei mit 63,4 Millionen Zeilen im Jahrgang 2025
- Lizenz: Die [Datensatzseite des BBSR](https://www.bbsr.bund.de/BBSR/DE/daten-karten/raumentwicklung/2025/inkar.html) nennt die Datenlizenz Deutschland – Namensnennung – Version 2.0 (dl-de/by-2-0). Die Seite nennt keine bestimmte Formulierung für die Quellenangabe. Vor einer Weiterverwendung der Daten sollten die aktuellen Bedingungen des Anbieters geprüft werden.
- Namensnennung in der Beispielkonfiguration: **Datenbasis INKAR, BBSR Bonn, dl-de/by-2-0**

## Abrufverhalten

Das Tool ruft die Dienste nacheinander ab, seitenweise und mit einer Pause zwischen den Seiten. Der User-Agent enthält keine personenbezogenen Daten. Unveränderte Archive werden nicht erneut geladen.

Für Tests sollten nur Daten ohne Personenbezug wie Verwaltungsgrenzen verwendet werden.
