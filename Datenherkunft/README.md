# Datenherkunft und Datenerhebung

Dieser Ordner dokumentiert, woher die Eingangsdaten der Event-Study stammen
und wie sie erhoben und aufbereitet wurden. Die fertigen, für die Reproduktion
verwendeten Dateien liegen unter [`Daten/`](../Daten/). Die dort enthaltenen
Dateien wurden aus einem geprüften und eingefrorenen Referenzbestand übernommen;
die ursprünglichen Pfade und SHA-256-Prüfsummen stehen in
[`Daten/Herkunft.json`](../Daten/Herkunft.json).

## Untersuchungsstichprobe

Untersucht werden die Quartalsberichte von Apple (AAPL), Amazon (AMZN),
Alphabet (GOOGL), Meta (META) und Microsoft (MSFT). Für jedes Unternehmen
wurden die verfügbaren Quartalsberichte ab 2018 gesammelt. Der resultierende
Analysebestand umfasst 170 Ereignisse.

## Erhebung der Daten

### Earnings und Ereignistage

Die historischen Quartals-Earnings werden über die Funktion **EARNINGS** der
Alpha-Vantage-API abgerufen. Verwendet werden insbesondere:

- tatsächliches und erwartetes EPS,
- Veröffentlichungsdatum und Veröffentlichungszeit,
- Geschäftsjahresende und Quartalszuordnung.

Aus diesen Angaben werden die EPS-Überraschung, die prozentuale Überraschung,
der SUE-Wert und der für die Börsenhandelstage relevante Ereignistag
bestimmt. Die Geschäftsquartale werden unternehmensspezifisch abgeleitet
(unter anderem endet das Geschäftsjahr von Apple im September und das von
Microsoft im Juni). Manuelle Korrekturen der Veröffentlichungszeit werden in
den Akquise-Dateien separat dokumentiert, statt die API-Rohdaten zu
überschreiben.

### Earnings-Call-Transkripte

Die Calltexte werden über die Alpha-Vantage-Funktion
**EARNINGS_CALL_TRANSCRIPT** geladen. Die API erwartet das Geschäftsquartal;
deshalb wird der API-Parameter für jedes Unternehmen aus dem
Geschäftsjahresende berechnet. Für historische Meta-Berichte wird zusätzlich
die frühere Kennung `FB` als Fallback berücksichtigt.

Die Transkripte werden fortsetzbar abgerufen, auf Mindestlänge und
Vollständigkeit geprüft und zusammen mit Quelle, Abrufstatus und Prüfsummen
gespeichert. Die im Repository enthaltenen vollständigen Texte befinden sich
unter [`Daten/Transkripte/`](../Daten/Transkripte/). Die zugehörigen
Quellseiten und Audio-Nachweise sind in
[`Transkriptverzeichnisse/`](Transkriptverzeichnisse/) verzeichnet.

Zusätzlich wurden 57 Transkripte manuell gegen die jeweils verknüpfte
Audioquelle geprüft. Dabei wurden insbesondere die Zuordnung von Unternehmen,
Berichtsdatum und Call, die Vollständigkeit des Textes sowie auffällige
Abweichungen zwischen Audio und Transkript kontrolliert. Festgestellte
Korrekturen und Prüfentscheidungen sind in den Dateien
`Transkriptverzeichnisse/Hoerpruefungen.csv` und
`Transkriptverzeichnisse/Korrekturen.csv` festgehalten. Die Prüfungen dienen
damit als zusätzliche Qualitätssicherung neben den automatischen
Vollständigkeitsprüfungen und den SHA-256-Prüfsummen.

### Aktien- und Benchmarkdaten

Tägliche Kurse und Handelsvolumina werden mit dem Open-Source-Paket
`yfinance` aus Yahoo Finance geladen. Für die fünf Unternehmen werden
historische Kurs- und Volumendaten erhoben; **SPY** dient als Hauptbenchmark,
**QQQ** als Robustheitsbenchmark. Für die Renditeberechnung werden
split- und dividendenbereinigte Schlusskurse (`Adj Close`) verwendet.

Das Akquise-Skript prüft, ob die Daten jedes Ereignis-, Schätz- und
Nachlauf-Fenster vollständig abdecken. Rohdownloads und aufbereitete Dateien
werden getrennt gespeichert und lokal archiviert, damit die konkrete
Datengrundlage nachvollziehbar bleibt.

### Google Trends

Die Google-Trends-Rohdaten liegen in
[`google_trends_raw/`](google_trends_raw/). Sie enthalten wöchentliche,
relative Suchinteressenwerte von 0 bis 100 für die jeweiligen Unternehmen.
Die Suchbegriffe sind grundsätzlich `AAPL stock`, `AMZN stock`,
`GOOGL stock`, `MSFT stock` sowie für Meta bis zum Rebranding `Facebook
stock` und anschließend `Meta stock`.

Getrennte Google-Trends-Zeitsegmente desselben Suchbegriffs werden über ihren
Überlappungszeitraum robust skaliert. Werte unter 1 werden als 0,5 kodiert.
Für jedes Ereignis wird die Kalenderwoche des Berichtstags mit bis zu acht
Vorwochen verglichen. Das resultierende Maß ist die logarithmische Veränderung
des Suchinteresses gegenüber dem Median der Vorwochen.

## Aufbereitung und Qualitätssicherung

Die Skripte in [`Datenakquise/`](Datenakquise/) bilden die einzelnen Schritte
ab:

1. Earnings laden und die Event-Study-Masterdatei erstellen.
2. Earnings-Call-Transkripte abrufen und auf Vollständigkeit prüfen.
3. Kurs- und Volumendaten inklusive Benchmarkdaten laden.
4. Google-Trends-Rohdaten zusammenführen und eventbezogene Variablen bilden.
5. Transkripte in englische Sätze zerlegen und jeden Satz mit
   `yiyanghkust/finbert-tone` klassifizieren.
6. Die Satzbewertungen zu `TONE` aggregieren:
   `(positive Sätze - negative Sätze) / alle Sätze`.

Jeder Abruf und jede Weiterverarbeitung soll die Rohdaten erhalten und
Prüfsummen beziehungsweise Manifestdateien erzeugen. Fehlende Beobachtungen
werden nicht künstlich ergänzt. Die vollständige Analyse verwendet den
eingefrorenen Bestand; FinBERT muss für die Standardreproduktion nicht erneut
ausgeführt werden.

## Reproduzierbarkeit und Grenzen

API- und Webdaten können sich nach dem Abruf ändern. Deshalb sind die
verwendeten Roh- beziehungsweise Ergebnisdateien, Quellenangaben und
Prüfsummen im Repository beziehungsweise in den Verzeichnissen zur
Transkriptprüfung festgehalten. Für neue Abrufe werden die in den Skripten
beschriebenen API-Schlüssel und die jeweiligen Nutzungsbedingungen der
Datenanbieter benötigt. Google-Trends-Werte sind relative Indizes und keine
absoluten Suchvolumina.
