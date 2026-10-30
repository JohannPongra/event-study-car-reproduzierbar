# Interface – Grafiken der Seminararbeit

**`index.html` doppelklicken** oder `STARTEN.bat` öffnen. Die Ansicht funktioniert
lokal ohne Internet und ohne laufenden Server. Suche und Themenfilter helfen
beim Finden; ein Klick auf eine Grafik öffnet die Großansicht. Mit den
Pfeiltasten geht es zur nächsten oder vorherigen Abbildung.

## Welche Grafiken angezeigt werden

- **In der Arbeit:** Alle 25 nummerierten Analyseabbildungen aus
  `W-Seminar-Johann-PongratzNeu.docx`, einschließlich ihrer Beschriftungen.
  Das dekorative Deckblatt gehört nicht zu diesen Analyseabbildungen.
- **Neu berechnet:** Die zehn Diagrammdateien der Kernversion. Mehrere in Word
  einzeln verwendete Diagramme stehen hier gemeinsam in einer Abbildung.
  Zu diesen Grafiken gibt es zusätzlich PDFs.

## Wie die Grafiken entstehen

`analyse.py` berechnet einfache Renditen, das Marktmodell und
`CAR = Summe der abnormalen Renditen`. `statistik.py` schätzt die Regressionen
und Robustheitsmodelle. `grafiken.py` zeichnet daraus mit Matplotlib Boxplots,
Korrelationen, Ereignisverläufe, partielle Zusammenhänge, Koeffizienten mit
95%-Konfidenzintervallen und Diagnostik. Die Bilder werden als PNG und PDF unter
`Ergebnisse/Abbildungen/` gespeichert; Renditeachsen verwenden Prozentpunkte.

Für die Ansicht **In der Arbeit** liest `Interface/erstellen.py` die bereits
eingebetteten Bilder direkt aus der Word-Datei und speichert sie unverändert
unter `Interface/Abbildungen/`. So zeigt diese Ansicht exakt die Bilder des
Arbeitsstands, auch wenn sich eine spätere Neuberechnung davon unterscheidet.
Gelöschte Änderungsstände werden ausgelassen; Einfügungen werden angezeigt.
Die Word-Datei wird nicht verändert. Beschriftungen und Prüfsummen stehen in
`Abbildungen.json`; `daten.js` macht sie ohne Server in der Galerie verfügbar.

## Aktualisieren

1. Neue Berechnung: im übergeordneten Projektordner `REPRODUZIEREN.bat` starten.
2. Danach oder nach Änderungen am Word-Dokument `AKTUALISIEREN.bat` ausführen
   und die Browserseite neu laden.

Optional wird `Arbeitsstand.docx` im Projektordner gelesen; die Word-Datei ist nicht im Repository enthalten.
Für eine andere Word-Datei: `AKTUALISIEREN.bat "C:\Pfad\Arbeitsstand.docx"`.
Zum bloßen Betrachten der gespeicherten Dokumentbilder wird Word nicht benötigt.
