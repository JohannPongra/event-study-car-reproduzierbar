'use strict';
const data = window.GRAFIKEN;
const $ = id => document.getElementById(id);
let source = 'document';
let shown = [];
let current = 0;
const viewer = $('viewer');
$('document-name').textContent = data.document;
$('updated').textContent = 'Galerie aktualisiert: ' + new Date(data.updated).toLocaleDateString('de-DE');
$('document-count').textContent = data.figures.filter(x => x.source === 'document').length;
$('calculated-count').textContent = data.figures.filter(x => x.source === 'calculated').length;

function render() {
  const query = $('search').value.trim().toLocaleLowerCase('de');
  const category = $('category').value;
  const available = data.figures.filter(x => x.source === source);
  shown = available.filter(x => (category === 'all' || x.category === category)
    && `${x.caption} ${x.category}`.toLocaleLowerCase('de').includes(query));
  $('source-note').textContent = source === 'document'
    ? `Die ${available.length} nummerierten Analyseabbildungen aus dem angegebenen Word-Arbeitsstand, mit ihren ursprünglichen Beschriftungen.`
    : 'Ausgabe der Kernversion: Grafiken aus den neu berechneten Daten und Modellen. Mehrteilige Abbildungen sind hier zusammengefasst.';
  $('section-title').textContent = source === 'document' ? 'Abbildungen im Arbeitsstand' : 'Abbildungen der Neuberechnung';
  $('result-count').textContent = `${shown.length} von ${available.length} Abbildungen`;
  $('gallery').replaceChildren();
  $('empty').hidden = shown.length > 0;
  shown.forEach((figure, index) => {
    const card = document.createElement('article');
    card.className = 'card';
    const preview = document.createElement('button');
    preview.type = 'button'; preview.className = 'preview';
    preview.setAttribute('aria-label', figure.caption + ' vergrößern');
    const img = document.createElement('img');
    img.src = figure.image; img.alt = figure.caption;
    img.loading = index < 4 ? 'eager' : 'lazy';
    const expand = document.createElement('span');
    expand.className = 'expand'; expand.textContent = 'Vergrößern ↗';
    preview.append(img, expand); preview.addEventListener('click', () => openFigure(index));
    const text = document.createElement('div'); text.className = 'card-text';
    const meta = document.createElement('div'); meta.className = 'card-meta';
    const number = document.createElement('span'); number.className = 'number';
    number.textContent = figure.number ? 'Abbildung ' + figure.number : 'Neuberechnung';
    const tag = document.createElement('span'); tag.className = 'tag'; tag.textContent = figure.category;
    meta.append(number, tag);
    const title = document.createElement('h3'); title.textContent = figure.title;
    const links = document.createElement('div'); links.className = 'card-links';
    const download = document.createElement('a'); download.href = figure.image;
    download.download = figure.image.split('/').pop(); download.textContent = 'PNG herunterladen ↓';
    links.append(download);
    if (figure.pdf) {
      const pdf = document.createElement('a'); pdf.href = figure.pdf;
      pdf.target = '_blank'; pdf.rel = 'noopener'; pdf.textContent = 'PDF öffnen ↗'; links.append(pdf);
    }
    text.append(meta, title, links); card.append(preview, text); $('gallery').append(card);
  });
}

function openFigure(index) {
  current = index;
  const figure = shown[current];
  $('large-image').src = figure.image; $('large-image').alt = figure.caption;
  $('viewer-title').textContent = figure.caption;
  $('viewer-category').textContent = figure.category;
  $('viewer-position').textContent = `${index + 1} / ${shown.length}`;
  $('original').href = figure.image;
  $('previous').disabled = index === 0; $('next').disabled = index === shown.length - 1;
  if (!viewer.open) viewer.showModal();
}

document.querySelectorAll('.source').forEach(button => button.addEventListener('click', () => {
  source = button.dataset.source;
  document.querySelectorAll('.source').forEach(item => {
    item.classList.toggle('active', item === button);
    item.setAttribute('aria-pressed', String(item === button));
  });
  $('search').value = ''; $('category').value = 'all'; render();
}));
$('search').addEventListener('input', render);
$('category').addEventListener('change', render);
$('reset').addEventListener('click', () => { $('search').value = ''; $('category').value = 'all'; render(); });
$('close').addEventListener('click', () => viewer.close());
$('previous').addEventListener('click', () => openFigure(current - 1));
$('next').addEventListener('click', () => openFigure(current + 1));
viewer.addEventListener('click', event => { if (event.target === viewer) viewer.close(); });
viewer.addEventListener('keydown', event => {
  if (event.key === 'ArrowLeft' && current > 0) { event.preventDefault(); openFigure(current - 1); }
  if (event.key === 'ArrowRight' && current < shown.length - 1) { event.preventDefault(); openFigure(current + 1); }
});
render();
