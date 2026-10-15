'use strict';
const $ = id => document.getElementById(id);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const icon = name => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const num = n => n.toLocaleString('en');
const PRESETS = {lossless: 'Lossless', high: 'High', balanced: 'Balanced', compact: 'Compact'};
// A page is changed by redrawing it, or by re-encoding its images at their own resolution.
const CHANGED = ['converted', 'reencoded'];
const PRESENTATION = /\.(pptx|ppt|pptm|ppsx|pps|ppsm|potx|pot|odp)$/i;
// PDFs, and presentations when PowerPoint or LibreOffice is there to convert them.
const openable = name => /\.pdf$/i.test(name) || (PRESENTATION.test(name) && !!state.config?.converter);
function cannotOpen(name) {
  if (PRESENTATION.test(name)) return 'Opening presentations needs Microsoft PowerPoint or LibreOffice on this computer.';
  return state.config?.converter ? 'Choose a PDF or a PowerPoint file.' : 'That isn’t a PDF. Choose a file ending in .pdf.';
}
const after = p => p.afterBytes ?? p.imageBytes + p.textBytes;
const EXTRAS = {
  editing: ['Editing data from design apps', 'Used only to reopen the file in Illustrator or Photoshop. Pages look the same.', true],
  thumbnails: ['Page thumbnails', 'Small previews stored in the file. PDF readers make their own.', true],
  attachments: ['Attached files', 'Files carried inside the PDF. They are not part of any page.', false],
  docmeta: ['Document metadata', 'Editing history and descriptions. The title and author stay.', false],
};
const SINGULAR = {images: 'Image', content: 'Text & drawings', fonts: 'Font', forms: 'Shared drawing', metadata: 'Metadata', attachments: 'Attachment', containers: 'Packed objects', other: 'Data', structure: 'PDF structure'};
const state = {doc: null, config: null, view: 'map', category: null, selection: null, query: '', sort: 'size', offset: 0, busy: false, job: null, xhr: null, compress: null, sample: null, compare: null, showAllChanged: false, zoom: {}};
const perPage = 20;

