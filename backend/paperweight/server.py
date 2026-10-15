"""Local HTTP transport shared by the browser and the Tauri desktop shell."""
import argparse
from concurrent.futures import ProcessPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing
import os
from pathlib import Path
import queue
import secrets
import shutil
import sys
import tempfile
import threading
from urllib.parse import parse_qs, quote, unquote, urlparse
import uuid
import zipfile

from paperweight.analyzer import inspect_pdf, object_info, preview
from paperweight.compressor import CHANGED, ORDER, PRESETS, REASONS, REMOVALS, Cancelled, compress, prepare_page
from paperweight.converter import available as converter_available, convert, is_presentation
from paperweight.organizer import check_pages, file_name, merge, organize, split, split_by_size, split_plan

RESOURCES = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2]))


def wait_for_parent_close():
    """Wait for a shutdown message or EOF without locking the shared stdin pipe.

    A blocking Windows stdin read prevents frozen workers from initializing
    their inherited standard handles. Peek first so worker startup stays free.
    """
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        import msvcrt

        peek = ctypes.WinDLL('kernel32', use_last_error=True).PeekNamedPipe
        peek.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                         wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        peek.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
        available = wintypes.DWORD()
        pause = threading.Event()
        while peek(handle, None, 0, None, ctypes.byref(available), None):
            if available.value:
                return
            pause.wait(0.2)
    else:
        import select
        select.select([sys.stdin], [], [])


def analyze_to_file(source, destination, events):
    """A separately cancellable process; the input is always read-only."""
    try:
        def progress(phase, completed, total):
            events.put({'phase': phase, 'completed': completed, 'total': total})
        report = inspect_pdf(source, progress)
        Path(destination).write_text(json.dumps(report), encoding='utf-8')
        events.put({'status': 'ready'})
    except Exception as exc:
        events.put({'status': 'error', 'error': str(exc)})


def compress_to_file(source, destination, workdir, options, report_path, events, stop):
    """A separately cancellable process. Writes the copy only after it passes checks."""
    try:
        def progress(phase, completed, total):
            events.put({'phase': phase, 'completed': completed, 'total': total})

        def cancelled():
            if stop.is_set():
                raise Cancelled()
        report = compress(source, destination, workdir, options, progress, cancelled)
        Path(report_path).write_text(json.dumps(report), encoding='utf-8')
        events.put({'status': 'ready'})
    except Cancelled:
        events.put({'status': 'cancelled'})
    except (ValueError, RuntimeError) as exc:
        events.put({'status': 'error', 'error': str(exc)})
    except Exception:
        events.put({'status': 'error', 'error': 'Could not compress this PDF. Your original is unchanged.'})


