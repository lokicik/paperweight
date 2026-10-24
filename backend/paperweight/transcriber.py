"""Turn lecture recordings into transcript PDFs.

A recording is sent to AssemblyAI, transcribed with speaker labels, and
deleted there as soon as the words are back. An AssemblyAI JSON export (a list
of words, or a transcript with "words") is laid out without the service.

The PDF keeps every word. Paragraphs break at pauses and speaker changes; each
has its time and speaker in the margin, and bookmarks mark every five minutes.
"""
from collections import Counter
import html
import io
import json
import os
from pathlib import Path
import re
import threading
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pymupdf as fitz

API = 'https://api.assemblyai.com/v2'
RECORDINGS = {'.mp3', '.wav', '.m4a', '.aac', '.ogg', '.oga', '.opus', '.flac', '.wma', '.amr', '.webm',
              '.mp4', '.m4v', '.mov', '.mkv', '.avi', '.wmv', '.mpeg', '.mpg', '.3gp'}
LANGUAGES = {'en': 'English', 'tr': 'Turkish', 'de': 'German', 'fr': 'French', 'es': 'Spanish', 'it': 'Italian',
             'pt': 'Portuguese', 'nl': 'Dutch', 'ru': 'Russian', 'uk': 'Ukrainian', 'pl': 'Polish', 'ar': 'Arabic',
             'zh': 'Chinese', 'ja': 'Japanese', 'ko': 'Korean', 'hi': 'Hindi', 'sv': 'Swedish', 'el': 'Greek'}
BOOKMARK_EVERY = 5 * 60 * 1000  # milliseconds
LIGATURES = {'fb00': 'ff', 'fb01': 'fi', 'fb02': 'fl', 'fb03': 'ffi', 'fb04': 'ffl'}
SECTION = re.compile(r'(\d+)\s+beginbf(range|char)(.*?)endbf\2', re.S)
ENTRY = {'range': re.compile(r'<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*(<[0-9a-fA-F]+>|\[[^\]]*\])'),
         'char': re.compile(r'<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>')}

PAGE = fitz.paper_rect('a4')
LEFT, RIGHT, TOP, BOTTOM = 48, PAGE.width - 48, 50, PAGE.height - 58
TEXT = LEFT + 60  # times and speakers sit in the 50 pt margin column before it
CSS = '''
body { margin: 0; color: #1d232b; font-family: serif; font-size: 10.7pt; line-height: 1.6; }
p { margin: 0; }
.student { font-style: italic; }
.kicker { font-family: sans-serif; font-weight: bold; font-size: 9pt; color: #2f5d8a; margin-bottom: 4pt; }
h1 { font-family: sans-serif; font-size: 21pt; line-height: 1.15; margin: 0 0 6pt 0; }
.meta { font-family: sans-serif; font-size: 9pt; line-height: 1.55; color: #66707d; padding-bottom: 8pt; border-bottom: 1px solid #dde2e8; }
.meta b { color: #1d232b; }
.label { font-family: sans-serif; text-align: right; line-height: 1.25; }
.time { font-size: 7.8pt; font-weight: bold; color: #66707d; }
.who { font-size: 7.4pt; font-weight: bold; color: #2f5d8a; }
.who.student { font-style: normal; color: #a2471c; }
.foot { font-family: sans-serif; font-size: 7pt; color: #9aa1aa; }
.number { font-family: sans-serif; font-size: 8pt; color: #6b7585; text-align: right; }
'''


class Cancelled(Exception):
    pass


def is_recording(name):
    return Path(name).suffix.lower() in RECORDINGS


def is_transcript(name):
    return Path(name).suffix.lower() == '.json'


def api_key():
    """ASSEMBLYAI_API_KEY, or the key built into this copy by build_sidecar.py."""
    key = os.environ.get('ASSEMBLYAI_API_KEY', '').strip()
    if key:
        return key
    try:
        from paperweight._key import KEY, MASK
    except ImportError:
        return None
    return bytes(k ^ m for k, m in zip(KEY, MASK)).decode()


