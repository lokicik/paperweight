"""Verify the frozen executable, its worker processes and managed shutdown."""
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import Request, urlopen

import pymupdf as fitz

root = Path(__file__).resolve().parents[1]
binary = Path(sys.argv[1]) if len(sys.argv) > 1 else next((root / 'src-tauri/binaries').glob('paperweight-engine-*.exe'))
environment = os.environ.copy()
environment.pop('PYTHONPATH', None)
environment.pop('PYTHONHOME', None)
process = subprocess.Popen(
    [str(binary), '--port', '0', '--managed'], stdin=subprocess.PIPE,
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment,
    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
)
try:
    lines = queue.Queue()
    threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
    line = lines.get(timeout=40)
    if not line:
        raise RuntimeError(process.stderr.read())
    ready = json.loads(line)
    base = ready['url'].rstrip('/')
    config = json.load(urlopen(base + '/api/config', timeout=10))

    def request(path, payload=None, filename=None):
        headers = {'X-Explorer-Token': config['token']}
        if filename:
            headers['X-Filename'] = filename
        with urlopen(Request(base + path, data=payload, headers=headers), timeout=20) as response:
            return response.read()

    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / 'sample.pdf'
        pix = fitz.Pixmap(fitz.csRGB, (0, 0, 32, 32), False)
        pix.clear_with(180)
        with fitz.open() as doc:
            page = doc.new_page()
            page.insert_text((30, 30), 'Frozen engine test')
            image = page.insert_image(fitz.Rect(30, 60, 130, 160), pixmap=pix)
            doc.new_page().insert_image(fitz.Rect(30, 60, 130, 160), xref=image)
            doc.save(source, deflate=True)
        original = source.read_bytes()
        job = json.loads(request('/api/open', original, 'sample.pdf'))['job']
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = json.loads(request('/api/job/' + job))
            if status['status'] != 'analysing':
                break
            time.sleep(0.1)
        assert status['status'] == 'ready', status
        report = json.loads(request('/api/document/' + job))
        assert report['pageCount'] == 2
        assert sum(c['bytes'] for c in report['categories']) == len(original)
        stored_image = next(o for o in report['objects'] if o['id'] == image)
        assert stored_image['pages'] == [1, 2]
        assert request(f'/api/document/{job}/preview/image/{image}').startswith(b'\x89PNG')
        assert request(f'/api/document/{job}/preview/page/2').startswith(b'\x89PNG')
        assert hashlib.sha256(original).digest() == hashlib.sha256(source.read_bytes()).digest()

        job2 = json.loads(request('/api/open', original, 'cancel.pdf'))['job']
        cancelled = json.loads(request('/api/job/' + job2 + '/cancel', b'{}'))
        assert cancelled['status'] == 'cancelled', cancelled

        # Compression uses worker processes inside the job process.
        heavy = Path(directory) / 'heavy.pdf'
        with fitz.open() as doc:
            for number in range(2):
                page = doc.new_page()
                shape = page.new_shape()
                for row in range(120, 700, 4):
                    for col in range(60, 520, 2):
                        shape.draw_line((col, row), (col + 1.6, row))
                shape.finish(color=(0.1, 0.3, 0.6), width=0.4)
                shape.commit()
                page.insert_text((72, 80), f'Figure {number + 1}')
            doc.save(heavy)
        job3 = json.loads(request('/api/open', heavy.read_bytes(), 'heavy.pdf'))['job']
        while json.loads(request('/api/job/' + job3))['status'] == 'analysing':
            time.sleep(0.1)
        compression = json.loads(request(f'/api/document/{job3}/compress', b'{"mode":"quality","preset":"high"}'))['job']
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            status = json.loads(request('/api/compress/' + compression))
            if status['status'] != 'running':
                break
            time.sleep(0.2)
        assert status['status'] == 'ready', status
        assert status['report']['checks']['passed'] and len(status['report']['converted']) == 2, status['report']
        copy = request(f'/api/compress/{compression}/download')
        assert len(copy) == status['report']['bytes'] < heavy.stat().st_size
        with fitz.open(stream=copy, filetype='pdf') as doc:
            assert doc[1].get_text().strip() == 'Figure 2'
    process.stdin.write('shutdown\n')
    process.stdin.flush()
    process.wait(timeout=20)
    assert process.returncode == 0, process.stderr.read()
    print(json.dumps({'frozenEngine': 'passed', 'sharedAccounting': 'passed', 'previews': 'passed', 'cancellation': 'passed', 'compression': 'passed', 'managedShutdown': 'passed', 'executableBytes': binary.stat().st_size}))
finally:
    if process.poll() is None:
        process.stdin.close()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