class Engine:
    def __init__(self, directory, samples=()):
        self.directory = Path(directory)
        self.samples = [Path(p).resolve() for p in samples]
        self.token = secrets.token_urlsafe(32)
        self.context = multiprocessing.get_context('spawn')
        self.previews = ProcessPoolExecutor(max_workers=1, mp_context=self.context)
        self.jobs = {}
        self.documents = {}
        self.compressions = {}
        self.page_samples = {}
        self.organized = {}
        self.splits = {}
        self.sampler = None
        self.lock = threading.RLock()
        self.converter = converter_available()

    def submit(self, source, name):
        """Analyse a PDF in a process of its own. A presentation is converted
        to PDF first, in a thread that can stop the converter."""
        with self.lock:
            ident = uuid.uuid4().hex
            job = self.jobs[ident] = {
                'path': Path(source), 'report_path': self.directory / (ident + '.json'), 'name': name,
                'status': 'analysing', 'phase': 'Opening PDF', 'completed': 0, 'total': 1,
            }
            if is_presentation(name):
                job.update(path=self.directory / (ident + '.pdf'), name=Path(name).stem + '.pdf',
                           convertedFrom=Path(name).name, phase=f'Converting slides with {self.converter}')
                threading.Thread(target=self.convert_then_analyse, args=(ident, Path(source)), daemon=True).start()
            else:
                self.start_analysis(ident)
            return ident

    def start_analysis(self, ident):
        job = self.jobs[ident]
        job['events'] = self.context.Queue()
        job['process'] = self.context.Process(target=analyze_to_file, args=(str(job['path']), str(job['report_path']), job['events']))
        job['process'].start()

    def convert_then_analyse(self, ident, source):
        job = self.jobs[ident]
        try:
            convert(source, job['path'], started=lambda process: job.__setitem__('converter', process))
        except Exception as exc:
            with self.lock:
                if job['status'] == 'analysing':
                    job.update(status='error', error=str(exc) if isinstance(exc, ValueError) else 'Could not convert this presentation.')
            return
        finally:
            source.unlink(missing_ok=True)
        with self.lock:
            if job['status'] == 'analysing':
                job['phase'] = 'Opening PDF'
                self.start_analysis(ident)

    def poll(self, ident):
        with self.lock:
            job = self.jobs.get(ident)
            if job is None:
                raise ValueError('Analysis not found.')
            if job['status'] == 'analysing' and 'process' in job:
                try:
                    while True:
                        job.update(job['events'].get_nowait())
                except queue.Empty:
                    pass
                # A final queue message can arrive just after process exit.
                if not job['process'].is_alive() and job['status'] == 'analysing':
                    if job['report_path'].exists():
                        job['status'] = 'ready'
                    else:
                        job['status'] = 'error'
                        job['error'] = 'Could not analyze this PDF. Try an unlocked, valid copy.'
            if job['status'] == 'ready' and ident not in self.documents:
                report = json.loads(job['report_path'].read_text(encoding='utf-8'))
                report.update(name=job['name'], id=ident)
                if 'convertedFrom' in job:
                    report.update(convertedFrom=job['convertedFrom'], converter=self.converter)
                self.documents[ident] = {'report': report, 'path': job['path']}
            return {key: job[key] for key in ('status', 'phase', 'completed', 'total', 'error') if key in job} | {'id': ident}

    def cancel(self, ident):
        with self.lock:
            job = self.jobs.get(ident)
            if job is None:
                raise ValueError('Analysis not found.')
            if job['status'] == 'analysing':
                if 'process' in job and job['process'].is_alive():
                    job['process'].terminate()
                    job['process'].join(timeout=5)
                elif 'converter' in job:
                    job['converter'].kill()
                job['status'] = 'cancelled'
            return {'status': job['status']}

    def compress(self, document, options):
        """Start compressing an open document. One compression runs at a time."""
        with self.lock:
            entry = self.documents.get(document)
            if entry is None:
                raise ValueError('Open this PDF again.')
            if any(job['status'] == 'running' for job in self.compressions.values()):
                raise ValueError('Another compression is running. Wait for it or cancel it first.')
            mode = options.get('mode')
            preset = options.get('preset')
            if mode not in ('quality', 'target') or preset not in PRESETS:
                raise ValueError('Choose a compression mode and quality.')
            remove, keep = options.get('remove', []), options.get('keepPages', [])
            if not isinstance(remove, list) or not set(remove) <= REMOVALS:
                raise ValueError('Unknown cleanup option.')
            pages = entry['report']['pageCount']
            if not isinstance(keep, list) or len(keep) > pages or not all(isinstance(n, int) and 1 <= n <= pages for n in keep):
                raise ValueError('Pages to keep must be page numbers in this PDF.')
            settings = {'mode': mode, 'preset': preset, 'remove': remove, 'keepPages': keep,
                        'split': options.get('split') is True, 'name': entry['report']['name']}
            if mode == 'target':
                target = options.get('targetBytes')
                if not isinstance(target, (int, float)) or not 0 < target < 10**12:
                    raise ValueError('Enter a size limit in MB.')
                settings['targetBytes'] = int(target)
            ident = uuid.uuid4().hex
            workdir = self.directory / ('work-' + ident)
            output = self.directory / (ident + '-compressed.pdf')
            report_path = self.directory / (ident + '-compression.json')
            events, stop = self.context.Queue(), self.context.Event()
            process = self.context.Process(
                target=compress_to_file,
                args=(str(entry['path']), str(output), str(workdir), settings, str(report_path), events, stop),
            )
            self.compressions[ident] = {
                'process': process, 'events': events, 'stop': stop, 'output': output,
                'workdir': workdir, 'report_path': report_path, 'document': document,
                'name': entry['report']['name'], 'status': 'running', 'phase': 'Starting',
                'completed': 0, 'total': 1,
            }
            process.start()
            return ident

    def sample_page(self, document, number):
        """Prepare one page at every preset, without touching the rest of the PDF."""
        with self.lock:
            entry = self.documents.get(document)
            if entry is None:
                raise ValueError('Open this PDF again.')
            report = entry['report']
            if not isinstance(number, int) or not 1 <= number <= report['pageCount']:
                raise ValueError('Choose a page in this PDF.')
            sizes = entry.setdefault('sizes', {o['id']: o for o in report['objects']})
            # Like the compressor: the page's own drawing plus images and artwork no other page uses.
            own = [x for x in report['pages'][number - 1]['objects'] if x in sizes and len(sizes[x]['pages']) == 1]
            eligible = sum(sizes[x]['bytes'] for x in own if sizes[x]['category'] in ('content', 'images', 'forms'))
            images = [x for x in own if sizes[x]['category'] == 'images']
            workdir = self.directory / ('samples-' + document)
            workdir.mkdir(exist_ok=True)
            futures = [self.pool().submit(prepare_page, str(entry['path']), number, eligible, preset, str(workdir), True, images)
                       for preset in ORDER]
        results = []
        for preset, future in zip(ORDER, futures):
            record = future.result(timeout=180)
            self.page_samples[(document, preset, number)] = record
            converted = record['status'] in CHANGED
            results.append({
                'preset': preset, 'label': PRESETS[preset]['label'], 'dpi': PRESETS[preset]['dpi'],
                'status': record['status'], 'reason': None if converted else REASONS.get(record['status'], record['status']),
                'afterBytes': eligible - record['estimatedSaving'] if converted else eligible,
                'format': record.get('format'), 'seconds': record['seconds'],
            })
        return {'page': number, 'eligibleBytes': eligible, 'results': results}

    def sample_render(self, document, preset, number, width):
        record = self.page_samples.get((document, preset, number))
        if record is None or record['status'] not in CHANGED:
            raise ValueError('Preview this page first.')
        return self.previews.submit(preview, record['artifact'] + '.pdf', 'page', 1, width)

    def pool(self):
        if self.sampler is None:
            self.sampler = ProcessPoolExecutor(max_workers=len(ORDER), mp_context=self.context)
        return self.sampler

    def organize(self, document, pages):
        """Write a new PDF from reordered, rotated, inserted or removed pages."""
        with self.lock:
            entry = self.documents.get(document)
            if entry is None:
                raise ValueError('Open this PDF again.')
            check_pages(pages, {ident: doc['report']['pageCount'] for ident, doc in self.documents.items()})
            paths = {ident: str(doc['path']) for ident, doc in self.documents.items()
                     if ident == document or any(p.get('doc') == ident for p in pages)}
            ident = uuid.uuid4().hex
            output = self.directory / (ident + '-organized.pdf')
            future = self.pool().submit(organize, document, paths, pages, str(output))
        result = future.result(timeout=600)
        name = entry['report']['name']
        name = (name[:-4] if name.lower().endswith('.pdf') else name) + '-organized.pdf'
        self.organized[ident] = {'output': output, 'name': name}
        return result | {'id': ident, 'name': name}

    def merge(self, documents):
        """Combine whole open documents, in the given order, into a new PDF."""
        with self.lock:
            if not isinstance(documents, list) or not 2 <= len(documents) <= 500:
                raise ValueError('Choose at least two files to combine.')
            entries = [self.documents.get(d) if isinstance(d, str) else None for d in documents]
            if any(entry is None for entry in entries):
                raise ValueError('Open those files again.')
            ident = uuid.uuid4().hex
            output = self.directory / (ident + '-combined.pdf')
            names = [entry['report']['name'] for entry in entries]
            future = self.pool().submit(merge, [str(entry['path']) for entry in entries], names, str(output))
        result = future.result(timeout=1800)
        name = Path(names[0]).stem + '-combined.pdf'
        self.organized[ident] = {'output': output, 'name': name}
        return result | {'id': ident, 'name': name}

    def split(self, document, options):
        """Split an open document into several PDFs, zipped together too.

        options: {'mode': 'every'|'ranges'|'bookmarks'|'size', 'value': ...}"""
        with self.lock:
            entry = self.documents.get(document)
            if entry is None:
                raise ValueError('Open this PDF again.')
            report, mode, value = entry['report'], options.get('mode'), options.get('value')
            ident = uuid.uuid4().hex
            folder = self.directory / ('split-' + ident)
            stem = file_name(Path(report['name']).stem)
            if mode == 'size':
                if not isinstance(value, (int, float)) or not 10_000 <= value < 10**12:
                    raise ValueError('Enter a part size of at least 0.01 MB.')
                future = self.pool().submit(split_by_size, str(entry['path']), int(value), str(folder), stem)
            else:
                plan = split_plan(report['pageCount'], mode, value, report.get('outline', []))
                if len(plan) < 2:
                    raise ValueError('That would make just one part. Choose a smaller size or more ranges.')
                future = self.pool().submit(split, str(entry['path']), plan, str(folder), stem)
        parts = future.result(timeout=1800)
        archive = folder.with_suffix('.zip')
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_STORED) as bundle:
            for part in parts:
                bundle.write(folder / part['name'], part['name'])
        self.splits[ident] = {'folder': folder, 'archive': archive, 'parts': parts, 'name': stem + '-parts.zip'}
        return {'id': ident, 'parts': parts, 'name': stem + '-parts.zip'}

    def bundle(self, idents):
        """A ZIP of several finished copies, for saving a batch at once."""
        jobs = [self.compression(i) for i in idents]
        if not jobs or len(jobs) > 500 or any(job['status'] != 'ready' for job in jobs):
            raise ValueError('These copies are not ready.')
        path = self.directory / ('bundle-' + uuid.uuid4().hex + '.zip')
        names = set()
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as archive:
            for job in jobs:
                name, n = compressed_name(job['name']), 1
                while name.lower() in names:
                    n += 1
                    name = compressed_name(job['name'])[:-4] + f' ({n}).pdf'
                names.add(name.lower())
                archive.write(job['output'], name)
        return path

    def compression(self, ident):
        job = self.compressions.get(ident)
        if job is None:
            raise ValueError('Compression not found.')
        return job

    def poll_compression(self, ident):
        with self.lock:
            job = self.compression(ident)
            if job['status'] == 'running':
                try:
                    while True:
                        job.update(job['events'].get_nowait())
                except queue.Empty:
                    pass
                if not job['process'].is_alive() and job['status'] == 'running':
                    job['status'] = 'error'
                    job['error'] = 'Compression stopped unexpectedly. Your original is unchanged.'
                if job['status'] != 'running':
                    shutil.rmtree(job['workdir'], ignore_errors=True)
            if job['status'] == 'ready' and 'report' not in job:
                job['report'] = json.loads(job['report_path'].read_text(encoding='utf-8'))
            return {key: job[key] for key in ('status', 'phase', 'completed', 'total', 'error', 'report') if key in job} | {'id': ident}

    def cancel_compression(self, ident):
        with self.lock:
            job = self.compression(ident)
            if job['status'] == 'running':
                job['stop'].set()
                job['process'].join(timeout=15)
                if job['process'].is_alive():
                    job['process'].terminate()
                    job['process'].join(timeout=5)
                job['status'] = 'cancelled'
                job['output'].unlink(missing_ok=True)
                job['output'].with_suffix('.zip').unlink(missing_ok=True)
                shutil.rmtree(job['workdir'], ignore_errors=True)
            return {'status': job['status']}

    def close(self):
        for ident in list(self.jobs):
            self.cancel(ident)
        for ident in list(self.compressions):
            self.cancel_compression(ident)
        if self.sampler is not None:
            self.sampler.shutdown(wait=True, cancel_futures=True)
        self.previews.shutdown(wait=True, cancel_futures=True)
        for job in [*self.jobs.values(), *self.compressions.values()]:
            if 'process' in job:
                job['process'].join(timeout=5)
                job['events'].close()