def available():
    """'AssemblyAI' when recordings can be transcribed, else None."""
    return 'AssemblyAI' if api_key() else None


def call(method, path, key, body=None, headers=None, timeout=60):
    request = Request(API + path, data=body, method=method, headers={'Authorization': key, **(headers or {})})
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except HTTPError as exc:
        if exc.code == 401:
            raise ValueError('AssemblyAI did not accept the transcription key in this copy of Paperweight.') from None
        try:
            reason = json.loads(exc.read()).get('error')
        except (ValueError, AttributeError):
            reason = None
        raise ValueError(f'AssemblyAI could not transcribe this recording. {reason or ""}'.strip()) from None
    except (URLError, OSError):
        raise ValueError('Could not reach AssemblyAI. Check the internet connection and try again.') from None


def transcribe(source, progress=lambda phase, completed, total: None, stop=None, poll=3):
    """The finished AssemblyAI transcript of the recording at `source`.

    AssemblyAI keeps no copy: the transcript, and with it the uploaded
    recording, is deleted there once it is read, or when `stop` is set."""
    key = api_key()
    if not key:
        raise ValueError('Transcribing recordings is not set up in this copy of Paperweight.')
    stop = stop or threading.Event()
    total = Path(source).stat().st_size
    if total == 0:
        raise ValueError('This recording is empty.')

    def chunks():
        sent = 0
        with open(source, 'rb') as data:
            while chunk := data.read(1024 * 1024):
                if stop.is_set():
                    raise Cancelled()
                yield chunk
                sent += len(chunk)
                progress('Uploading the recording', sent, total)
    progress('Uploading the recording', 0, total)
    upload = call('POST', '/upload', key, chunks(), {'Content-Type': 'application/octet-stream', 'Content-Length': str(total)}, timeout=300)
    progress('Transcribing with AssemblyAI. A lecture takes a few minutes', 0, 0)
    request = {'audio_url': upload['upload_url'], 'speaker_labels': True, 'language_detection': True}
    job = call('POST', '/transcript', key, json.dumps(request).encode(), {'Content-Type': 'application/json'})
    try:
        while job['status'] not in ('completed', 'error'):
            if stop.wait(poll):
                raise Cancelled()
            job = call('GET', '/transcript/' + job['id'], key)
        if job['status'] == 'error':
            raise ValueError(f'AssemblyAI could not transcribe this recording. {job.get("error") or ""}'.strip())
        return job
    finally:
        try:
            call('DELETE', '/transcript/' + job['id'], key)
        except ValueError:
            pass  # AssemblyAI may refuse while still processing; nothing more to do here.


def words_of(data):
    """The timed words of an AssemblyAI transcript or JSON export."""
    if isinstance(data, dict):
        data = data.get('words')
    if not isinstance(data, list):
        raise ValueError('This JSON file is not an AssemblyAI transcript with words.')
    words = []
    for word in data:
        if not isinstance(word, dict) or not isinstance(word.get('text'), str) or not isinstance(word.get('start'), (int, float)):
            raise ValueError('This JSON file is not an AssemblyAI transcript with words.')
        if word['text'].strip():
            end = word.get('end')
            words.append({'text': word['text'].strip(), 'start': int(word['start']),
                          'end': int(end) if isinstance(end, (int, float)) else int(word['start']), 'speaker': word.get('speaker')})
    if not words:
        raise ValueError('No words were heard in this recording.')
    return words


def paragraphs(words):
    """Break at pauses and speaker changes once a sentence ends, and keep paragraphs readable in length."""
    groups, current = [], []
    for word in words:
        if current:
            gap, size = word['start'] - current[-1]['end'], len(current)
            other = word['speaker'] != current[0]['speaker']
            ended = current[-1]['text'].rstrip('"\')”’')[-1:] in ('.', '?', '!', '…') and not word['text'][:1].islower()
            if (ended and (other or gap > 2500 or (gap > 900 and size > 70) or size > 150)) or (other and gap > 1500) or size >= 250:
                groups.append(current)
                current = []
        current.append(word)
    groups.append(current)
    return [{'start': g[0]['start'], 'speaker': g[0]['speaker'], 'text': ' '.join(w['text'] for w in g)} for g in groups]


