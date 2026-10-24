"""Transcript PDFs from AssemblyAI words, and the AssemblyAI round trip against a stand-in server."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest import mock

from pypdf import PdfReader

from paperweight.analyzer import fitz
from paperweight import transcriber
from paperweight.transcriber import Cancelled, paragraphs, speaker_names, transcribe, transcript_pdf, words_of


def spoken(text, speaker='A', start=0, pace=400):
    """Timed words, one every `pace` ms."""
    return [{'text': t, 'start': start + i * pace, 'end': start + i * pace + 300, 'confidence': 0.9, 'speaker': speaker}
            for i, t in enumerate(text.split())]


class FakeAssemblyAI(BaseHTTPRequestHandler):
    """Accepts one upload, finishes the transcript after `polls` checks, and records every call."""
    def log_message(self, *args):
        pass

    def reply(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_call(self):
        api = self.server.api
        length = int(self.headers.get('Content-Length', '0'))
        body = self.rfile.read(length) if length else b''
        api['calls'].append((self.command, self.path, self.headers.get('Authorization')))
        if self.headers.get('Authorization') != 'test-key':
            return self.reply(401, {'error': 'Invalid API key'})
        if self.path == '/v2/upload':
            api['uploaded'] = body
            return self.reply(200, {'upload_url': 'https://cdn.example/upload/1'})
        if self.path == '/v2/transcript' and self.command == 'POST':
            api['request'] = json.loads(body)
            return self.reply(200, {'id': 't1', 'status': 'queued'})
        if self.path == '/v2/transcript/t1' and self.command == 'GET':
            api['polls'] -= 1
            if api['polls'] > 0:
                return self.reply(200, {'id': 't1', 'status': 'processing'})
            return self.reply(200, api['result'])
        if self.path == '/v2/transcript/t1' and self.command == 'DELETE':
            return self.reply(200, {'id': 't1', 'status': 'completed'})
        self.reply(404, {'error': 'Not found'})

    do_GET = do_POST = do_DELETE = handle_call


class TranscriptLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_paragraphs_break_at_speakers_and_pauses_but_never_mid_sentence(self):
        words = spoken('Today we look at the thalamus. It relays signals')
        words += spoken('to the cortex. Is that every signal?', 'B', start=words[-1]['end'] + 1000)
        words += spoken('Almost every one. Smell is the exception.', 'A', start=words[-1]['end'] + 400)
        words += spoken('After the break we continue.', 'A', start=words[-1]['end'] + 4000)
        result = paragraphs(words)
        self.assertEqual([p['text'] for p in result], [
            'Today we look at the thalamus. It relays signals to the cortex.', 'Is that every signal?',
            'Almost every one. Smell is the exception.', 'After the break we continue.'])
        self.assertEqual([p['speaker'] for p in result], ['A', 'B', 'A', 'A'])
        self.assertEqual(result[3]['start'], words[-5]['start'])
        monologue = paragraphs(spoken(' '.join(['and'] * 600)))
        self.assertTrue(all(len(p['text'].split()) <= 250 for p in monologue))
        self.assertEqual(sum(len(p['text'].split()) for p in monologue), 600)

    def test_speakers_are_named_for_a_lecture(self):
        lecture = spoken('word ' * 90, 'A') + spoken('question ' * 10, 'B')
        self.assertEqual(speaker_names(lecture), {'A': 'Lecturer', 'B': 'Student'})
        self.assertEqual(speaker_names(spoken('one voice only')), {})
        self.assertEqual(speaker_names(spoken('a ' * 50, 'A') + spoken('b ' * 50, 'B')), {'A': 'Speaker A', 'B': 'Speaker B'})

    def test_pdf_keeps_every_word_with_times_speakers_and_bookmarks(self):
        sentence = 'Şimdi ığdır ve güneşli İstanbul, the first field of office flow. '
        words, start = [], 0
        for minute in range(16):
            words += spoken(sentence * 4, 'A' if minute % 5 else 'B', start)
            start = minute * 60_000 + 60_000
        source = self.folder / 'Nöroanatomi 3.json'
        source.write_text(json.dumps({'language_code': 'tr', 'words': words}), encoding='utf-8')
        output = self.folder / 'out.pdf'
        pages = transcript_pdf(source, output, 'Nöroanatomi 3')
        with fitz.open(output) as doc:
            self.assertEqual(len(doc), pages)
            text = ''.join(page.get_text() for page in doc)
            self.assertIn('Nöroanatomi 3', text)
            self.assertIn('Language: Turkish', text)
            self.assertIn(f'1 / {pages}', text)
            self.assertIn('0:15:00', text)
            self.assertEqual(text.count('LECTURER'), sum(1 for p in paragraphs(words) if p['speaker'] != 'B'))
            column = ''.join(page.get_text(clip=fitz.Rect(transcriber.TEXT - 2, 0, transcriber.RIGHT + 2, transcriber.BOTTOM))
                             for page in doc)
            self.assertIn(re.sub(r'\s', '', ''.join(w['text'] for w in words)), re.sub(r'\s', '', column))
            self.assertEqual([entry[1] for entry in doc.get_toc()], ['0:00:00', '0:05:00', '0:10:00', '0:15:00'])
        # Ligatures are drawn, but copy and search give the plain letters.
        extracted = ''.join(page.extract_text() for page in PdfReader(output).pages)
        self.assertIn('first field of office flow', extracted)
        self.assertFalse(re.search('[ﬀ-ﬄ]', extracted))

    def test_json_must_be_an_assemblyai_transcript(self):
        for content in ('{"text": "no words"}', '[{"word": "x"}]', 'not json', '[]'):
            source = self.folder / 'bad.json'
            source.write_text(content, encoding='utf-8')
            with self.assertRaises(ValueError):
                transcript_pdf(source, self.folder / 'bad.pdf', 'Bad')
        self.assertEqual(len(words_of([{'text': ' ', 'start': 0}, {'text': 'Hi', 'start': 5}])), 1)


class AssemblyAITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.recording = Path(self.temp.name) / 'lecture.mp3'
        self.recording.write_bytes(os.urandom(3 * 1024 * 1024 + 17))
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeAssemblyAI)
        words = spoken('Hello and welcome. Today is about memory.')
        self.server.api = {'calls': [], 'polls': 2, 'result': {'id': 't1', 'status': 'completed', 'language_code': 'en', 'words': words}}
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.patches = [mock.patch.object(transcriber, 'API', f'http://127.0.0.1:{self.server.server_port}/v2'),
                        mock.patch.dict(os.environ, {'ASSEMBLYAI_API_KEY': 'test-key'})]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def test_recording_is_uploaded_transcribed_and_deleted_there(self):
        phases = []
        result = transcribe(self.recording, lambda phase, done, total: phases.append((phase, done, total)), poll=0.01)
        api = self.server.api
        self.assertEqual(api['uploaded'], self.recording.read_bytes())
        self.assertEqual(api['request'], {'audio_url': 'https://cdn.example/upload/1', 'speaker_labels': True, 'language_detection': True})
        self.assertEqual([c[:2] for c in api['calls']], [('POST', '/v2/upload'), ('POST', '/v2/transcript'), ('GET', '/v2/transcript/t1'),
                                                         ('GET', '/v2/transcript/t1'), ('DELETE', '/v2/transcript/t1')])
        self.assertEqual(result['words'][0]['text'], 'Hello')
        uploads = [p for p in phases if p[0].startswith('Uploading')]
        self.assertEqual(uploads[-1][1:], (self.recording.stat().st_size,) * 2)
        self.assertTrue(any(p[0].startswith('Transcribing') for p in phases))

    def test_failures_are_reported_and_the_transcript_is_still_deleted(self):
        self.server.api['result'] = {'id': 't1', 'status': 'error', 'error': 'File does not appear to contain audio.'}
        with self.assertRaisesRegex(ValueError, 'does not appear to contain audio'):
            transcribe(self.recording, poll=0.01)
        self.assertEqual(self.server.api['calls'][-1][:2], ('DELETE', '/v2/transcript/t1'))
        with mock.patch.dict(os.environ, {'ASSEMBLYAI_API_KEY': 'wrong'}):
            with self.assertRaisesRegex(ValueError, 'did not accept'):
                transcribe(self.recording, poll=0.01)
        with mock.patch.dict(os.environ, {'ASSEMBLYAI_API_KEY': ''}), mock.patch.object(transcriber, 'api_key', return_value=None):
            with self.assertRaisesRegex(ValueError, 'not set up'):
                transcribe(self.recording)

    def test_stopping_cancels_and_deletes_the_transcript(self):
        self.server.api['polls'] = 10_000
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        with self.assertRaises(Cancelled):
            transcribe(self.recording, stop=stop, poll=0.05)
        self.assertEqual(self.server.api['calls'][-1][:2], ('DELETE', '/v2/transcript/t1'))

    def test_recording_becomes_a_transcript_pdf(self):
        output = Path(self.temp.name) / 'lecture.pdf'
        with mock.patch.object(transcriber, 'transcribe', lambda source, progress, stop: transcribe(source, progress, stop, poll=0.01)):
            self.assertEqual(transcript_pdf(self.recording, output, 'Memory'), 1)
        with fitz.open(output) as doc:
            self.assertIn('Hello and welcome. Today is about memory.', doc[0].get_text().replace('\n', ' '))
            self.assertIn('Language: English', doc[0].get_text())


if __name__ == '__main__':
    unittest.main()
