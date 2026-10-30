"""Liest die Abbildungen eines Word-Arbeitsstands für die lokale Galerie aus."""
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET
from datetime import datetime
import argparse
import hashlib
import json
import posixpath
import re

ORDNER = Path(__file__).resolve().parent
PROJEKT = ORDNER.parent
STANDARD = PROJEKT / 'Arbeitsstand.docx'
NS = {
    'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
    'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
    'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
    'wp': 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing',
}


def gruppe(caption):
    if 'Empfindlichkeit' in caption:
        return 'Robustheit'
    if any(word in caption for word in ['Residuen', 'Cook-Distanzen']):
        return 'Diagnostik'
    if 'Koeffizienten' in caption:
        return 'Koeffizienten'
    if 'Partieller' in caption:
        return '|CAR|' if '|CAR|' in caption else 'CAR'
    return 'Überblick'


def dokumentgrafiken(document):
    """Aktuelle Bilder lesen; gelöschte Revisionen und das Deckblatt auslassen."""
    images, payloads = [], {}
    with ZipFile(document) as package:
        root = ET.fromstring(package.read('word/document.xml'))
        # Anzeige wie bei angenommenen Einfügungen: gelöschte Inhalte ausblenden.
        for parent in root.iter():
            for child in list(parent):
                if child.tag in {f"{{{NS['w']}}}del", f"{{{NS['w']}}}moveFrom"}:
                    parent.remove(child)
        relationships = {
            item.attrib['Id']: item.attrib['Target']
            for item in ET.fromstring(package.read('word/_rels/document.xml.rels'))
            if item.attrib.get('TargetMode') != 'External'
        }
        paragraphs = list(root.find('w:body', NS).iter(f"{{{NS['w']}}}p"))
        texts = [''.join(t.text or '' for t in p.iter(f"{{{NS['w']}}}t")) for p in paragraphs]
        for index, paragraph in enumerate(paragraphs):
            drawings = paragraph.findall('.//w:drawing', NS)
            for drawing in drawings:
                blip = drawing.find('.//a:blip', NS)
                if blip is None:
                    continue
                props = drawing.find('.//wp:docPr', NS)
                description = props.attrib.get('descr', '') if props is not None else ''
                number_match = re.search(r'Abbildung\s+(\d+)', description)
                caption = None
                for text in texts[index:index + 4]:
                    match = re.match(r'Abbildung\s+(\d+)\s*:', text)
                    if match and (number_match is None or match[1] == number_match[1]):
                        caption, number = text, int(match[1])
                        break
                if caption is None:
                    if number_match:
                        raise ValueError('Beschriftung fehlt für ' + description)
                    continue  # Unnummerierte Gestaltung auf dem Deckblatt.
                rid = blip.attrib[f"{{{NS['r']}}}embed"]
                member = posixpath.normpath(posixpath.join('word', relationships[rid]))
                extension = Path(member).suffix.lower()
                if extension not in {'.png', '.jpg', '.jpeg', '.gif', '.svg'}:
                    raise ValueError('Bildformat wird im Browser nicht unterstützt: ' + extension)
                name = f'Abbildung_{number:02d}{extension}'
                content = package.read(member)
                payloads[name] = content
                images.append({
                    'id': f'abb-{number}', 'number': number, 'source': 'document',
                    'title': re.sub(r'^Abbildung\s+\d+\s*:\s*', '', caption),
                    'caption': caption, 'category': gruppe(caption),
                    'image': 'Abbildungen/' + name,
                    'sha256': hashlib.sha256(content).hexdigest(),
                    'origin': member,
                })
    numbers = sorted(item['number'] for item in images)
    if not numbers or numbers != list(range(1, len(numbers) + 1)):
        raise ValueError('Die Abbildungen müssen eindeutig und lückenlos nummeriert sein.')
    return sorted(images, key=lambda item: item['number']), payloads


def berechnete_grafiken():
    charts = [
        ('abs_car_verteilung', 'Verteilung von |CAR| nach Unternehmen', 'Überblick'),
        ('ueberblick', 'Mittlere Kursreaktion und Korrelationsmatrix', 'Überblick'),
        ('car_partiell', 'Partielle Zusammenhänge mit CAR', 'CAR'),
        ('abs_car_partiell', 'Partielle Zusammenhänge mit |CAR|', '|CAR|'),
        ('car_koeffizienten', 'Skalierte Koeffizienten für CAR', 'Koeffizienten'),
        ('abs_car_koeffizienten', 'Skalierte Koeffizienten für |CAR|', 'Koeffizienten'),
        ('car_diagnostik', 'Modelldiagnostik für CAR', 'Diagnostik'),
        ('abs_car_diagnostik', 'Modelldiagnostik für |CAR|', 'Diagnostik'),
        ('car_unternehmen_cluster', 'Unternehmensauswahl im CAR-Modell', 'Robustheit'),
        ('abs_car_unternehmen_cluster', 'Unternehmensauswahl im |CAR|-Modell', 'Robustheit'),
    ]
    result = []
    for stem, title, category in charts:
        image = PROJEKT / 'Ergebnisse/Abbildungen' / (stem + '.png')
        if image.is_file():
            item = {'id': stem, 'source': 'calculated', 'title': title,
                    'caption': title, 'category': category,
                    'image': '../Ergebnisse/Abbildungen/' + image.name}
            if image.with_suffix('.pdf').is_file():
                item['pdf'] = '../Ergebnisse/Abbildungen/' + stem + '.pdf'
            result.append(item)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dokument', nargs='?', type=Path, default=STANDARD)
    document = parser.parse_args().dokument.resolve()
    if not document.is_file():
        parser.error('Word-Datei angeben: python Interface/erstellen.py PFAD_ZUR_DATEI.docx')
    pictures, payloads = dokumentgrafiken(document)
    target = ORDNER / 'Abbildungen'
    target.mkdir(exist_ok=True)
    for name, content in payloads.items():
        (target / name).write_bytes(content)
    data = {
        'document': document.name,
        'documentSha256': hashlib.sha256(document.read_bytes()).hexdigest(),
        'updated': datetime.now().astimezone().isoformat(timespec='seconds'),
        'figures': pictures + berechnete_grafiken(),
    }
    serialized = json.dumps(data, ensure_ascii=False, indent=2)
    (ORDNER / 'Abbildungen.json').write_text(serialized, encoding='utf-8')
    # Eine lokale JS-Datei funktioniert auch bei Doppelklick ohne Webserver.
    (ORDNER / 'daten.js').write_text('window.GRAFIKEN = ' + serialized + ';\n', encoding='utf-8')
    print(f'Galerie aktualisiert: {len(pictures)} Dokumentabbildungen; '
          f'{len(data["figures"]) - len(pictures)} berechnete Grafiken.')


if __name__ == '__main__':
    main()
