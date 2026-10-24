"""Exercise the local transport, cancellation and temporary-file cleanup."""
import hashlib
import io
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import zipfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from paperweight.analyzer import fitz
from paperweight import server
from paperweight.server import Engine, Handler


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.source = Path(cls.temp.name) / 'source.pdf'
        with fitz.open() as doc:
            for number in range(30):
                doc.new_page().insert_text((50, 50), f'Page {number + 1}')
            doc.save(cls.source)
        cls.engine = Engine(cls.temp.name, [cls.source])
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.engine = cls.engine
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.engine.close()
        cls.temp.cleanup()

    def request(self, path, data=None, headers=None):
        request_headers = {'X-Explorer-Token': self.engine.token}
        request_headers.update(headers or {})
        request = Request(self.base + path, data=data, headers=request_headers)
        with urlopen(request, timeout=15) as response:
            return response.read()

    def test_upload_analyze_and_preview(self):
        original = self.source.read_bytes()
        result = json.loads(self.request('/api/open', original, {'X-Filename': 'example.pdf'}))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = json.loads(self.request('/api/job/' + result['job']))
            if status['status'] != 'analysing':
                break
            time.sleep(0.05)
        self.assertEqual(status['status'], 'ready')
        report = json.loads(self.request('/api/document/' + result['job']))
        self.assertEqual(report['name'], 'example.pdf')
        self.assertEqual(sum(c['bytes'] for c in report['categories']), len(original))
        png = self.request('/api/document/' + result['job'] + '/preview/page/1')
        self.assertTrue(png.startswith(b'\x89PNG'))
        self.assertEqual(hashlib.sha256(original).digest(), hashlib.sha256(self.source.read_bytes()).digest())

    def open_sample(self):
        job = json.loads(self.request('/api/sample', b'{"id":0}'))['job']
        deadline = time.monotonic() + 15
        while json.loads(self.request('/api/job/' + job))['status'] == 'analysing' and time.monotonic() < deadline:
            time.sleep(0.05)
        return job

    def wait_compression(self, job):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status = json.loads(self.request('/api/compress/' + job))
            if status['status'] != 'running':
                return status
            time.sleep(0.1)
        self.fail('Compression did not finish.')

    def test_compress_download_and_explore_copy(self):
        document = self.open_sample()
        body = json.dumps({'mode': 'target', 'preset': 'balanced', 'targetBytes': 10**8}).encode()
        job = json.loads(self.request(f'/api/document/{document}/compress', body))['job']
        status = self.wait_compression(job)
        self.assertEqual(status['status'], 'ready')
        self.assertTrue(status['report']['checks']['passed'])
        copy = self.request(f'/api/compress/{job}/download')
        self.assertTrue(copy.startswith(b'%PDF'))
        self.assertEqual(len(copy), status['report']['bytes'])
        self.assertTrue(self.request(f'/api/compress/{job}/preview/page/1').startswith(b'\x89PNG'))
        explored = json.loads(self.request(f'/api/compress/{job}/explore', b'{}'))['job']
        deadline = time.monotonic() + 15
        while json.loads(self.request('/api/job/' + explored))['status'] == 'analysing' and time.monotonic() < deadline:
            time.sleep(0.05)
        report = json.loads(self.request('/api/document/' + explored))
        self.assertEqual(report['name'], 'source-compressed.pdf')
        self.assertEqual(report['bytes'], len(copy))

    def test_preview_one_page_at_every_quality(self):
        heavy = Path(self.temp.name) / 'heavy.pdf'
        with fitz.open() as doc:
            page = doc.new_page()
            shape = page.new_shape()
            for row in range(120, 700, 4):
                for col in range(60, 520, 2):
                    shape.draw_line((col, row), (col + 1.6, row))
            shape.finish(color=(0.1, 0.3, 0.6), width=0.4)
            shape.commit()
            page.insert_text((72, 80), 'Figure 1')
            doc.new_page().insert_text((72, 80), 'Plain page')
            doc.save(heavy)
        job = json.loads(self.request('/api/open', heavy.read_bytes(), {'X-Filename': 'heavy.pdf'}))['job']
        deadline = time.monotonic() + 15
        while json.loads(self.request('/api/job/' + job))['status'] == 'analysing' and time.monotonic() < deadline:
            time.sleep(0.05)
        sample = json.loads(self.request(f'/api/document/{job}/sample', b'{"page":1}'))
        self.assertEqual([r['preset'] for r in sample['results']], ['high', 'balanced', 'compact'])
        self.assertTrue(all(r['status'] == 'converted' and r['afterBytes'] < sample['eligibleBytes'] for r in sample['results']))
        png = self.request(f'/api/document/{job}/sample/compact/1?width=600')
        self.assertEqual(fitz.Pixmap(png).width, 600)
        self.assertEqual(fitz.Pixmap(self.request(f'/api/document/{job}/preview/page/1?width=600')).width, 600)
        plain = json.loads(self.request(f'/api/document/{job}/sample', b'{"page":2}'))
        self.assertTrue(all(r['status'] == 'small' and r['reason'] for r in plain['results']))
        with self.assertRaises(HTTPError) as error:
            self.request(f'/api/document/{job}/sample/high/2')
        self.assertEqual(error.exception.code, 422)

    def test_organize_pages_into_a_new_pdf(self):
        document = self.open_sample()
        pages = [{'doc': document, 'page': 3}, {'doc': document, 'page': 1, 'rotate': 180}, {'blank': True, 'width': 300, 'height': 300}]
        result = json.loads(self.request(f'/api/document/{document}/organize', json.dumps({'pages': pages}).encode()))
        self.assertEqual((result['pages'], result['name']), (3, 'source-organized.pdf'))
        copy = self.request(f'/api/organized/{result["id"]}/download')
        self.assertEqual(len(copy), result['bytes'])
        with fitz.open(stream=copy, filetype='pdf') as doc:
            self.assertEqual(doc[0].get_text().strip(), 'Page 3')
            self.assertEqual(doc[1].rotation, 180)
        explored = json.loads(self.request(f'/api/organized/{result["id"]}/explore', b'{}'))['job']
        deadline = time.monotonic() + 15
        while json.loads(self.request('/api/job/' + explored))['status'] == 'analysing' and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(json.loads(self.request('/api/document/' + explored))['pageCount'], 3)
        with self.assertRaises(HTTPError) as error:
            self.request(f'/api/document/{document}/organize', b'{"pages":[]}')
        self.assertEqual(error.exception.code, 400)

    def wait_analysis(self, job):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = json.loads(self.request('/api/job/' + job))
            if status['status'] != 'analysing':
                return status
            time.sleep(0.05)
        return status

    def test_combine_files_and_split_one(self):
        first, second = self.open_sample(), self.open_sample()
        result = json.loads(self.request('/api/merge', json.dumps({'documents': [first, second]}).encode()))
        self.assertEqual((result['pages'], result['files'], result['name']), (60, 2, 'source-combined.pdf'))
        with fitz.open(stream=self.request(f'/api/organized/{result["id"]}/download'), filetype='pdf') as doc:
            self.assertEqual((len(doc), doc[30].get_text().strip()), (60, 'Page 1'))
            self.assertEqual(doc.get_toc(), [[1, 'source', 1], [1, 'source', 31]])
        parts = json.loads(self.request(f'/api/document/{first}/split', b'{"mode":"every","value":12}'))
        self.assertEqual([(p['firstPage'], p['lastPage']) for p in parts['parts']], [(1, 12), (13, 24), (25, 30)])
        self.assertEqual(parts['parts'][0]['name'], 'source - pages 1-12.pdf')
        with zipfile.ZipFile(io.BytesIO(self.request(f'/api/split/{parts["id"]}/download'))) as bundle:
            self.assertEqual(bundle.namelist(), [p['name'] for p in parts['parts']])
        with fitz.open(stream=self.request(f'/api/split/{parts["id"]}/part/3'), filetype='pdf') as doc:
            self.assertEqual((len(doc), doc[0].get_text().strip()), (6, 'Page 25'))
        for path, body in ((f'/api/document/{first}/split', b'{"mode":"every","value":30}'),
                           (f'/api/document/{first}/split', b'{"mode":"bookmarks","value":1}'),
                           ('/api/merge', json.dumps({'documents': [first]}).encode()),
                           ('/api/merge', b'{"documents":["missing","gone"]}')):
            with self.assertRaises(HTTPError) as error:
                self.request(path, body)
            self.assertEqual(error.exception.code, 400)

    def test_presentations_open_as_pdfs(self):
        def fake_convert(source, destination, started=None):
            with fitz.open() as doc:
                doc.new_page().insert_text((50, 50), 'Slide 1')
                doc.save(destination)
            return 'PowerPoint'
        real, server.convert, self.engine.converter = server.convert, fake_convert, 'PowerPoint'
        try:
            job = json.loads(self.request('/api/open', b'PK\x03\x04 slides', {'X-Filename': 'deck.pptx'}))['job']
            self.assertEqual(self.wait_analysis(job)['status'], 'ready')
            report = json.loads(self.request('/api/document/' + job))
            self.assertEqual((report['name'], report['convertedFrom'], report['pageCount']), ('deck.pdf', 'deck.pptx', 1))
            self.assertTrue(self.request(f'/api/document/{job}/download').startswith(b'%PDF'))
            self.assertFalse(list(Path(self.temp.name).glob('*.pptx')))
            with self.assertRaises(HTTPError) as error:
                self.request('/api/open', b'%PDF-1.7 not slides', {'X-Filename': 'deck.pptx'})
            self.assertEqual(error.exception.code, 400)
            self.engine.converter = None
            with self.assertRaises(HTTPError) as error:
                self.request('/api/open', b'PK\x03\x04 slides', {'X-Filename': 'deck.pptx'})
            self.assertIn('PowerPoint', json.loads(error.exception.read())['error'])
        finally:
            server.convert = real
            self.engine.converter = server.converter_available()

    def test_transcripts_and_recordings_open_as_pdfs(self):
        words = [{'text': w, 'start': i * 400, 'end': i * 400 + 300, 'speaker': 'A'} for i, w in enumerate('Good morning, everyone.'.split())]
        before = set(Path(self.temp.name).glob('*.json'))
        job = json.loads(self.request('/api/open', json.dumps(words).encode(), {'X-Filename': 'lecture.json'}))['job']
        self.assertEqual(self.wait_analysis(job)['status'], 'ready')
        report = json.loads(self.request('/api/document/' + job))
        self.assertEqual((report['name'], report['convertedFrom'], report['pageCount']), ('lecture-transcript.pdf', 'lecture.json', 1))
        self.assertIn(b'Good morning', fitz.open('pdf', self.request(f'/api/document/{job}/download'))[0].get_text().encode())
        # Only the analysis report is left; the uploaded transcript is removed.
        self.assertEqual(set(Path(self.temp.name).glob('*.json')) - before, {Path(self.temp.name) / (job + '.json')})
        with self.assertRaises(HTTPError) as error:
            self.request('/api/open', b'%PDF-1.7', {'X-Filename': 'lecture.json'})
        self.assertEqual(error.exception.code, 400)

        started, stop_seen = threading.Event(), threading.Event()

        def fake_transcript(source, destination, title, progress, stop):
            progress('Uploading the recording', 5, 10)
            started.set()
            if stop.wait(10):
                stop_seen.set()
                raise ValueError('stopped')
        real, server.transcript_pdf = server.transcript_pdf, fake_transcript
        try:
            self.engine.transcriber = None
            with self.assertRaises(HTTPError) as error:
                self.request('/api/open', b'ID3 audio', {'X-Filename': 'lecture.mp3'})
            self.assertIn('not set up', json.loads(error.exception.read())['error'])
            self.engine.transcriber = 'AssemblyAI'
            job = json.loads(self.request('/api/open', b'ID3 audio', {'X-Filename': 'lecture.mp3'}))['job']
            self.assertTrue(started.wait(10))
            status = json.loads(self.request('/api/job/' + job))
            self.assertEqual((status['phase'], status['completed'], status['total']), ('Uploading the recording', 5, 10))
            self.assertEqual(json.loads(self.request(f'/api/job/{job}/cancel', b'{}'))['status'], 'cancelled')
            self.assertTrue(stop_seen.wait(10))
            deadline = time.monotonic() + 5
            while list(Path(self.temp.name).glob('*.mp3')) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(list(Path(self.temp.name).glob('*.mp3')))
        finally:
            server.transcript_pdf = real
            self.engine.transcriber = server.transcriber_available()

    def test_several_copies_save_as_one_zip(self):
        document = self.open_sample()
        jobs = []
        for _ in range(2):
            job = json.loads(self.request(f'/api/document/{document}/compress', b'{"mode":"quality","preset":"lossless"}'))['job']
            self.assertEqual(self.wait_compression(job)['status'], 'ready')
            jobs.append(job)
        with zipfile.ZipFile(io.BytesIO(self.request('/api/bundle?jobs=' + ','.join(jobs)))) as bundle:
            self.assertEqual(bundle.namelist(), ['source-compressed.pdf', 'source-compressed (2).pdf'])
            self.assertTrue(bundle.read('source-compressed.pdf').startswith(b'%PDF'))
        with self.assertRaises(HTTPError) as error:
            self.request('/api/bundle?jobs=missing')
        self.assertEqual(error.exception.code, 422)

    def test_compression_requests_are_validated_and_cancellable(self):
        document = self.open_sample()
        with self.assertRaises(HTTPError) as error:
            self.request(f'/api/document/{document}/compress', b'{"mode":"target","preset":"high"}')
        self.assertEqual(error.exception.code, 400)
        job = json.loads(self.request(f'/api/document/{document}/compress', b'{"mode":"quality","preset":"high"}'))['job']
        status = json.loads(self.request(f'/api/compress/{job}/cancel', b'{}'))
        self.assertIn(status['status'], ('cancelled', 'ready'))
        self.assertFalse(self.engine.compressions[job]['process'].is_alive())

    def test_cancel_stops_worker(self):
        result = json.loads(self.request('/api/sample', b'{"id":0}'))
        status = json.loads(self.request('/api/job/' + result['job'] + '/cancel', b'{}'))
        self.assertEqual(status['status'], 'cancelled')
        self.assertFalse(self.engine.jobs[result['job']]['process'].is_alive())

    def test_invalid_upload_is_removed(self):
        before = set(Path(self.temp.name).glob('*.pdf'))
        with self.assertRaises(HTTPError) as error:
            self.request('/api/open', b'not a PDF')
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(before, set(Path(self.temp.name).glob('*.pdf')))

    def test_local_access_guards(self):
        with self.assertRaises(HTTPError) as error:
            self.request('/api/sample', b'{}', {'X-Explorer-Token': 'wrong'})
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(HTTPError) as error:
            self.request('/api/config', headers={'Host': 'example.test'})
        self.assertEqual(error.exception.code, 403)


if __name__ == '__main__':
    unittest.main()
