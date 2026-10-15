"""Build new PDFs from pages of open documents: reorder, delete, rotate and
insert pages, combine whole documents, or split one into several.

The sources are never modified. Pages keep their links and bookmarks wherever
the pages they point to come along.
"""
from pathlib import Path
import re

import pymupdf as fitz

from paperweight.compressor import part_bytes, source_text, split_pdf

ROTATIONS = {0, 90, 180, 270}


def check_pages(pages, documents):
    """Validate a page list. `documents` maps document ids to page counts."""
    if not isinstance(pages, list) or not 0 < len(pages) <= 20_000:
        raise ValueError('Keep at least one page.')
    for entry in pages:
        if not isinstance(entry, dict):
            raise ValueError('Invalid page list.')
        if entry.get('blank'):
            if not all(isinstance(entry.get(k), (int, float)) and 36 <= entry[k] <= 14_400 for k in ('width', 'height')):
                raise ValueError('Blank pages need a size between 0.5 and 200 inches.')
            continue
        count = documents.get(entry.get('doc'))
        if count is None:
            raise ValueError('Open the PDF those pages came from again.')
        if not isinstance(entry.get('page'), int) or not 1 <= entry['page'] <= count:
            raise ValueError('A page in the list no longer exists.')
        if entry.get('rotate', 0) not in ROTATIONS:
            raise ValueError('Pages can only be turned in quarter turns.')


def organize(base, paths, pages, destination):
    """Write `pages` (in order) to `destination`.

    base: id of the document being organized; paths: {id: path}.
    Each entry is {'doc', 'page' (1-based), 'rotate'} or {'blank', 'width', 'height'}.
    """
    own = [entry['page'] - 1 for entry in pages if entry.get('doc') == base]
    others = {}
    doc = fitz.open(paths[base]) if own else fitz.open()
    try:
        if own:
            # select keeps links and bookmarks that point to kept pages.
            doc.select(own)
        for position, entry in enumerate(pages):
            if entry.get('blank'):
                doc.new_page(pno=position, width=entry['width'], height=entry['height'])
            elif entry['doc'] != base:
                if entry['doc'] not in others:
                    others[entry['doc']] = fitz.open(paths[entry['doc']])
                doc.insert_pdf(others[entry['doc']], from_page=entry['page'] - 1, to_page=entry['page'] - 1, start_at=position)
        for position, entry in enumerate(pages):
            if entry.get('rotate'):
                page = doc[position]
                page.set_rotation((page.rotation + entry['rotate']) % 360)
        doc.save(destination, garbage=2, deflate=True, use_objstms=1)
    finally:
        doc.close()
        for other in others.values():
            other.close()
    verify(paths, pages, destination)
    return {'bytes': Path(destination).stat().st_size, 'pages': len(pages)}


def verify(paths, pages, destination):
    """Every page that was not turned must read exactly like its source page."""
    sources = {}
    try:
        with fitz.open(destination) as result:
            if len(result) != len(pages):
                raise RuntimeError('The new PDF has the wrong number of pages. Your originals are unchanged.')
            for position, entry in enumerate(pages):
                if entry.get('blank') or entry.get('rotate'):
                    continue
                if entry['doc'] not in sources:
                    sources[entry['doc']] = fitz.open(paths[entry['doc']])
                if result[position].get_text() != sources[entry['doc']][entry['page'] - 1].get_text():
                    raise RuntimeError(f'Page {position + 1} did not come out right. Your originals are unchanged.')
    finally:
        for source in sources.values():
            source.close()


