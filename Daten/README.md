# Eingaben und vollständige Transkripte

Die Tabellen wurden unverändert aus dem bereinigten, eingefrorenen Referenzbestand
übernommen. `Herkunft.json` dokumentiert ursprüngliche Pfade, SHA-256-Prüfsummen
und die FinBERT-Modellrevision.

| Datei | Inhalt |
|---|---|
| `prices.csv` | Tägliche Aktienkurse und Benchmark SPY. |
| `volume.csv` | Tägliches Handelsvolumen. |
| `events.csv` | 170 Quartalsberichte mit Ereignishandelstagen und Merkmalen für feste Effekte. |
| `earnings_history.csv` | EPS-Historie einschließlich der früheren Berichte zur SUE-Standardisierung. |
| `trends_wochen.csv` | Wöchentliche Google-Trends-Werte mit Exportzuordnung. |
| `finbert_saetze.csv` | Alle 80.262 Sätze mit Text, Ereigniszuordnung, Klasse und FinBERT-Wahrscheinlichkeiten. |
| `Transkripte/transcripts.csv` | 170 vollständige Calltexte mit Quellenangaben und ursprünglichen Prüfsummen. |

**[Alle vollständigen Transkripte einzeln lesen](Transkripte/README.md).**

Die Textdateien unter `Transkripte/` enthalten jeweils den vollständigen
analysierten Calltext. Die Satzdatei ergänzt die Aufteilung und gespeicherten
FinBERT-Bewertungen. Die Standardreproduktion aggregiert TONE daraus neu;
das Sprachmodell wird dabei nicht erneut ausgeführt.

`Referenz/` enthält ausschließlich Vergleichsergebnisse. Sie gehen nicht in die
Berechnung der Kennzahlen oder Regressionen ein. Andere Rohdatenpakete, Audios
und lokale Sicherungen sind nicht Teil dieses Repositories.