function size(n) {
  if (n >= 1e6) return (n / 1e6).toLocaleString('en', {maximumFractionDigits: 2, minimumFractionDigits: 2}) + ' MB';
  if (n >= 1e3) return (n / 1e3).toLocaleString('en', {maximumFractionDigits: 1}) + ' KB';
  return num(n) + ' B';
}
const pct = (n, total) => (100 * n / total).toFixed(1) + '%';
const category = id => state.doc.categories.find(c => c.id === id);
const swatch = id => `<i class="swatch" style="--swatch:var(--cat-${id})"></i>`;
function objName(r) {
  if (r.id === 0) return 'PDF structure';
  if (r.category === 'content') return `Page ${r.pages[0] ?? '—'} text & drawings`;
  if (r.category === 'images') return r.pages.length === 1 ? `Image on page ${r.pages[0]}` : r.pages.length ? `Image on ${num(r.pages.length)} pages` : 'Unused image';
  return `${SINGULAR[r.category] ?? 'Part'} #${r.id}`;
}
function usedOn(pages) { return pages.length === 0 ? 'Whole file' : pages.length === 1 ? `Page ${pages[0]}` : `${num(pages.length)} pages` }
function notice(message) { $('notice').textContent = message; $('notice').hidden = !message }
function surface(which) {
  for (const name of ['welcome', 'loading', 'workspace', 'batch']) $(name).hidden = name !== which;
  const working = which === 'workspace';
  $('sidebar').hidden = !working; $('doc-title').hidden = !working;
  if (!working) $('shrink-button').hidden = true;
  $('shell').classList.toggle('bare', !working);
  if (!working) closeInspector();
}
async function api(url, opts = {}) {
  const response = await fetch(url, opts);
  const data = await response.json();
  if (!response.ok) throw Error(data.error || 'Could not complete this request.');
  return data;
}
function post(data) { return {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Explorer-Token': state.config.token}, body: JSON.stringify(data)} }
function busy(value) {
  state.busy = value; $('open-file').disabled = value; $('dropzone').disabled = value;
  $('cancel-open').hidden = !value; $('cancel-open').disabled = false;
  if (!value) { state.job = null; state.xhr = null }
}

// Opening and analysing ------------------------------------------------------
async function awaitAnalysis(job) {
  state.job = job; state.xhr = null;
  $('upload-progress').hidden = false; $('upload-progress').removeAttribute('value');
  $('loading-title').textContent = 'Measuring your PDF…'; $('loading-detail').textContent = '';
  while (true) {
    const result = await api('/api/job/' + job);
    if (result.status === 'error') throw Error(result.error);
    if (result.status === 'cancelled') { surface(state.doc ? 'workspace' : 'welcome'); notice('Stopped. Your PDF is unchanged.'); return }
    if (result.phase) {
      $('loading-title').textContent = result.phase.startsWith('Converting') ? 'Converting your slides…' : 'Measuring your PDF…';
      $('loading-detail').textContent = result.total > 1 ? `${result.phase} · ${Math.floor(100 * result.completed / result.total)}%` : result.phase;
      $('upload-progress').max = result.total; $('upload-progress').value = result.completed;
    }
    if (result.status === 'ready') {
      state.doc = await api('/api/document/' + result.id);
      state.doc.byId = new Map(state.doc.objects.map(r => [r.id, r]));
      Object.assign(state, {selection: null, category: null, view: 'map', query: '', offset: 0, sample: null, compare: null});
      $('search').value = ''; $('sample-page').value = ''; $('keep-pages').value = ''; $('more-options').open = false;
      surface('workspace'); closeInspector(); render();
      if (state.doc.repaired) notice('This PDF needed repair to open. The breakdown is an estimate; the total is the real file size.');
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 450));
  }
}
async function openSample(id) {
  if (state.busy) return;
  busy(true); notice(''); surface('loading');
  try { await awaitAnalysis((await api('/api/sample', post({id}))).job) }
  catch (e) { notice(e.message); surface(state.doc ? 'workspace' : 'welcome') }
  finally { busy(false) }
}
function upload(file) {
  if (!file || state.busy || !state.config) return;
  if (!openable(file.name)) { notice(cannotOpen(file.name)); return }
  if (file.size > 1024 ** 3) { notice('Choose a file smaller than 1 GB.'); return }
  busy(true); notice(''); surface('loading');
  $('loading-title').textContent = PRESENTATION.test(file.name) ? 'Opening your presentation…' : 'Opening your PDF…'; $('loading-detail').textContent = file.name;
  $('upload-progress').hidden = false; $('upload-progress').max = 100; $('upload-progress').value = 0;
  const xhr = new XMLHttpRequest(); state.xhr = xhr;
  xhr.open('POST', '/api/open'); xhr.setRequestHeader('X-Explorer-Token', state.config.token); xhr.setRequestHeader('X-Filename', encodeURIComponent(file.name));
  xhr.upload.onprogress = e => { if (e.lengthComputable) $('upload-progress').value = 100 * e.loaded / e.total };
  xhr.onerror = () => { notice('Paperweight stopped responding. Restart it and try again.'); surface(state.doc ? 'workspace' : 'welcome'); busy(false) };
  xhr.onabort = () => { notice('Stopped. Your PDF is unchanged.'); surface(state.doc ? 'workspace' : 'welcome'); busy(false) };
  xhr.onload = async () => {
    try { const result = JSON.parse(xhr.responseText); if (xhr.status >= 400) throw Error(result.error); await awaitAnalysis(result.job) }
    catch (e) { notice(e.message); surface(state.doc ? 'workspace' : 'welcome') }
    finally { busy(false) }
  };
  xhr.send(file);
}

// Views ----------------------------------------------------------------------
const VIEWS = {
  map: ['Where the space goes', 'Each block is sized by its share of the file. Select one to look inside.'],
  pages: ['Pages', 'How much each page uses. Parts shared between pages count under each of them.'],
  objects: ['Parts of the file', 'Everything stored in the PDF, largest first.'],
  split: ['Split into several PDFs', 'Each part becomes a PDF of its own, and a ZIP holds them all. The original stays as it is.'],
  organize: ['Organize pages', 'Drag pages to reorder them. Select pages to turn or delete them. Saving makes a new PDF; the original stays as it is.'],
  compress: ['Make a smaller copy', 'Text stays real, selectable text. On heavy pages the artwork underneath is redrawn as one image, or large pictures are stored more compactly.'],
};
function setView(view, cat = null) {
  if (!state.doc) return;
  Object.assign(state, {view, category: cat, offset: 0, query: '', selection: null, compare: null});
  $('search').value = ''; $('map-scroll').scrollTop = 0; closeInspector(); render();
  if (view === 'compress') $('compress-region').scrollIntoView({block: 'nearest'});
}
function render() {
  const d = state.doc; if (!d) return;
  const view = state.view, c = state.category ? category(state.category) : null, compressing = view === 'compress', organizing = view === 'organize', splitting = view === 'split';
  $('doc-name').textContent = d.name; $('doc-meta').textContent = `${size(d.bytes)} · ${num(d.pageCount)} page${d.pageCount === 1 ? '' : 's'}${d.convertedFrom ? ` · from ${d.convertedFrom}` : ''}`;
  $('save-pdf').hidden = !d.convertedFrom; $('save-pdf').href = `/api/document/${d.id}/download`;
  $('page-count').textContent = num(d.pageCount); $('object-count').textContent = num(d.objectCount);
  document.querySelectorAll('.nav-item').forEach(b => { const on = b.dataset.view === view; b.classList.toggle('active', on); on ? b.setAttribute('aria-current', 'page') : b.removeAttribute('aria-current') });
  $('shrink-button').hidden = compressing; $('doc-title').hidden = false;
  const [title, description] = VIEWS[view];
  $('view-title').textContent = c && view === 'map' ? c.label : title;
  $('view-description').textContent = c && view === 'map' ? c.description : description;
  $('breadcrumb').hidden = !(c && view === 'map');
  $('map-region').hidden = view !== 'map'; $('accounting').hidden = true;
  $('table-section').hidden = compressing || organizing || splitting; $('compress-region').hidden = !compressing; $('organize-region').hidden = !organizing; $('split-region').hidden = !splitting;
  $('table-title').textContent = view === 'pages' ? 'All pages' : c ? `All ${c.label.toLowerCase()}` : view === 'objects' ? 'All parts' : 'Largest parts';
  const filter = $('category-filter'); filter.hidden = view !== 'objects';
  if (view === 'objects') {
    filter.innerHTML = `<option value="">All types</option>` + d.categories.map(x => `<option value="${x.id}">${esc(x.label)}</option>`).join('');
    filter.value = state.category ?? '';
  }
  if (compressing) renderCompress(); else if (organizing) { renderOrganize(); $('organize-result').hidden = !state.org?.result } else if (splitting) renderSplit(); else { renderMap(); renderTable() }
  badge();
}
// Rows from top to bottom, largest parts first. Each row takes parts while
// that keeps its tiles closer to a readable, slightly wide shape.
function rows(items, width, aspect = 2.4) {
  const out = []; let y = 0, i = 0;
  const worst = (row, sum) => { const h = sum / width; return Math.max(...row.map(it => { const r = it.area / h / h; return Math.max(r / aspect, aspect / r) })) };
  while (i < items.length) {
    const row = [items[i]]; let sum = items[i].area, score = worst(row, sum); i++;
    while (i < items.length) {
      const next = worst([...row, items[i]], sum + items[i].area);
      if (next > score) break;
      row.push(items[i]); sum += items[i].area; score = next; i++;
    }
    // A last row of a few small parts would be a sliver; give it a readable height.
    const h = Math.max(sum / width, 56); let x = 0;
    for (const it of row) { const w = width * it.area / sum; out.push({...it, x, y, w, h}); x += w }
    y += h;
  }
  return out;
}
const MAP_LIMIT = 400, MIN_TILE = 160 * 64;
function renderMap() {
  if (!state.doc || state.view !== 'map') return;
  const map = $('treemap'), scroller = $('map-scroll'), d = state.doc; let items, total;
  if (state.category) {
    const rows = d.objects.filter(r => r.category === state.category && r.bytes); total = category(state.category).bytes;
    items = rows.slice(0, MAP_LIMIT).map(r => ({id: r.id, bytes: r.bytes, label: objName(r), kind: 'object', cat: r.category}));
    if (rows.length > MAP_LIMIT) items.push({id: 'rest', bytes: rows.slice(MAP_LIMIT).reduce((s, r) => s + r.bytes, 0), label: `${num(rows.length - MAP_LIMIT)} smaller`, kind: 'rest', cat: state.category});
  } else {
    total = d.bytes;
    items = d.categories.map(c => ({id: c.id, bytes: c.bytes, label: c.label, kind: 'category', cat: c.id, count: c.id === 'images' ? `${num(c.count)} image${c.count === 1 ? '' : 's'}` : c.id === 'fonts' ? `${num(c.count)} font${c.count === 1 ? '' : 's'}` : ''}));
  }
  items.sort((a, b) => b.bytes - a.bytes);
  // Like a disk-usage map, but tall: parts keep their true share of the visible
  // area, and parts too small to read are enlarged to a minimum and placed lower.
  const width = scroller.clientWidth, fit = width * scroller.clientHeight / items.reduce((s, i) => s + i.bytes, 0);
  let enlarged = 0;
  for (const i of items) { i.area = i.bytes * fit; if (i.area < MIN_TILE) { i.area = MIN_TILE; enlarged++ } }
  const rects = rows(items, width), height = Math.ceil(Math.max(scroller.clientHeight, ...rects.map(r => r.y + r.h)));
  map.style.height = height + 'px';
  map.replaceChildren();
  for (const r of rects) {
    const b = document.createElement('button');
    b.className = 'tile' + (r.w < 90 || r.h < 46 ? ' tiny' : r.w < 170 || r.h < 92 ? ' compact' : '');
    b.style.cssText = `left:${r.x}px;top:${r.y}px;width:${r.w}px;height:${r.h}px;--swatch:var(--cat-${r.cat})`;
    b.setAttribute('aria-label', `${r.label}, ${size(r.bytes)}, ${pct(r.bytes, total)}`); b.title = `${r.label} · ${size(r.bytes)}`;
    b.innerHTML = `<span class="tile-label">${esc(r.label)}</span><span class="tile-size">${size(r.bytes)} <small>${pct(r.bytes, total)}</small></span>${r.count ? `<span class="tile-count">${esc(r.count)}</span>` : ''}`;
    b.onclick = () => r.kind === 'category' ? setView('map', r.id) : r.kind === 'object' ? selectObject(r.id) : $('table-section').scrollIntoView({block: 'start'});
    map.append(b);
  }
  $('map-caption').textContent = (state.category ? `${size(total)} in ${category(state.category).label.toLowerCase()}` : `${size(total)} in total`)
    + (enlarged ? ` · ${num(enlarged)} small part${enlarged === 1 ? ' is' : 's are'} drawn larger${height > scroller.clientHeight ? '; scroll for more' : ''}` : '');
}
function getRows() {
  const pages = state.view === 'pages';
  let rows = pages ? state.doc.pages.slice() : state.doc.objects.filter(r => !state.category || r.category === state.category);
  const q = state.query.trim().toLowerCase();
  if (q) rows = rows.filter(r => pages ? `${r.number} ${r.label}`.toLowerCase().includes(q) : `${objName(r)} ${r.id} ${category(r.category)?.label} ${r.codec}`.toLowerCase().includes(q));
  rows.sort((a, b) => state.sort === 'number' ? (a.number ?? a.id) - (b.number ?? b.id) : (b.referencedBytes ?? b.bytes) - (a.referencedBytes ?? a.bytes));
  return rows;
}
function pageName(p) { return `Page ${p.number}${p.label && p.label !== String(p.number) ? ` <small>printed ${esc(p.label)}</small>` : ''}` }
function renderTable() {
  if (!state.doc) return;
  const pages = state.view === 'pages', rows = getRows();
  state.offset = Math.min(state.offset, Math.max(0, Math.ceil(rows.length / perPage) - 1));
  const slice = rows.slice(state.offset * perPage, (state.offset + 1) * perPage), sel = state.selection;
  $('table-head').innerHTML = pages ? '<tr><th>Page</th><th class="num">Its own</th><th class="num narrow-hide">Shared</th><th class="num">Total</th></tr>' : '<tr><th>Part</th><th class="narrow-hide">Type</th><th class="narrow-hide">Used on</th><th class="num">Size</th></tr>';
  $('table-body').innerHTML = slice.length ? slice.map(r => pages
    ? `<tr tabindex="0" data-page="${r.number}" class="${sel?.kind === 'page' && sel.id === r.number ? 'selected' : ''}"><td>${pageName(r)}</td><td class="num">${size(r.exclusiveBytes)}</td><td class="num narrow-hide">${size(r.sharedBytes)}</td><td class="num strong">${size(r.referencedBytes)}</td></tr>`
    : `<tr tabindex="0" data-object="${r.id}" class="${sel?.kind === 'object' && sel.id === r.id ? 'selected' : ''}"><td>${swatch(r.category)}${esc(objName(r))}</td><td class="narrow-hide">${esc(r.category === 'images' ? r.codec : category(r.category)?.label)}</td><td class="narrow-hide">${usedOn(r.pages)}</td><td class="num strong">${size(r.bytes)}</td></tr>`).join('')
    : '<tr class="empty-row"><td colspan="4">Nothing matches. Try a page number or a type such as “JPEG”.</td></tr>';
  $('results-count').textContent = rows.length ? `${num(state.offset * perPage + 1)}–${num(Math.min((state.offset + 1) * perPage, rows.length))} of ${num(rows.length)}` : '';
  $('previous').disabled = state.offset === 0; $('next').disabled = (state.offset + 1) * perPage >= rows.length;
  $('previous').parentElement.hidden = rows.length <= perPage;
}

// Details panel --------------------------------------------------------------
function openInspector(html) {
  $('inspector-body').innerHTML = html; $('inspector').hidden = false; $('shell').classList.add('inspecting');
  if (innerWidth <= 860) $('inspector').scrollIntoView({block: 'start'});
}
function closeInspector() { $('inspector').hidden = true; $('shell').classList.remove('inspecting'); state.selection = null }
function facts(entries) { return '<dl class="facts">' + entries.map(([a, b]) => `<div><dt>${esc(a)}</dt><dd>${esc(b)}</dd></div>`).join('') + '</dl>' }
function showPreview(kind, number) {
  const frame = $('preview-frame'); if (!frame) return;
  frame.innerHTML = '<p>Rendering…</p>';
  const image = new Image(); image.alt = kind === 'page' ? `Page ${number}` : 'Stored image';
  image.onload = () => frame.replaceChildren(image);
  image.onerror = () => frame.innerHTML = '<p>No preview for this part. Try its page.</p>';
  image.src = `/api/document/${state.doc.id}/preview/${kind}/${number}?width=700`;
  document.querySelectorAll('[data-preview-kind]').forEach(b => b.classList.toggle('active', b.dataset.previewKind === kind));
}
async function selectObject(id) {
  const r = state.doc.byId.get(id); if (!r) return;
  state.selection = {kind: 'object', id}; renderTable();
  const image = r.category === 'images', docid = state.doc.id;
  openInspector(`<p class="detail-kind">${swatch(r.category)}${esc(category(r.category)?.label)}</p><h2>${esc(objName(r))}</h2><p class="detail-size">${size(r.bytes)} · ${pct(r.bytes, state.doc.bytes)} of the file</p>
    ${image || r.pages.length ? '<div class="preview" id="preview-frame"></div>' : ''}
    ${image && r.pages.length ? `<div class="segmented" role="group" aria-label="Preview"><button data-preview-kind="image" data-number="${id}">Image</button><button data-preview-kind="page" data-number="${r.pages[0]}">On page ${r.pages[0]}</button></div>` : ''}
    ${facts([['Used on', usedOn(r.pages)], ['Stored as', r.codec], ...(image ? [['Pixels', '…']] : [])])}
    ${r.pages.length > 1 ? `<p class="note">Shared by ${num(r.pages.length)} pages, stored once.</p>` : ''}
    ${r.pages.length ? '<div class="page-links">' + r.pages.slice(0, 12).map(p => `<button data-page="${p}">Page ${p}</button>`).join('') + (r.pages.length > 12 ? `<span>+${num(r.pages.length - 12)} more</span>` : '') + '</div>' : ''}
    ${r.category === 'attachments' ? `<button class="button quiet wide" data-remove-extra="attachments">${icon('compress')}Remove attached files</button>` : ''}
    ${r.category === 'metadata' ? '<p class="note">Metadata of images and pages is removed whenever you make a smaller copy.</p>' : ''}
    <details class="technical"><summary>Technical details</summary><pre id="dictionary-text">Loading…</pre></details>`);
  if (image || r.pages.length) showPreview(image ? 'image' : 'page', image ? id : r.pages[0]);
  try {
    const info = await api(`/api/document/${docid}/object/${id}`);
    if (state.selection?.kind === 'object' && state.selection.id === id) {
      $('dictionary-text').textContent = `Object ${id}\n` + info.dictionary;
      const pixels = [...document.querySelectorAll('#inspector-body .facts dt')].find(dt => dt.textContent === 'Pixels');
      if (pixels && info.image) pixels.nextElementSibling.textContent = info.image.width ? `${num(info.image.width)} × ${num(info.image.height)}` : '—';
    }
  } catch (e) { if ($('dictionary-text')) $('dictionary-text').textContent = e.message }
}
function selectPage(number) {
  const p = state.doc.pages[number - 1]; if (!p) return;
  state.selection = {kind: 'page', id: number}; renderTable();
  const objects = p.objects.map(x => state.doc.byId.get(x)).filter(Boolean).sort((a, b) => b.bytes - a.bytes).slice(0, 6);
  openInspector(`<p class="detail-kind">${p.label && p.label !== String(number) ? `Printed page ${esc(p.label)}` : 'Page'}</p><h2>Page ${number}</h2><p class="detail-size">${size(p.referencedBytes)} with everything it uses</p>
    <div class="preview" id="preview-frame"></div>
    ${facts([['Only on this page', size(p.exclusiveBytes)], ['Shared with other pages', size(p.sharedBytes)]])}
    <button class="button quiet wide" data-preview-compress="${number}">${icon('compress')}Preview compression</button>
    <button class="text-button keep-toggle" data-keep-page="${number}">${keptPages().includes(number) ? 'Allow changes to this page' : 'Keep this page as it is'}</button>
    ${objects.length ? `<h3>Largest parts</h3><div class="part-list">${objects.map(r => `<button data-object="${r.id}">${swatch(r.category)}<span>${esc(objName(r))}</span><span>${size(r.bytes)}</span></button>`).join('')}</div>` : ''}`);
  showPreview('page', number);
}

// Compression ----------------------------------------------------------------
function compressJob() { const c = state.compress; return c && (c.status === 'running' || c.doc === state.doc?.id) ? c : null }
function badge() {
  const c = state.compress, running = c?.status === 'running';
  $('compress-badge').textContent = c?.status === 'ready' && c.doc === state.doc?.id ? 'Ready' : '';
  $('compress-badge').classList.toggle('working', running); $('compress-badge').title = running ? 'Making a smaller copy…' : '';
}
function preset() { return $('compress-form').preset.value }
function renderCompress() {
  const c = compressJob(), running = c?.status === 'running', ready = c?.status === 'ready';
  $('compress-form').hidden = running || ready; $('compress-progress').hidden = !running; $('compress-result').hidden = !ready;
  const limited = $('use-limit').checked;
  $('target-mb').disabled = !limited; $('target-mb').required = limited; $('limit-hint').hidden = !limited; $('split-option').hidden = !limited;
  $('quality-legend').textContent = limited ? 'Lowest quality allowed' : 'Quality';
  if (!$('target-mb').value && state.doc) $('target-mb').value = Math.max(0.1, Math.round(state.doc.bytes / 2e6 * 10) / 10);
  if (running) {
    $('compress-phase').textContent = c.phase || 'Starting…';
    if (c.total > 1) { $('compress-bar').max = c.total; $('compress-bar').value = c.completed } else $('compress-bar').removeAttribute('value');
    $('compress-count').textContent = (c.doc !== state.doc?.id ? c.name + ' · ' : '') + (c.total > 1 ? `${Math.floor(100 * c.completed / c.total)}%` : '');
  }
  if (ready) renderResult(c);
  if (!running && !ready) { renderSuggestions(); renderExtras(); renderSample() }
}
function pageWeight(p) { return p.objects.reduce((sum, x) => { const o = state.doc.byId.get(x); return o && ['content', 'images', 'forms'].includes(o.category) && o.pages.length === 1 ? sum + o.bytes : sum }, 0) }
function renderSuggestions() {
  const d = state.doc;
  if (d.heaviest?.doc !== d.id) d.heaviest = {doc: d.id, pages: d.pages.map(p => ({number: p.number, bytes: pageWeight(p)})).filter(p => p.bytes > 0).sort((a, b) => b.bytes - a.bytes).slice(0, 3)};
  $('sample-suggestions').innerHTML = d.heaviest.pages.map(p => `<button type="button" class="chip" data-sample-page="${p.number}">Page ${p.number}</button>`).join('');
  $('sample-page').max = d.pageCount;
}
async function runSample(number) {
  const d = state.doc;
  if (!d || !(number >= 1 && number <= d.pageCount)) { notice(`Choose a page from 1 to ${num(d?.pageCount ?? 1)}.`); return }
  notice(''); $('sample-page').value = number;
  state.sample = {doc: d.id, page: number, loading: true}; renderSample();
  try {
    const data = await api(`/api/document/${d.id}/sample`, post({page: number}));
    if (state.sample?.doc === d.id && state.sample.page === number) { state.sample.data = data; state.sample.loading = false; renderSample() }
  } catch (err) { if (state.sample?.page === number) { state.sample = null; renderSample() } notice(err.message) }
}
function renderSample() {
  const s = state.sample?.doc === state.doc?.id ? state.sample : null, box = $('sample-view');
  document.querySelectorAll('.chip').forEach(b => b.classList.toggle('active', Number(b.dataset.samplePage) === s?.page));
  for (const el of document.querySelectorAll('[data-sample-for]')) {
    const r = s?.data?.results.find(x => x.preset === el.dataset.sampleFor);
    el.textContent = !s ? '' : s.loading ? `Page ${s.page}…` : !r ? `Page ${s.page}: unchanged` : CHANGED.includes(r.status) ? `Page ${s.page}: ${size(s.data.eligibleBytes)} → ${size(r.afterBytes)}` : `Page ${s.page}: stays as it is`;
  }
  if (!s) { box.hidden = true; box.innerHTML = ''; return }
  box.hidden = false;
  if (s.loading) { box.innerHTML = `<p class="pending">Preparing page ${s.page} at all three qualities…</p>`; return }
  const r = s.data.results.find(x => x.preset === preset());
  if (!r) { box.innerHTML = `<p class="pending">Lossless keeps every page exactly as it is. Choose another quality to preview the change.</p>`; return }
  if (keptPages().includes(s.page)) { box.innerHTML = `<p class="pending">You chose to keep page ${s.page} as it is.</p>`; return }
  const converted = CHANGED.includes(r.status);
  compare(box, {
    key: 'sample', page: s.page,
    note: converted ? `At ${PRESETS[r.preset]}, page ${s.page}’s ${r.status === 'reencoded' ? 'images take' : 'drawing takes'} <strong>${Math.round(100 * (1 - r.afterBytes / s.data.eligibleBytes))}% less</strong> space.` : `At ${PRESETS[r.preset]}, page ${s.page} stays as it is: ${esc(r.reason.toLowerCase())}.`,
    left: ['Original', w => `/api/document/${s.doc}/preview/page/${s.page}?width=${w}`],
    right: [converted ? PRESETS[r.preset] : `${PRESETS[r.preset]}, unchanged`, w => converted ? `/api/document/${s.doc}/sample/${r.preset}/${s.page}?width=${w}` : `/api/document/${s.doc}/preview/page/${s.page}?width=${w}`],
  });
}
function compare(box, v) {
  const zoom = state.zoom[v.key] ?? 1, page = state.doc.pages[v.page - 1], aspect = page ? page.height / page.width : 1.3;
  box.innerHTML = `<div class="compare-bar"><p>${v.note}</p><div class="segmented" role="group" aria-label="Zoom">${[[1, 'Fit'], [2, '2×'], [4, '4×']].map(([z, l]) => `<button type="button" data-zoom="${z}" data-zoom-key="${v.key}" class="${zoom === z ? 'active' : ''}">${l}</button>`).join('')}</div></div>
    <div class="compare"><figure><figcaption>${esc(v.left[0])}</figcaption><div class="pane"></div></figure><figure><figcaption>${esc(v.right[0])}</figcaption><div class="pane"></div></figure></div>`;
  const panes = box.querySelectorAll('.pane'), inner = panes[0].clientWidth;
  const height = Math.round(Math.min(inner * aspect, innerHeight * 0.72)), width = Math.min(3200, Math.round(inner * zoom * (devicePixelRatio || 1)));
  [v.left, v.right].forEach(([label, src], i) => {
    const pane = panes[i], image = new Image();
    pane.style.height = height + 'px';
    image.alt = `Page ${v.page}, ${label}`; image.style.width = zoom === 1 ? `${Math.min(100, 100 * height / (inner * aspect))}%` : zoom * 100 + '%';
    image.onerror = () => pane.innerHTML = '<p>Preview unavailable.</p>';
    image.src = src(width); pane.replaceChildren(image);
  });
  let syncing = false;
  const sync = (from, to) => () => { if (syncing) return; syncing = true; to.scrollTop = from.scrollTop; to.scrollLeft = from.scrollLeft; requestAnimationFrame(() => syncing = false) };
  panes[0].onscroll = sync(panes[0], panes[1]); panes[1].onscroll = sync(panes[1], panes[0]);
}
function renderResult(c) {
  const r = c.report, saved = r.originalBytes - r.bytes, k = r.checks;
  const changed = r.converted.slice().sort((a, b) => (b.eligibleBytes - after(b)) - (a.eligibleBytes - after(a)));
  const redrawn = changed.some(p => p.change !== 'images');
  const shown = state.showAllChanged ? changed : changed.slice(0, 8);
  const parts = r.parts ?? [];
  const status = parts.length ? `<p class="status ${parts.every(p => p.fits) ? 'ok' : 'warn'}">${icon(parts.every(p => p.fits) ? 'check' : 'alert')}One file couldn’t get under ${size(r.targetBytes)} at ${PRESETS[r.preset]} quality, so it’s split into ${num(parts.length)} parts${parts.every(p => p.fits) ? ' that each fit' : '; a single page is larger than the limit'}.</p>
      <table class="changed parts"><thead><tr><th>Part</th><th>Pages</th><th class="num">Size</th></tr></thead><tbody>${parts.map((p, i) => `<tr class="static"><td>Part ${i + 1}</td><td>${p.firstPage === p.lastPage ? p.firstPage : `${p.firstPage}–${p.lastPage}`}</td><td class="num strong">${size(p.bytes)}</td></tr>`).join('')}</tbody></table>`
    : r.targetBytes == null ? '' : r.targetMet
    ? `<p class="status ok">${icon('check')}Under your ${size(r.targetBytes)} limit${r.converted.length ? ` at ${PRESETS[r.preset]} quality` : ', without changing any page'}.</p>`
    : `<p class="status warn">${icon('alert')}Couldn’t get under ${size(r.targetBytes)}. This is the smallest copy at ${PRESETS[r.preset]} quality.</p>`;
  const textCheck = k.textPages === 0 ? `No text to compare: all ${num(k.pages)} pages are images` : k.textPages < k.pages ? `Text unchanged on all ${num(k.textPages)} pages that have text` : `Text unchanged on all ${num(k.pages)} pages`;
  const kept = r.kept.filter(x => x.reason !== 'small');
  $('compress-result').innerHTML = `<div class="sizes"><div><span>Original</span><strong>${size(r.originalBytes)}</strong></div>${icon('arrow')}<div class="new"><span>Smaller copy</span><strong>${size(r.bytes)}</strong></div><p>${saved > 0 ? `${Math.round(100 * saved / r.originalBytes)}% smaller` : 'Not smaller than the original'}</p></div>${status}
    <ul class="checks"><li>${icon('check')}${textCheck}</li><li>${icon('check')}Links${k.bookmarks ? ` and ${num(k.bookmarks)} bookmarks` : ''} unchanged</li><li>${icon('check')}Page sizes unchanged</li>${r.removed?.length ? `<li>${icon('check')}Removed: ${r.removed.map(x => EXTRAS[x][0].toLowerCase()).join(', ')}</li>` : ''}</ul>
    <div class="actions">${parts.length ? `<a class="button primary" href="/api/compress/${c.job}/download-parts" download>${icon('down')}Save ${num(parts.length)} parts (ZIP)</a><a class="button quiet" href="/api/compress/${c.job}/download" download>Save as one file</a>` : `<a class="button primary" href="/api/compress/${c.job}/download" download>${icon('down')}Save copy</a>`}<button class="button quiet" id="explore-copy" type="button">Open the copy</button><button class="text-button" id="new-compression" type="button">Try other settings</button></div>
    ${changed.length ? `<h3 class="result-heading">${num(changed.length)} of ${num(r.pages)} pages made smaller</h3><p class="note">Largest savings first. Select a page to compare it with the original.</p>
      <table class="changed"><thead><tr><th>Page</th><th class="num">Before</th><th class="num">After</th></tr></thead><tbody>${shown.map(p => `<tr tabindex="0" data-compare="${p.page}" class="${state.compare === p.page ? 'selected' : ''}"><td>Page ${p.page}${p.change === 'images' ? '<small> · images only</small>' : ''}</td><td class="num">${size(p.eligibleBytes)}</td><td class="num strong">${size(after(p))}</td></tr>`).join('')}</tbody></table>
      ${changed.length > shown.length ? `<button class="text-button" id="show-all-changed" type="button">Show all ${num(changed.length)}</button>` : ''}
      <div id="result-compare" class="compare-box" hidden></div>` : '<p class="note">No page needed changing; the savings come from cleanup without quality loss.</p>'}
    ${kept.length ? `<details class="kept"><summary>Why some pages stayed as they were</summary><ul>${kept.map(x => `<li><span>${esc(x.label)}</span><span>${num(x.pages)} page${x.pages === 1 ? '' : 's'}</span></li>`).join('')}</ul></details>` : ''}
    ${redrawn ? '<p class="note">Redrawn artwork is an image now, so it doesn’t stay sharp at extreme zoom. Keep the original if you need that.</p>' : ''}`;
  $('explore-copy').onclick = exploreCopy;
  $('new-compression').onclick = () => { state.compress = null; state.showAllChanged = false; state.compare = null; renderCompress(); badge() };
  if ($('show-all-changed')) $('show-all-changed').onclick = () => { state.showAllChanged = true; renderResult(c) };
  if (state.compare) showCompare(state.compare);
}
function showCompare(number) {
  const c = compressJob(); if (c?.status !== 'ready') return;
  const p = c.report.converted.find(x => x.page === number); if (!p) return;
  state.compare = number;
  document.querySelectorAll('[data-compare]').forEach(row => row.classList.toggle('selected', Number(row.dataset.compare) === number));
  const box = $('result-compare'); box.hidden = false;
  compare(box, {key: 'result', page: number, note: `Page ${number}: ${size(p.eligibleBytes)} → ${size(after(p))}`,
    left: ['Original', w => `/api/document/${c.doc}/preview/page/${number}?width=${w}`], right: ['Smaller copy', w => `/api/compress/${c.job}/preview/page/${number}?width=${w}`]});
  box.scrollIntoView({block: 'nearest'});
}
function parsePages(text, max) {
  const pages = new Set();
  for (const part of text.split(/[,;\s]+/).filter(Boolean)) {
    const m = part.match(/^(\d+)(?:[-–](\d+))?$/); if (!m) return null;
    const a = Number(m[1]), b = Number(m[2] ?? m[1]);
    if (a < 1 || b > max || a > b || b - a > 100000) return null;
    for (let n = a; n <= b; n++) pages.add(n);
  }
  return [...pages].sort((x, y) => x - y);
}
function keptPages() { return parsePages($('keep-pages').value, state.doc?.pageCount ?? 0) ?? [] }
function formatPages(pages) {
  const out = [];
  for (let i = 0; i < pages.length; i++) { let j = i; while (j + 1 < pages.length && pages[j + 1] === pages[j] + 1) j++; out.push(j > i ? `${pages[i]}–${pages[j]}` : `${pages[i]}`); i = j }
  return out.join(', ');
}
function toggleKeep(number) {
  const pages = new Set(keptPages()); pages.has(number) ? pages.delete(number) : pages.add(number);
  $('keep-pages').value = formatPages([...pages].sort((a, b) => a - b));
  if (state.selection?.kind === 'page') selectPage(state.selection.id);
}
function renderExtras() {
  const d = state.doc, list = $('extras-list');
  $('extras').hidden = !d.removable?.length;
  if (list.dataset.doc === d.id) return;
  list.dataset.doc = d.id;
  list.innerHTML = (d.removable ?? []).map(x => { const [label, detail, on] = EXTRAS[x.id]; return `<label class="check extra"><input type="checkbox" data-extra="${x.id}" ${on ? 'checked' : ''}><span><strong>${esc(label)} <small>${size(x.bytes)}${x.id === 'attachments' ? ` · ${num(x.count)} file${x.count === 1 ? '' : 's'}` : ''}</small></strong><small>${esc(detail)}</small></span></label>` }).join('');
}
async function startCompression(e) {
  e.preventDefault(); if (!state.doc) return;
  const keep = parsePages($('keep-pages').value, state.doc.pageCount);
  if (keep === null) { $('more-options').open = true; notice(`Pages to keep: use numbers and ranges from 1 to ${num(state.doc.pageCount)}, like 12, 40–45.`); return }
  const limited = $('use-limit').checked, body = {mode: limited ? 'target' : 'quality', preset: preset(), keepPages: keep,
    remove: [...document.querySelectorAll('[data-extra]:checked')].map(x => x.dataset.extra)};
  if (limited) { const mb = Number($('target-mb').value); if (!(mb > 0)) { notice('Enter a size in MB.'); return } body.targetBytes = Math.round(mb * 1e6); body.split = $('use-split').checked }
  notice('');
  try {
    const result = await api(`/api/document/${state.doc.id}/compress`, post(body));
    state.compress = {job: result.job, doc: state.doc.id, name: state.doc.name, status: 'running', phase: 'Starting…', completed: 0, total: 1};
    Object.assign(state, {showAllChanged: false, compare: null}); renderCompress(); badge(); pollCompression(result.job);
  } catch (err) { notice(err.message) }
}
async function pollCompression(job) {
  while (state.compress?.job === job) {
    let result;
    try { result = await api('/api/compress/' + job) } catch (err) { notice(err.message); return }
    if (state.compress?.job !== job) return;
    Object.assign(state.compress, result);
    if (result.status === 'error') { notice(result.error); state.compress = null }
    else if (result.status === 'cancelled') { notice('Stopped. Your PDF is unchanged.'); state.compress = null }
    if (state.view === 'compress') renderCompress();
    badge();
    if (result.status !== 'running') return;
    await new Promise(resolve => setTimeout(resolve, 700));
  }
}
async function cancelCompression() {
  const c = state.compress; if (c?.status !== 'running') return;
  $('cancel-compress').disabled = true;
  try { await api(`/api/compress/${c.job}/cancel`, post({})) } catch (err) { notice(err.message) } finally { $('cancel-compress').disabled = false }
}
async function exploreCopy() {
  const c = compressJob(); if (!c || state.busy) return;
  busy(true); notice(''); surface('loading');
  try { await awaitAnalysis((await api(`/api/compress/${c.job}/explore`, post({}))).job) }
  catch (err) { notice(err.message); surface('workspace') }
  finally { busy(false) }
}

// Organize pages ---------------------------------------------------------------
let organizeKey = 0;
const orgItem = (doc, page, extra = {}) => ({key: ++organizeKey, doc, page, rotate: 0, ...extra});
function organizeState() {
  const d = state.doc;
  if (state.org?.doc !== d.id) {
    state.org = {doc: d.id, items: d.pages.map(p => orgItem(d.id, p.number)), selected: new Set(), anchor: null, history: [],
      docs: {[d.id]: {name: d.name, pages: d.pages}}, result: null};
  }
  return state.org;
}
function pageInfo(o, item) { return item.blank ? {width: item.width, height: item.height} : o.docs[item.doc].pages[item.page - 1] }
function organizeChanges(o) {
  const own = o.items.filter(i => i.doc === o.doc), total = state.doc.pageCount;
  const parts = [];
  if (own.length < total) parts.push(`${num(total - own.length)} deleted`);
  const added = o.items.length - own.length; if (added) parts.push(`${num(added)} added`);
  const turned = o.items.filter(i => i.rotate).length; if (turned) parts.push(`${num(turned)} turned`);
  if (own.some((item, i) => i && item.page < own[i - 1].page)) parts.push('order changed');
  return parts;
}
function organizeStatus() {
  const o = state.org, changes = organizeChanges(o), count = o.selected.size;
  $('organize-summary').innerHTML = `<strong>${num(o.items.length)} page${o.items.length === 1 ? '' : 's'}</strong>${changes.length ? ' · ' + changes.join(' · ') : ''}${count ? ` · <span class="selected-count">${num(count)} selected</span>` : ''}`;
  document.querySelectorAll('[data-org]').forEach(b => { const a = b.dataset.org; b.disabled = o.busy || (['left', 'right', 'delete'].includes(a) && !count) || (a === 'undo' && !o.history.length) });
  $('organize-save').disabled = o.busy || !o.items.length || !changes.length;
  document.querySelectorAll('.page-tile').forEach(t => { const on = o.selected.has(Number(t.dataset.key)); t.classList.toggle('selected', on); t.setAttribute('aria-selected', on) });
}
function renderOrganize() {
  const o = organizeState();
  $('page-grid').innerHTML = o.items.map((item, index) => {
    const info = pageInfo(o, item), turned = item.rotate % 180 !== 0, w = turned ? info.height : info.width, h = turned ? info.width : info.height;
    const source = item.blank ? 'Blank' : item.doc !== o.doc ? `${esc(o.docs[item.doc].name)} · ${item.page}` : item.page !== index + 1 ? `was ${item.page}` : '';
    const image = item.blank ? '' : `<img alt="" loading="lazy" decoding="async" draggable="false" src="/api/document/${item.doc}/preview/page/${item.page}?width=240" style="width:${turned ? 100 * info.width / info.height : 100}%;transform:translate(-50%,-50%) rotate(${item.rotate}deg)">`;
    return `<div class="page-tile${o.selected.has(item.key) ? ' selected' : ''}" role="option" aria-selected="${o.selected.has(item.key)}" aria-label="Page ${index + 1}${source ? ', ' + source : ''}" tabindex="0" draggable="true" data-key="${item.key}"><div class="thumb${item.blank ? ' blank' : ''}" style="aspect-ratio:${w}/${h}">${image}</div><span class="page-num">${index + 1}</span>${source ? `<small>${source}</small>` : ''}</div>`;
  }).join('') || '<p class="empty-grid">No pages left. Undo, or insert pages from another PDF.</p>';
  organizeStatus();
}
function organizeEdit(change) {
  const o = state.org; o.history.push(o.items.slice()); if (o.history.length > 100) o.history.shift();
  change(o); o.result = null; $('organize-result').hidden = true; renderOrganize();
}
function insertionPoint(o) { const keys = o.items.map(i => i.key), last = Math.max(-1, ...[...o.selected].map(k => keys.indexOf(k))); return last >= 0 ? last + 1 : o.items.length }
function organizeAction(action) {
  const o = state.org; if (!o || o.busy) return;
  const turn = d => organizeEdit(o => { o.items = o.items.map(i => o.selected.has(i.key) ? {...i, rotate: (i.rotate + d + 360) % 360} : i) });
  if (action === 'left') turn(-90);
  else if (action === 'right') turn(90);
  else if (action === 'delete' && o.selected.size) organizeEdit(o => { o.items = o.items.filter(i => !o.selected.has(i.key)); o.selected.clear() });
  else if (action === 'blank') organizeEdit(o => {
    const at = insertionPoint(o), near = o.items[Math.max(0, at - 1)], info = near ? pageInfo(o, near) : {width: 595, height: 842};
    const blank = orgItem(null, null, {blank: true, width: Math.round(info.width), height: Math.round(info.height)});
    o.items.splice(at, 0, blank); o.selected = new Set([blank.key]);
  });
  else if (action === 'insert') $('insert-input').click();
  else if (action === 'undo' && o.history.length) { o.items = o.history.pop(); o.selected.clear(); o.result = null; $('organize-result').hidden = true; renderOrganize() }
}
function selectTile(key, e) {
  const o = state.org, keys = o.items.map(i => i.key);
  if (e.shiftKey && o.anchor !== null && keys.includes(o.anchor)) {
    const [a, b] = [keys.indexOf(o.anchor), keys.indexOf(key)].sort((x, y) => x - y);
    o.selected = new Set(keys.slice(a, b + 1));
  } else if (e.ctrlKey || e.metaKey) { o.selected.has(key) ? o.selected.delete(key) : o.selected.add(key); o.anchor = key }
  else { o.selected = new Set([key]); o.anchor = key }
  organizeStatus();
}
async function insertPdf(file) {
  const o = state.org; if (!o || !file) return;
  if (!openable(file.name)) { notice(cannotOpen(file.name)); return }
  o.busy = true; organizeStatus(); $('organize-summary').textContent = `Adding ${file.name}…`;
  try {
    const response = await fetch('/api/open', {method: 'POST', headers: {'X-Explorer-Token': state.config.token, 'X-Filename': encodeURIComponent(file.name)}, body: file});
    const opened = await response.json(); if (!response.ok) throw Error(opened.error);
    let status;
    do { await sleep(400); status = await api('/api/job/' + opened.job) } while (status.status === 'analysing');
    if (status.status !== 'ready') throw Error(status.error || 'Could not open that PDF.');
    const report = await api('/api/document/' + status.id);
    o.docs[status.id] = {name: report.name, pages: report.pages};
    o.busy = false;
    organizeEdit(o => { const added = report.pages.map(p => orgItem(status.id, p.number)); o.items.splice(insertionPoint(o), 0, ...added); o.selected = new Set(added.map(i => i.key)) });
  } catch (err) { notice(err.message); o.busy = false; organizeStatus() }
}
async function saveOrganized() {
  const o = state.org; if (!o || o.busy) return;
  o.busy = true; organizeStatus(); $('organize-save').textContent = 'Saving…'; notice('');
  try {
    const pages = o.items.map(i => i.blank ? {blank: true, width: i.width, height: i.height} : {doc: i.doc, page: i.page, rotate: i.rotate});
    o.result = await api(`/api/document/${o.doc}/organize`, post({pages}));
    const r = o.result, box = $('organize-result');
    box.innerHTML = `<p>${icon('check')}<span><strong>New PDF ready:</strong> ${num(r.pages)} page${r.pages === 1 ? '' : 's'}, ${size(r.bytes)} <small>(was ${size(state.doc.bytes)})</small>. Every unturned page was checked against its source.</span></p><div class="actions"><a class="button primary small" href="/api/organized/${r.id}/download" download>${icon('down')}Save PDF</a><button class="button quiet small" type="button" id="organized-open">Open it to make it smaller</button></div>`;
    box.hidden = false;
    $('organized-open').onclick = async () => {
      if (state.busy) return;
      busy(true); surface('loading');
      try { await awaitAnalysis((await api(`/api/organized/${r.id}/explore`, post({}))).job) } catch (err) { notice(err.message); surface('workspace') } finally { busy(false) }
    };
  } catch (err) { notice(err.message) }
  finally { o.busy = false; $('organize-save').textContent = 'Save as new PDF'; organizeStatus() }
}
let dragTarget = null;
function dropIndex(e) {
  const tile = e.target.closest('.page-tile');
  if (!tile) return {index: state.org.items.length, tile: null};
  const rect = tile.getBoundingClientRect(), after = e.clientX > rect.left + rect.width / 2;
  return {index: state.org.items.findIndex(i => i.key === Number(tile.dataset.key)) + (after ? 1 : 0), tile, after};
}
function markDrop(target) {
  document.querySelectorAll('.drop-before, .drop-after').forEach(t => t.classList.remove('drop-before', 'drop-after'));
  if (target?.tile) target.tile.classList.add(target.after ? 'drop-after' : 'drop-before');
}

// Split ----------------------------------------------------------------------
// Bookmark levels that give a different number of parts, deepest last.
function splitLevels(d) {
  const levels = [];
  for (let level = 1; level <= 4; level++) {
    const starts = new Set(d.outline.filter(([l]) => l <= level).map(([, , page]) => page));
    const parts = starts.size + (starts.size && Math.min(...starts) > 1 ? 1 : 0);
    if (parts > 1 && !levels.some(x => x.parts === parts)) levels.push({level, parts});
  }
  return levels;
}
function parseRanges(text, max) {
  const ranges = [];
  for (const part of text.split(/[,;]+/).map(t => t.trim()).filter(Boolean)) {
    const m = part.match(/^(\d+)\s*(?:([-–])\s*(\d*))?$/); if (!m) return null;
    const a = Number(m[1]), b = m[2] ? (m[3] ? Number(m[3]) : max) : a;
    if (a < 1 || b > max || a > b) return null;
    ranges.push([a, b]);
  }
  return ranges.length ? ranges : null;
}
function splitChoice() {
  const d = state.doc, mode = $('split-form').split.value;
  if (mode === 'every') {
    const n = Number($('split-every').value);
    return Number.isInteger(n) && n >= 1 && n < d.pageCount ? {mode, value: n} : {error: `Enter a number of pages from 1 to ${num(d.pageCount - 1)}.`};
  }
  if (mode === 'ranges') {
    const ranges = parseRanges($('split-ranges').value, d.pageCount);
    return ranges ? {mode, value: ranges} : {error: `Enter page ranges between 1 and ${num(d.pageCount)}, like 1–20, 21–48, 49–.`};
  }
  if (mode === 'bookmarks') return {mode, value: Number($('split-level').value)};
  const mb = Number($('split-size').value);
  return mb >= 0.01 ? {mode, value: Math.round(mb * 1e6)} : {error: 'Enter a part size in MB.'};
}
function splitPreview(choice) {
  const d = state.doc;
  if (choice.error) return choice.error;
  if (choice.mode === 'size') return 'Each part runs as long as it can while staying under that size, measured on the saved file.';
  let ranges;
  if (choice.mode === 'every') ranges = Array.from({length: Math.ceil(d.pageCount / choice.value)}, (_, i) => [i * choice.value + 1, Math.min((i + 1) * choice.value, d.pageCount)]);
  else if (choice.mode === 'ranges') ranges = choice.value;
  else {
    const starts = [...new Set(d.outline.filter(([l]) => l <= choice.value).map(([, , page]) => page))].sort((a, b) => a - b);
    if (starts[0] > 1) starts.unshift(1);
    ranges = starts.map((start, i) => [start, (starts[i + 1] ?? d.pageCount + 1) - 1]);
  }
  const shown = ranges.slice(0, 6).map(([a, b]) => a === b ? `${a}` : `${a}–${b}`).join(', ');
  return `Makes ${num(ranges.length)} PDF${ranges.length === 1 ? '' : 's'}: pages ${shown}${ranges.length > 6 ? ', …' : ''}.`;
}
function renderSplit() {
  const d = state.doc, form = $('split-form');
  if (state.splitDoc !== d.id) {
    const levels = splitLevels(d);
    Object.assign(state, {splitDoc: d.id, splitResult: null});
    $('split-every').value = Math.max(1, Math.min(10, d.pageCount - 1));
    $('split-level').innerHTML = levels.map(x => `<option value="${x.level}">${x.level} · ${num(x.parts)} parts</option>`).join('');
    $('split-bookmarks').hidden = !levels.length;
    if (!levels.length && form.split.value === 'bookmarks') form.split.value = 'every';
  }
  const choice = splitChoice();
  $('split-preview').textContent = splitPreview(choice);
  $('split-preview').classList.toggle('warn-text', !!choice.error);
  $('split-start').disabled = !!choice.error || !!state.splitting;
  const r = state.splitResult, box = $('split-result');
  box.hidden = !r;
  if (r) box.innerHTML = `<p class="status ok">${icon('check')}${num(r.parts.length)} PDFs ready. Every page was checked against your original.</p>
    <table class="changed parts"><thead><tr><th>Part</th><th>Pages</th><th class="num">Size</th><th class="num"><span class="visually-hidden">Save</span></th></tr></thead><tbody>${r.parts.map((p, i) => `<tr class="static"><td title="${esc(p.name)}">${esc(p.label ?? p.name)}</td><td>${p.firstPage === p.lastPage ? p.firstPage : `${p.firstPage}–${p.lastPage}`}</td><td class="num">${size(p.bytes)}</td><td class="num"><a class="text-button" href="/api/split/${r.id}/part/${i + 1}" download>Save</a></td></tr>`).join('')}</tbody></table>
    <div class="actions"><a class="button primary" href="/api/split/${r.id}/download" download>${icon('down')}Save all (ZIP)</a></div>`;
}
async function runSplit(e) {
  e.preventDefault();
  const d = state.doc, choice = splitChoice(); if (choice.error || state.splitting) return;
  Object.assign(state, {splitting: true, splitResult: null}); notice(''); renderSplit();
  $('split-start').lastChild.textContent = 'Splitting…';
  try { state.splitResult = await api(`/api/document/${d.id}/split`, post({mode: choice.mode, value: choice.value})) }
  catch (err) { notice(err.message) }
  finally { state.splitting = false; $('split-start').lastChild.textContent = 'Split into PDFs'; if (state.doc === d && state.view === 'split') renderSplit() }
}

// Several files ----------------------------------------------------------------
const BATCH_HINTS = {
  lossless: 'No redrawing. Only hidden data is removed and each file is repacked.',
  high: '200 DPI. Looks like the original at reading size.',
  balanced: '150 DPI. Fine lines soften when you zoom in.',
  compact: '110 DPI. Smallest; drawings look visibly softer.',
};
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
function startBatch(files) {
  const kinds = state.config?.converter ? 'PDFs and presentations' : 'PDFs';
  const usable = [...files].filter(f => openable(f.name) && f.size <= 1024 ** 3);
  if (!usable.length) { notice(`Choose ${kinds} smaller than 1 GB.`); return }
  state.batch ??= {items: [], preset: 'high', mode: 'compress', running: false};
  for (const file of usable) state.batch.items.push({file, name: file.name, bytes: file.size, status: 'waiting'});
  state.batch.combined = null;
  const skipped = files.length - usable.length;
  notice(skipped ? `${num(skipped)} file${skipped === 1 ? ' was' : 's were'} skipped: only ${kinds} up to 1 GB.` : '');
  surface('batch'); renderBatch();
}
// Upload and analyse one file of the list, once; presentations are converted first.
async function openItem(item) {
  if (item.doc) return item.doc;
  item.status = PRESENTATION.test(item.name) ? 'converting' : 'opening'; renderBatch();
  const response = await fetch('/api/open', {method: 'POST', headers: {'X-Explorer-Token': state.config.token, 'X-Filename': encodeURIComponent(item.name)}, body: item.file});
  const opened = await response.json(); if (!response.ok) throw Error(opened.error);
  item.analysis = opened.job;
  if (item.status === 'opening') { item.status = 'measuring'; renderBatch() }
  let status;
  do { await sleep(400); status = await api('/api/job/' + opened.job) } while (status.status === 'analysing');
  if (status.status !== 'ready') throw Error(status.error || 'Stopped');
  item.doc = status.id;
  return item.doc;
}
function batchStatus(item) {
  if (item.status === 'done') {
    const r = item.result, saved = Math.round(100 * (1 - r.bytes / r.originalBytes));
    return `<span class="strong">${size(r.bytes)}</span> <small>${saved > 0 ? `−${saved}%` : 'no smaller'}</small>${r.targetMet === false ? ' <small class="warn-text">over the limit</small>' : ''}`;
  }
  if (state.batch.mode === 'combine' && item.pages) return `${num(item.pages)} page${item.pages === 1 ? '' : 's'}`;
  return {waiting: '<small>Waiting</small>', opening: '<small>Opening…</small>', measuring: '<small>Measuring…</small>', converting: '<small>Converting…</small>', ready: '<small>Ready</small>',
    compressing: `<small>${esc(item.phase || 'Working')}${item.progress ? ` · ${item.progress}%` : '…'}</small>`,
    stopped: '<small>Stopped</small>', error: `<small class="warn-text" title="${esc(item.error)}">${state.batch.mode === 'combine' ? 'Couldn’t open it' : 'Couldn’t make it smaller'}</small>`}[item.status];
}
function renderBatch() {
  const b = state.batch; if (!b) return;
  const combine = b.mode === 'combine';
  document.querySelectorAll('[data-batch-mode]').forEach(x => { x.classList.toggle('active', x.dataset.batchMode === b.mode); x.disabled = b.running });
  $('batch-title').textContent = combine ? 'Combine into one PDF' : 'Make several PDFs smaller';
  $('batch-lede').textContent = combine ? 'Pages follow the order below, and each file becomes a bookmark. The originals stay as they are.' : 'Each file gets its own checked copy. The originals stay as they are.';
  $('batch-settings').hidden = combine; $('batch-hint').hidden = combine;
  $('batch-result-head').textContent = combine ? 'Pages' : 'Smaller copy';
  document.querySelectorAll('[data-batch-preset]').forEach(x => { x.classList.toggle('active', x.dataset.batchPreset === b.preset); x.disabled = b.running });
  $('batch-target').disabled = !$('batch-use-limit').checked || b.running;
  $('batch-use-limit').disabled = b.running;
  $('batch-hint').textContent = `${BATCH_HINTS[b.preset]} Hidden editing data and page thumbnails are removed too.`;
  const last = b.items.length - 1;
  $('batch-rows').innerHTML = b.items.map((item, i) => {
    const save = !combine && item.status === 'done' ? `<a class="text-button" href="/api/compress/${item.job}/download" download>Save</a>` : '';
    const move = combine ? `<button class="icon-button" type="button" data-batch-move="${i}" data-delta="-1" aria-label="Move ${esc(item.name)} up" ${b.running || i === 0 ? 'disabled' : ''}>${icon('up')}</button><button class="icon-button" type="button" data-batch-move="${i}" data-delta="1" aria-label="Move ${esc(item.name)} down" ${b.running || i === last ? 'disabled' : ''}>${icon('chevron-down')}</button>` : '';
    const remove = !b.running && (combine || item.status === 'waiting') ? `<button class="icon-button" type="button" data-batch-remove="${i}" aria-label="Remove ${esc(item.name)}">${icon('close')}</button>` : '';
    return `<tr class="static"><td>${icon('file')}<span>${esc(item.name)}</span></td><td class="num">${size(item.bytes)}</td><td class="num">${batchStatus(item)}</td><td class="num row-actions">${save}${move}${remove}</td></tr>`;
  }).join('');
  const done = b.items.filter(i => i.status === 'done'), waiting = b.items.some(i => i.status === 'waiting');
  $('batch-start').hidden = combine || !waiting || b.running; $('batch-stop').hidden = !b.running;
  $('combine-start').hidden = !combine || b.running || !!b.combined; $('combine-start').disabled = b.items.length < 2;
  $('batch-add').disabled = b.running; $('batch-clear').hidden = b.running;
  $('batch-save-all').hidden = combine || b.running || done.length < 2;
  $('batch-save-all').href = '/api/bundle?jobs=' + done.map(i => i.job).join(',');
  const r = combine && b.combined, box = $('combine-result');
  box.hidden = !r;
  if (r) {
    box.innerHTML = `<p>${icon('check')}<span><strong>Combined PDF ready:</strong> ${num(r.files)} files, ${num(r.pages)} pages, ${size(r.bytes)}. Every page was checked against its file.</span></p><div class="actions"><a class="button primary small" href="/api/organized/${r.id}/download" download>${icon('down')}Save PDF</a><button class="button quiet small" type="button" id="combined-open">Open it</button></div>`;
    $('combined-open').onclick = async () => {
      if (state.busy) return;
      busy(true); surface('loading');
      try { await awaitAnalysis((await api(`/api/organized/${r.id}/explore`, post({}))).job) } catch (err) { notice(err.message); surface('batch') } finally { busy(false) }
    };
  }
}
async function runCombine() {
  const b = state.batch; if (!b || b.running || b.items.length < 2) return;
  Object.assign(b, {running: true, stop: false, combined: null}); notice(''); renderBatch();
  try {
    for (const item of b.items) {
      if (b.stop) throw Error('Stopped. Your files are unchanged.');
      try { await openItem(item); item.pages ??= (await api('/api/document/' + item.doc)).pageCount; item.status = 'ready' }
      catch (err) { Object.assign(item, {status: b.stop ? 'stopped' : 'error', error: err.message}); throw err }
      renderBatch();
    }
    $('combine-start').hidden = true; $('batch-lede').textContent = 'Combining…';
    b.combined = await api('/api/merge', post({documents: b.items.map(i => i.doc)}));
  } catch (err) { notice(b.stop ? 'Stopped. Your files are unchanged.' : err.message) }
  finally { b.running = false; renderBatch() }
}
async function runBatch() {
  const b = state.batch; if (!b || b.running) return;
  const limited = $('batch-use-limit').checked, mb = Number($('batch-target').value);
  if (limited && !(mb > 0)) { notice('Enter a size in MB.'); return }
  Object.assign(b, {running: true, stop: false}); notice(''); renderBatch();
  for (const item of b.items.filter(i => i.status === 'waiting')) {
    if (b.stop) break;
    try {
      const doc = await openItem(item);
      const body = {mode: limited ? 'target' : 'quality', preset: b.preset, remove: ['editing', 'thumbnails']};
      if (limited) body.targetBytes = Math.round(mb * 1e6);
      item.job = (await api(`/api/document/${doc}/compress`, post(body))).job; item.status = 'compressing'; renderBatch();
      let result;
      do {
        await sleep(700); result = await api('/api/compress/' + item.job);
        Object.assign(item, {phase: result.phase, progress: result.total > 1 ? Math.floor(100 * result.completed / result.total) : 0}); renderBatch();
      } while (result.status === 'running');
      if (result.status !== 'ready') throw Error(result.error || 'Stopped');
      Object.assign(item, {status: 'done', result: result.report});
    } catch (err) { Object.assign(item, {status: b.stop ? 'stopped' : 'error', error: err.message}) }
    renderBatch();
  }
  b.running = false; renderBatch();
}
async function stopBatch() {
  const b = state.batch; if (!b?.running) return;
  b.stop = true; $('batch-stop').disabled = true;
  const current = b.items.find(i => ['opening', 'measuring', 'converting', 'compressing'].includes(i.status));
  try {
    if (current?.status === 'compressing') await api(`/api/compress/${current.job}/cancel`, post({}));
    else if (current?.analysis && !current.doc) await api(`/api/job/${current.analysis}/cancel`, post({}));
  } catch (err) { notice(err.message) } finally { $('batch-stop').disabled = false }
}

// Events ---------------------------------------------------------------------
document.addEventListener('click', e => {
  const t = e.target, at = s => t.closest(s);
  let el;
  if ((el = at('[data-preview-compress]'))) { setView('compress'); runSample(Number(el.dataset.previewCompress)) }
  else if ((el = at('[data-keep-page]'))) toggleKeep(Number(el.dataset.keepPage))
  else if ((el = at('[data-remove-extra]'))) { setView('compress'); $('more-options').open = true; const box = document.querySelector(`[data-extra="${el.dataset.removeExtra}"]`); if (box) box.checked = true }
  else if ((el = at('[data-view]'))) setView(el.dataset.view)
  else if (at('[data-reset]')) setView('map')
  else if ((el = at('[data-compare]'))) showCompare(Number(el.dataset.compare))
  else if ((el = at('[data-zoom]'))) { state.zoom[el.dataset.zoomKey] = Number(el.dataset.zoom); el.dataset.zoomKey === 'sample' ? renderSample() : showCompare(state.compare) }
  else if ((el = at('[data-sample-page]'))) runSample(Number(el.dataset.samplePage))
  else if (at('#sample-run')) runSample(Number($('sample-page').value))
  else if ((el = at('[data-preview-kind]'))) showPreview(el.dataset.previewKind, Number(el.dataset.number))
  else if ((el = at('[data-object]'))) selectObject(Number(el.dataset.object))
  else if ((el = at('[data-page]'))) selectPage(Number(el.dataset.page))
});
document.addEventListener('keydown', e => {
  if ((e.key === 'Enter' || e.key === ' ') && e.target.matches('tr[data-object],tr[data-page],tr[data-compare]')) { e.preventDefault(); e.target.click() }
  if (e.key === 'Escape' && !$('inspector').hidden) { closeInspector(); renderTable() }
  if (state.view === 'organize' && state.org && !$('workspace').hidden && !e.target.matches('input, select, textarea')) {
    const key = e.key.toLowerCase(), mod = e.ctrlKey || e.metaKey;
    if (mod && key === 'z') { e.preventDefault(); organizeAction('undo') }
    else if (mod && key === 'a') { e.preventDefault(); state.org.selected = new Set(state.org.items.map(i => i.key)); organizeStatus() }
    else if ((e.key === 'Delete' || e.key === 'Backspace') && state.org.selected.size) { e.preventDefault(); organizeAction('delete') }
    else if (!mod && key === 'l' && state.org.selected.size) organizeAction('left')
    else if (!mod && key === 'r' && state.org.selected.size) organizeAction('right')
    else if (e.key === 'Escape') { state.org.selected.clear(); organizeStatus() }
    else if ((e.key === 'Enter' || e.key === ' ') && e.target.matches('.page-tile')) { e.preventDefault(); selectTile(Number(e.target.dataset.key), e) }
  }
});
$('open-file').onclick = $('dropzone').onclick = () => $('file-input').click();
function openFiles(files) { files = [...files]; if (!files.length) return; if (files.length > 1 || !$('batch').hidden) startBatch(files); else upload(files[0]) }
$('file-input').onchange = e => { openFiles(e.target.files); e.target.value = '' };
$('cancel-open').onclick = async () => {
  if (state.xhr) { state.xhr.abort(); return }
  if (state.job) { $('cancel-open').disabled = true; try { await api(`/api/job/${state.job}/cancel`, post({})) } catch (e) { notice(e.message); $('cancel-open').disabled = false } }
};
$('search').oninput = e => { state.query = e.target.value; state.offset = 0; renderTable() };
$('sort').onchange = e => { state.sort = e.target.value; state.offset = 0; renderTable() };
$('category-filter').onchange = e => { state.category = e.target.value || null; state.offset = 0; renderTable() };
$('previous').onclick = () => { state.offset--; renderTable() };
$('next').onclick = () => { state.offset++; renderTable() };
$('clear-selection').onclick = () => { closeInspector(); renderTable() };
$('map-help').onclick = () => { $('accounting').hidden = !$('accounting').hidden };
$('close-help').onclick = () => { $('accounting').hidden = true };
$('export').onclick = () => {
  const {byId, heaviest, ...data} = state.doc;
  const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'}));
  a.download = state.doc.name.replace(/\.pdf$/i, '') + '-size-breakdown.json'; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
};
$('compress-form').onsubmit = startCompression;
$('organize-save').onclick = saveOrganized;
$('insert-input').onchange = e => { insertPdf(e.target.files[0]); e.target.value = '' };
$('organize-region').addEventListener('click', e => {
  const action = e.target.closest('[data-org]'), tile = e.target.closest('.page-tile');
  if (action) organizeAction(action.dataset.org);
  else if (tile) selectTile(Number(tile.dataset.key), e);
  else if (e.target.closest('#page-grid') && state.org) { state.org.selected.clear(); organizeStatus() }
});
$('page-grid').addEventListener('dragstart', e => {
  const tile = e.target.closest('.page-tile'); if (!tile || state.org.busy) return;
  const key = Number(tile.dataset.key);
  if (!state.org.selected.has(key)) { state.org.selected = new Set([key]); state.org.anchor = key; tile.classList.add('selected') }
  e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', 'pages');
});
$('page-grid').addEventListener('dragover', e => { if (!e.dataTransfer.types.includes('Files')) { e.preventDefault(); dragTarget = dropIndex(e); markDrop(dragTarget) } });
$('page-grid').addEventListener('dragleave', e => { if (!e.currentTarget.contains(e.relatedTarget)) markDrop(null) });
$('page-grid').addEventListener('drop', e => {
  if (e.dataTransfer.types.includes('Files')) return;
  e.preventDefault(); e.stopPropagation(); markDrop(null);
  const {index} = dropIndex(e);
  organizeEdit(o => {
    const moving = o.items.filter(i => o.selected.has(i.key)), before = o.items.slice(0, index).filter(i => !o.selected.has(i.key)).length;
    const rest = o.items.filter(i => !o.selected.has(i.key));
    o.items = [...rest.slice(0, before), ...moving, ...rest.slice(before)];
  });
});
$('page-grid').addEventListener('dragend', () => markDrop(null));
$('batch-start').onclick = runBatch;
$('combine-start').onclick = runCombine;
$('batch-mode').onclick = e => { const x = e.target.closest('[data-batch-mode]'); if (x && state.batch && !state.batch.running) { state.batch.mode = x.dataset.batchMode; notice(''); renderBatch() } };
$('batch-rows').onclick = e => {
  const b = state.batch, move = e.target.closest('[data-batch-move]'), remove = e.target.closest('[data-batch-remove]');
  if (!b || b.running) return;
  if (move) { const i = Number(move.dataset.batchMove), j = i + Number(move.dataset.delta); [b.items[i], b.items[j]] = [b.items[j], b.items[i]] }
  else if (remove) b.items.splice(Number(remove.dataset.batchRemove), 1);
  else return;
  b.combined = null;
  if (!b.items.length) { state.batch = null; surface(state.doc ? 'workspace' : 'welcome'); if (state.doc) render() } else renderBatch();
};
$('split-form').onsubmit = runSplit;
// Using a field picks its way of splitting.
for (const kind of ['focusin', 'input', 'change']) $('split-form').addEventListener(kind, e => {
  const mode = e.target.closest('.split-mode');
  if (mode && e.target.type !== 'radio') mode.querySelector('[type=radio]').checked = true;
  if (kind !== 'focusin' || mode) renderSplit();
});
$('batch-stop').onclick = stopBatch;
$('batch-add').onclick = () => $('file-input').click();
$('batch-clear').onclick = () => { state.batch = null; notice(''); surface(state.doc ? 'workspace' : 'welcome'); if (state.doc) render() };
$('batch-use-limit').onchange = renderBatch;
$('batch-quality').onclick = e => { const x = e.target.closest('[data-batch-preset]'); if (x && state.batch && !state.batch.running) { state.batch.preset = x.dataset.batchPreset; renderBatch() } };
$('compress-form').onchange = e => { if (e.target.id === 'use-limit') renderCompress(); if (e.target.name === 'preset') renderSample() };
$('cancel-compress').onclick = cancelCompression;
$('sample-page').onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); runSample(Number(e.target.value)) } };
let dragDepth = 0;
document.addEventListener('dragenter', e => { if (e.dataTransfer.types.includes('Files')) { e.preventDefault(); dragDepth++; $('drop-overlay').hidden = false } });
document.addEventListener('dragover', e => { if (e.dataTransfer.types.includes('Files')) e.preventDefault() });
document.addEventListener('dragleave', () => { if (--dragDepth <= 0) { dragDepth = 0; $('drop-overlay').hidden = true } });
document.addEventListener('drop', e => { e.preventDefault(); dragDepth = 0; $('drop-overlay').hidden = true; if (!state.busy && !state.batch?.running) openFiles(e.dataTransfer.files) });
let mapWidth = 0;
new ResizeObserver(() => { const w = $('map-scroll').clientWidth; if (w !== mapWidth) { mapWidth = w; renderMap() } }).observe($('map-scroll'));
async function init() {
  surface('welcome'); busy(true);
  try {
    state.config = await api('/api/config'); busy(false);
    if (state.config.converter) $('dropzone-title').textContent = 'Drop a PDF or a PowerPoint file here, or choose one';
    $('samples').innerHTML = state.config.samples.map(s => `<button class="sample" data-sample="${s.id}">${icon('file')}<span>${esc(s.name)}</span><small>${size(s.bytes)}</small></button>`).join('');
    document.querySelectorAll('[data-sample]').forEach(b => b.onclick = () => openSample(Number(b.dataset.sample)));
    const sample = new URLSearchParams(location.search).get('sample');
    if (sample !== null && state.config.samples.some(s => s.id === Number(sample))) openSample(Number(sample));
  } catch (e) { notice('Paperweight isn’t responding. Restart it and reload this window.'); $('open-file').disabled = true }
}
init();