def compressed_name(name):
    stem = name[:-4] if name.lower().endswith('.pdf') else name
    return stem + '-compressed.pdf'


class Handler(BaseHTTPRequestHandler):
    @property
    def engine(self):
        return self.server.engine

    def log_message(self, *args):
        pass

    def send(self, status, data, kind='application/json', cache=False):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        # Rendered pages of an open document never change; let thumbnails be reused.
        self.send_header('Cache-Control', 'private, max-age=3600' if cache else 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # The user may cancel an upload or close a preview.

    def send_pdf(self, path, name, kind='application/pdf'):
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(path.stat().st_size))
        self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(name))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            with path.open('rb') as data:
                shutil.copyfileobj(data, self.wfile, 1024 * 1024)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def valid_host(self):
        return self.headers.get('Host') in (
            f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'
        )

    def do_GET(self):
        if not self.valid_host():
            return self.send(403, {'error': 'Local access only.'})
        url = urlparse(self.path)
        path = url.path
        try:
            width = int(parse_qs(url.query).get('width', ['1000'])[0])
            if path == '/api/config':
                return self.send(200, {
                    'token': self.engine.token, 'converter': self.engine.converter,
                    'samples': [{'id': i, 'name': p.name, 'bytes': p.stat().st_size}
                                for i, p in enumerate(self.engine.samples) if p.exists()],
                })
            if path.startswith('/api/job/'):
                return self.send(200, self.engine.poll(path.rsplit('/', 1)[-1]))
            parts = path.strip('/').split('/')
            if len(parts) == 4 and parts[:2] == ['api', 'organized'] and parts[3] == 'download':
                job = self.engine.organized.get(parts[2])
                if job is None:
                    return self.send(404, {'error': 'Save the organized pages again.'})
                return self.send_pdf(job['output'], job['name'])
            if len(parts) >= 4 and parts[:2] == ['api', 'split']:
                job = self.engine.splits.get(parts[2])
                if job is None:
                    return self.send(404, {'error': 'Split the PDF again.'})
                if len(parts) == 4 and parts[3] == 'download':
                    return self.send_pdf(job['archive'], job['name'], 'application/zip')
                if len(parts) == 5 and parts[3] == 'part' and parts[4].isdigit() and 1 <= int(parts[4]) <= len(job['parts']):
                    name = job['parts'][int(parts[4]) - 1]['name']
                    return self.send_pdf(job['folder'] / name, name)
            if path == '/api/bundle':
                archive = self.engine.bundle(parse_qs(url.query).get('jobs', [''])[0].split(','))
                try:
                    return self.send_pdf(archive, 'smaller-pdfs.zip', 'application/zip')
                finally:
                    archive.unlink(missing_ok=True)
            if len(parts) >= 3 and parts[:2] == ['api', 'compress']:
                if len(parts) == 3:
                    return self.send(200, self.engine.poll_compression(parts[2]))
                job = self.engine.compression(parts[2])
                if job['status'] != 'ready':
                    return self.send(409, {'error': 'The compressed copy is not ready.'})
                if len(parts) == 4 and parts[3] == 'download':
                    return self.send_pdf(job['output'], compressed_name(job['name']))
                if len(parts) == 4 and parts[3] == 'download-parts' and job['output'].with_suffix('.zip').exists():
                    return self.send_pdf(job['output'].with_suffix('.zip'), compressed_name(job['name'])[:-4] + '-parts.zip', 'application/zip')
                if len(parts) == 6 and parts[3:5] == ['preview', 'page']:
                    result = self.engine.previews.submit(preview, str(job['output']), 'page', int(parts[5]), width)
                    return self.send(200, result.result(timeout=45), 'image/png')
            if len(parts) >= 3 and parts[:2] == ['api', 'document']:
                entry = self.engine.documents.get(parts[2])
                if entry is None:
                    return self.send(404, {'error': 'Open this PDF again.'})
                if len(parts) == 3:
                    return self.send(200, entry['report'])
                if len(parts) == 4 and parts[3] == 'download':
                    return self.send_pdf(entry['path'], entry['report']['name'])
                if len(parts) == 5 and parts[3] == 'object':
                    result = self.engine.previews.submit(object_info, str(entry['path']), int(parts[4]))
                    return self.send(200, result.result(timeout=40))
                if len(parts) == 6 and parts[3] == 'preview':
                    result = self.engine.previews.submit(preview, str(entry['path']), parts[4], int(parts[5]), width)
                    return self.send(200, result.result(timeout=45), 'image/png', cache=True)
                if len(parts) == 6 and parts[3] == 'sample':
                    result = self.engine.sample_render(parts[2], parts[4], int(parts[5]), width)
                    return self.send(200, result.result(timeout=45), 'image/png')
            files = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}
            if path in files:
                file = RESOURCES / 'web' / files[path]
                kind = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8'}[file.suffix]
                return self.send(200, file.read_bytes(), kind)
            self.send(404, {'error': 'Not found.'})
        except (ValueError, RuntimeError) as exc:
            self.send(422, {'error': str(exc)})
        except Exception:
            self.send(500, {'error': 'Could not read this PDF component. Try its page preview.'})

    def do_POST(self):
        if not self.valid_host() or not secrets.compare_digest(self.headers.get('X-Explorer-Token', ''), self.engine.token):
            return self.send(403, {'error': 'Reload the local app and try again.'})
        origin = self.headers.get('Origin')
        if origin and origin not in (f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'):
            return self.send(403, {'error': 'Local access only.'})
        target = None
        try:
            length = int(self.headers.get('Content-Length', '0'))
            path = urlparse(self.path).path
            if path.startswith('/api/job/') and path.endswith('/cancel'):
                return self.send(200, self.engine.cancel(path.split('/')[-2]))
            parts = path.strip('/').split('/')
            if len(parts) == 4 and parts[:2] == ['api', 'document'] and parts[3] == 'compress':
                if not 0 < length <= 4096:
                    raise ValueError('Invalid compression request.')
                options = json.loads(self.rfile.read(length))
                if not isinstance(options, dict):
                    raise ValueError('Invalid compression request.')
                return self.send(202, {'job': self.engine.compress(parts[2], options)})
            if len(parts) == 4 and parts[:2] == ['api', 'document'] and parts[3] == 'sample':
                if not 0 < length <= 1024:
                    raise ValueError('Invalid preview request.')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('Invalid preview request.')
                return self.send(200, self.engine.sample_page(parts[2], body.get('page')))
            if len(parts) == 4 and parts[:2] == ['api', 'document'] and parts[3] == 'organize':
                if not 0 < length <= 4 * 1024 * 1024:
                    raise ValueError('Invalid page list.')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('Invalid page list.')
                return self.send(200, self.engine.organize(parts[2], body.get('pages')))
            if len(parts) == 4 and parts[:2] == ['api', 'document'] and parts[3] == 'split':
                if not 0 < length <= 256 * 1024:
                    raise ValueError('Invalid split request.')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('Invalid split request.')
                return self.send(200, self.engine.split(parts[2], body))
            if path == '/api/merge':
                if not 0 < length <= 64 * 1024:
                    raise ValueError('Invalid list of files.')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('Invalid list of files.')
                return self.send(200, self.engine.merge(body.get('documents')))
            if len(parts) == 4 and parts[:2] == ['api', 'organized'] and parts[3] == 'explore':
                job = self.engine.organized.get(parts[2])
                if job is None:
                    raise ValueError('Save the organized pages again.')
                return self.send(202, {'job': self.engine.submit(job['output'], job['name'])})
            if len(parts) == 4 and parts[:2] == ['api', 'compress'] and parts[3] == 'cancel':
                return self.send(200, self.engine.cancel_compression(parts[2]))
            if len(parts) == 4 and parts[:2] == ['api', 'compress'] and parts[3] == 'explore':
                job = self.engine.compression(parts[2])
                if job['status'] != 'ready':
                    raise ValueError('The compressed copy is not ready.')
                return self.send(202, {'job': self.engine.submit(job['output'], compressed_name(job['name']))})
            if path == '/api/sample':
                if not 0 < length <= 1024:
                    raise ValueError('Invalid sample request.')
                index = json.loads(self.rfile.read(length))['id']
                if not isinstance(index, int) or not 0 <= index < len(self.engine.samples):
                    raise ValueError('Sample not found.')
                source = self.engine.samples[index]
                return self.send(202, {'job': self.engine.submit(source, source.name)})
            if path == '/api/open':
                if not 0 < length <= 1024**3:
                    return self.send(413, {'error': 'Choose a PDF smaller than 1 GB for this prototype.'})
                name = unquote(self.headers.get('X-Filename', 'document.pdf')).replace('\\', '/').rsplit('/', 1)[-1][:200]
                presentation = is_presentation(name)
                if presentation and self.engine.converter is None:
                    raise ValueError('Opening presentations needs Microsoft PowerPoint or LibreOffice on this computer.')
                # Office programs go by the extension, so a presentation keeps its own.
                target = self.engine.directory / (uuid.uuid4().hex + (Path(name).suffix.lower() if presentation else '.pdf'))
                with target.open('wb') as out:
                    remaining = length
                    while remaining:
                        data = self.rfile.read(min(1024 * 1024, remaining))
                        if not data:
                            raise ValueError('File transfer interrupted. Please select the PDF again.')
                        out.write(data)
                        remaining -= len(data)
                with target.open('rb') as inp:
                    head = inp.read(1024)
                if presentation and not head.startswith((b'PK', b'\xd0\xcf\x11\xe0')):
                    raise ValueError('This file is not a presentation PowerPoint can open.')
                if not presentation and b'%PDF-' not in head:
                    raise ValueError('This file is not a PDF. Please choose a PDF file.')
                return self.send(202, {'job': self.engine.submit(target, name)})
            self.send(404, {'error': 'Not found.'})
        except (ValueError, KeyError, IndexError, TypeError, OSError) as exc:
            if target is not None:
                target.unlink(missing_ok=True)
            self.send(400, {'error': str(exc)})


def main():
    parser = argparse.ArgumentParser(description='Paperweight local engine')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--managed', action='store_true', help='Exit when the desktop parent closes stdin')
    parser.add_argument('--sample', action='append', default=[], help='Optional local development sample')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='paperweight-') as cache:
        engine = Engine(cache, args.sample)
        server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
        server.engine = engine
        if args.managed:
            def monitor_parent():
                try:
                    wait_for_parent_close()
                finally:
                    server.shutdown()
            threading.Thread(target=monitor_parent, daemon=True).start()
        print(json.dumps({'event': 'ready', 'url': f'http://127.0.0.1:{server.server_port}/'}), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            engine.close()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