def speaker_names(words):
    """Lecturer and Student when one voice does most of the talking; no labels for a single voice."""
    counts = Counter(w['speaker'] for w in words if w['speaker'] is not None)
    if len(counts) < 2:
        return {}
    main, spoken = counts.most_common(1)[0]
    if spoken >= 0.6 * sum(counts.values()):
        return {s: 'Lecturer' if s == main else 'Student' for s in counts}
    return {s: f'Speaker {s}' for s in counts}


def clock(ms):
    s = ms // 1000
    return f'{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}'


def layout(target, title, header, paras, names, pages=None):
    """Draw the transcript; returns the page count and where each paragraph starts."""
    writer = fitz.DocumentWriter(target)
    state = {'page': 0, 'device': None}

    def put(markup, rect):
        story = fitz.Story(markup, user_css=CSS)
        story.place(rect)
        story.draw(state['device'])

    def finish_page():
        put(f'<div class="foot">{html.escape(title)}</div>', fitz.Rect(LEFT, BOTTOM + 22, RIGHT - 60, BOTTOM + 40))
        if pages:
            put(f'<div class="number">{state["page"]} / {pages}</div>', fitz.Rect(RIGHT - 60, BOTTOM + 21, RIGHT, BOTTOM + 40))
        writer.end_page()

    def new_page():
        if state['device'] is not None:
            finish_page()
        state['device'] = writer.begin_page(PAGE)
        state['page'] += 1
        return TOP

    y = new_page()
    story = fitz.Story(header, user_css=CSS)
    _, filled = story.place(fitz.Rect(LEFT, y, RIGHT, BOTTOM))
    story.draw(state['device'])
    y = fitz.Rect(filled).y1 + 12
    starts = []
    for p in paras:
        if BOTTOM - y < 40:  # room for two lines, so a time never sits alone at the foot of a page
            y = new_page()
        starts.append((state['page'], y))
        who = names.get(p['speaker'])
        student = ' student' if who == 'Student' else ''
        label = f'<div class="time">{clock(p["start"])}</div>' + (f'<div class="who{student}">{html.escape(who.upper())}</div>' if who else '')
        put(f'<div class="label">{label}</div>', fitz.Rect(LEFT, y + 2, TEXT - 10, y + 40))
        story = fitz.Story(f'<p class="{student.strip()}">{html.escape(p["text"])}</p>', user_css=CSS)
        while True:
            more, filled = story.place(fitz.Rect(TEXT, y, RIGHT, BOTTOM))
            story.draw(state['device'])
            if not more:
                break
            y = new_page()
        y = fitz.Rect(filled).y1 + 6.5
    finish_page()
    writer.close()
    return state['page'], starts