def merge(paths, names, destination):
    """Write the documents `paths` one after another to `destination`.

    Each document's bookmarks move under a new top-level bookmark named after
    its file `names`; links inside each document keep working."""
    toc, counts = [], []
    with fitz.open() as out:
        for path, name in zip(paths, names):
            with fitz.open(path) as doc:
                start = len(out)
                out.insert_pdf(doc)
                counts.append(len(doc))
                toc.append([1, Path(name).stem, start + 1])
                for level, title, page, *dest in doc.get_toc(simple=False):
                    entry = [level + 1, title, page + start if page > 0 else -1]
                    # Keep the position on the page, not just the page.
                    if page > 0 and dest and dest[0].get('kind') == fitz.LINK_GOTO and dest[0].get('to') is not None:
                        entry.append({'kind': fitz.LINK_GOTO, 'to': dest[0]['to'], 'zoom': dest[0].get('zoom', 0)})
                    toc.append(entry)
        out.set_toc(toc)
        out.save(destination, garbage=2, deflate=True, use_objstms=1)
    with fitz.open(destination) as result:
        if len(result) != sum(counts):
            raise RuntimeError('The combined PDF has the wrong number of pages. Your files are unchanged.')
        position = 0
        for path, count in zip(paths, counts):
            with fitz.open(path) as doc:
                for number in range(count):
                    if source_text(result[position + number]) != source_text(doc[number]):
                        raise RuntimeError(f'Page {position + number + 1} did not come out right. Your files are unchanged.')
            position += count
    return {'bytes': Path(destination).stat().st_size, 'pages': sum(counts), 'files': len(paths)}


def file_name(text):
    """`text` made safe as a file name on every system."""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip(' .')[:80] or 'part'


def split_plan(count, mode, value, outline=()):
    """[(first, last, title)] with 1-based inclusive pages.

    mode 'every': `value` pages per part; 'ranges': `value` is [[first, last], ...];
    'bookmarks': a new part starts at every bookmark of level `value` or above
    in `outline` ([level, title, page] entries)."""
    if mode == 'every':
        if not isinstance(value, int) or not 1 <= value <= count:
            raise ValueError(f'Choose a number of pages from 1 to {count}.')
        return [(first, min(first + value - 1, count), None) for first in range(1, count + 1, value)]
    if mode == 'ranges':
        if not isinstance(value, list) or not 0 < len(value) <= 1000:
            raise ValueError('Enter at least one page range.')
        plan = []
        for item in value:
            if (not isinstance(item, list) or len(item) != 2 or not all(isinstance(n, int) for n in item)
                    or not 1 <= item[0] <= item[1] <= count):
                raise ValueError(f'Page ranges must lie between 1 and {count}.')
            plan.append((item[0], item[1], None))
        return plan
    if mode == 'bookmarks':
        starts = {}
        for level, title, page in outline:
            if level <= value and 1 <= page <= count:
                starts[page] = title  # The deepest bookmark on a page names its part.
        if not starts:
            raise ValueError('This PDF has no bookmarks at that level.')
        pages = sorted(starts)
        plan = [(1, pages[0] - 1, None)] if pages[0] > 1 else []
        plan += [(first, (pages[i + 1] - 1 if i + 1 < len(pages) else count), starts[first]) for i, first in enumerate(pages)]
        return plan
    raise ValueError('Choose how to split.')


def split(path, plan, folder, stem):
    """Write each part of `plan` (see split_plan) as its own PDF in `folder`.

    Every page of every part is checked against the source."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    parts, used = [], set()
    with fitz.open(path) as doc:
        toc = doc.get_toc(simple=False)
        width = len(str(len(plan)))
        for index, (first, last, title) in enumerate(plan, 1):
            label = f'{index:0{width}d} {title}' if title else (f'pages {first}-{last}' if first != last else f'page {first}')
            name = file_name(f'{stem} - {label}')
            while name.lower() in used:
                name += ' (2)'
            used.add(name.lower())
            name += '.pdf'
            data = part_bytes(doc, first - 1, last - 1, toc)
            (folder / name).write_bytes(data)
            with fitz.open(folder / name) as copy:
                if len(copy) != last - first + 1 or any(source_text(copy[i]) != source_text(doc[first - 1 + i]) for i in range(len(copy))):
                    raise RuntimeError(f'Part {index} did not come out right. Your PDF is unchanged.')
            parts.append({'name': name, 'label': label, 'firstPage': first, 'lastPage': last, 'bytes': len(data)})
    return parts


def split_by_size(path, limit, folder, stem):
    """Split into consecutive parts that each stay under `limit` bytes, measured
    on the saved parts; a single page larger than the limit stays alone."""
    parts = split_pdf(path, limit, folder, stem, lambda *args: None, lambda: None)
    for index, part in enumerate(parts, 1):
        part['label'] = f'Part {index} of {len(parts)}'
    return parts
