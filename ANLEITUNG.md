# Event Study – verständliche Kernversion

**Grafiken ansehen:** `Interface/index.html` öffnen. Dort stehen alle 25
Abbildungen des Word-Arbeitsstands und die neu berechneten Diagramme bereit.
Die kurze Anleitung dazu liegt in `Interface/README.md`.

**Start: `REPRODUZIEREN.bat` doppelklicken.** Die Ergebnisse werden neu berechnet
und mit der geprüften Auswertung vom 28.10.2026 verglichen.
Auf einem anderen Computer zuerst Python 3.12 (64 Bit) installieren und
`INSTALLIEREN.bat` ausführen. Danach läuft die Analyse ohne Internet.

## Die drei Schritte im Code

| Datei | Aufgabe |
|---|---|
| `analyse.py` | Eingaben prüfen, CAR und die fünf Einflussgrößen berechnen, Auswertung starten und Ergebnisse prüfen. `main()` verbindet die Schritte. |
| `statistik.py` | Regressionen, feste Effekte, Standardfehler, Diagnostik und Robustheitsprüfungen. |
| `grafiken.py` | Die Abbildungen aus den berechneten Ergebnissen erstellen. |

`Daten/` enthält die sechs benötigten Eingabetabellen.
`Ergebnisse/Ergebnisse.md` zeigt die wichtigsten Zahlen; daneben liegen die
vollständigen Ergebnistabellen und unter `Abbildungen/` die PNG- und PDF-Dateien.
Ein erneuter Lauf ersetzt diese erzeugten Ergebnisse.

## Was genau berechnet wird

1. **Einfache Rendite:** `r_t = P_t / P_(t−1) − 1`, für Aktien und SPY.
2. **Marktmodell:** Für jeden Bericht werden Alpha und Beta über die Handelstage
   `[-250,−30]` geschätzt. Erwartete Rendite = `Alpha + Beta × SPY-Rendite`.
3. **CAR:** Tatsächliche minus erwartete Rendite ergibt AR.
   `CAR = AR_0 + AR_1`, ohne Aufzinsung. Tag 0 ist der erste Handelstag nach der
   Veröffentlichung nach Börsenschluss. `|CAR|` misst die Stärke der Reaktion.
4. **Einflussgrößen:** SUE, VOL, PREVOL, SEARCH und TONE; Definitionen unten.
5. **Regressionen:** CAR mit SUE; |CAR| mit |SUE|. Beide enthalten VOL, PREVOL,
   SEARCH, TONE sowie Unternehmens-, Berichtsjahres- und Kalenderquartalseffekte.
   Standardfehler werden nach Ereignishandelstag geclustert (CR1, t-Tests mit
   G−1 Freiheitsgraden). Die Hauptmodelle verwenden keine Winsorisierung.
6. **Prüfungen:** Einzelmodelle, Korrelationen, VIF, Residuen, Breusch-Pagan,
   Jarque-Bera, Cook-Distanzen, AAR-/CAAR-Tests; außerdem Modelle ohne feste
   Effekte, mit Winsorisierung, ohne jeweils ein Unternehmen sowie mit
   CAR-Fenstern `[-1,+1]` und `[0,+2]`. Beim Fensterwechsel bleiben die
   Einflussgrößen unverändert.

| Variable | Definition |
|---|---|
| SUE | Tatsächlicher minus erwarteter Gewinn je Aktie, geteilt durch die Stichprobenstandardabweichung der Gewinnüberraschungen der acht vorherigen Berichte. |
| VOL | Mittleres standardisiertes logarithmiertes Handelsvolumen an Tag 0 und +1; Vergleichszeitraum `[-60,−10]`. |
| PREVOL | Dieselbe Standardisierung, gemittelt über `[-3,−1]`. |
| SEARCH | Logarithmus des Suchinteresses in der Ereigniswoche geteilt durch den Median der acht vorherigen Wochen; alle neun Werte aus demselben Export. |
| TONE | `(positive Sätze − negative Sätze) / alle Sätze`, einschließlich neutraler Sätze. |

In den CSV-Dateien stehen Renditen als Dezimalzahlen: **0,01 entspricht einem
Prozentpunkt**. Die Diagramme der Renditen verwenden Prozentpunkte.
Die Logarithmen von Handelsvolumen und Suchinteresse gehören weiterhin zu deren
Definition; die Kursrenditen sind einfach.

## Reproduzierbarkeit

Die Kernversion reproduziert die **statistische Auswertung ab den bereinigten
Eingabetabellen**. Sie lädt keine Daten herunter, bereinigt keine ursprünglichen
Anbieterdateien und führt FinBERT nicht erneut aus. TONE wird aus den 80.262
gespeicherten Satzbewertungen neu aggregiert. Alle weiteren Kennzahlen und
Modelle werden neu berechnet.

Jeder Lauf prüft die Eingabedateien anhand ihrer Prüfsummen und vergleicht
zwölf Ergebnistabellen mit dem bisherigen CAR-Lauf. Erwartet werden **170
Ereignisse und 113 Datumscluster**. Die numerische Toleranz beträgt relativ
`1e-9`, absolut `1e-11`. Nur die Herkunftsbezeichnung von TONE wurde präzisiert;
sie behauptet nun keine neue FinBERT-Inferenz. `Ergebnisse/Pruefung.json` wird
erst nach erfolgreichem Abschluss geschrieben.

Die 170 vollständigen Transkripte sind unter `Daten/Transkripte/` enthalten.
Die Satztexte und Bewertungen stehen zusätzlich in `Daten/finbert_saetze.csv`.
Lokale Sicherungen und die Python-Umgebung sind nicht Teil dieses Repositories.

Entfernt aus der aktiven Fassung sind die Browseroberfläche, Downloads,
Audioverwaltung, zusätzliche Sentimentverfahren, alte Log-Rendite-Varianten,
mehrfache Datenbestände, historische Berichte und Spezialexporte nach Jamovi,
PSPP und SQLite. Für die Interpretation gelten die Einschränkungen der
Seminararbeit: Die Regressionen zeigen Zusammenhänge und beweisen keine Kausalität.