def unfold_ligatures(doc):
    """Map ligature glyphs to their letters in each font's ToUnicode map, so
    copied and searched text reads "fi" rather than "ﬁ"."""
    utf16 = lambda letters: ''.join(f'{ord(c):04x}' for c in letters)
    for xref in range(1, doc.xref_length()):
        kind, value = doc.xref_get_key(xref, 'ToUnicode')
        if kind != 'xref':
            continue
        target = int(value.split()[0])
        cmap, unfolded = doc.xref_stream(target).decode('latin-1'), []

        def section(match):
            kind, kept = match.group(2), []
            for entry in ENTRY[kind].finditer(match.group(3)):
                low, *rest = entry.groups()
                if kind == 'char':
                    letters = LIGATURES.get(rest[0].lower())
                    kept.append(f'<{low}> <{utf16(letters)}>' if letters else entry.group(0))
                    continue
                high, first = int(rest[0], 16), rest[1]
                span = range(int(low, 16), high + 1)
                start = int(first[1:-1], 16) if len(first) == 6 and first[0] == '<' else None
                if start is None or not any(f'{start + i:04x}' in LIGATURES for i in range(len(span))):
                    kept.append(entry.group(0))
                    continue
                unfolded.extend(f'<{code:0{len(low)}x}> <{utf16(LIGATURES.get(f"{start + i:04x}", chr(start + i)))}>'
                                for i, code in enumerate(span))
            return f'{len(kept)} beginbf{kind}\n' + '\n'.join(kept) + f'\nendbf{kind}' if kept else ''
        cmap = SECTION.sub(section, cmap)
        if unfolded:
            blocks = [unfolded[i:i + 100] for i in range(0, len(unfolded), 100)]
            extra = ''.join(f'{len(b)} beginbfchar\n' + '\n'.join(b) + '\nendbfchar\n' for b in blocks)
            cmap = cmap.replace('endcmap', extra + 'endcmap', 1)
        doc.update_stream(target, cmap.encode('latin-1'))


def write_pdf(words, destination, title, language=None):
    """Lay out a transcript PDF: every word, in paragraphs, with times, speakers and bookmarks."""
    paras, names = paragraphs(words), speaker_names(words)
    seconds = words[-1]['end'] // 1000
    length = f'{seconds // 3600} h {seconds % 3600 // 60} min' if seconds >= 3600 else f'{max(1, round(seconds / 60))} min'
    facts = [f'<b>Length:</b> {length}', f'<b>Words:</b> {len(words):,}']
    if language:
        facts.append(f'<b>Language:</b> {html.escape(LANGUAGES.get(language.split("_")[0].lower(), language))}')
    who = ' Speaker labels come from automatic recognition and may occasionally be wrong.' if names else ''
    header = (f'<div class="kicker">LECTURE TRANSCRIPT</div><h1>{html.escape(title)}</h1><div class="meta">{" &#183; ".join(facts)}<br>'
              f'The complete recording, word for word, in paragraphs with the time each one starts. Transcribed '
              f'automatically by AssemblyAI, so names and technical terms may be misheard.{who}</div>')
    pages = None
    for _ in range(2):  # the second pass knows the page count for "3 / 21"
        buffer = io.BytesIO()
        pages, starts = layout(buffer, title, header, paras, names, pages)
    toc, mark = [], 0
    for p, (page, y) in zip(paras, starts):
        if p['start'] >= mark:
            toc.append([1, clock(p['start']), page, {'kind': fitz.LINK_GOTO, 'page': page - 1, 'to': fitz.Point(LEFT, y), 'zoom': 0}])
            mark = (p['start'] // BOOKMARK_EVERY + 1) * BOOKMARK_EVERY
    with fitz.open('pdf', buffer.getvalue()) as doc:
        doc.set_toc(toc)
        doc.set_metadata({'title': title, 'subject': 'Lecture transcript', 'creator': 'Paperweight'})
        doc.subset_fonts()
        unfold_ligatures(doc)
        doc.save(destination, garbage=3, deflate=True)
    return pages


def transcript_pdf(source, destination, title, progress=lambda phase, completed, total: None, stop=None):
    """Write a transcript PDF of a recording, or of an AssemblyAI JSON export."""
    if is_transcript(source):
        if Path(source).stat().st_size > 256 * 1024 ** 2:
            raise ValueError('This JSON file is too large to be a transcript.')
        try:
            data = json.loads(Path(source).read_text(encoding='utf-8-sig'))
        except ValueError:
            raise ValueError('This JSON file is not an AssemblyAI transcript.') from None
    else:
        data = transcribe(source, progress, stop)
    progress('Writing the transcript', 0, 0)
    language = data.get('language_code') if isinstance(data, dict) else None
    return write_pdf(words_of(data), destination, title, language)
