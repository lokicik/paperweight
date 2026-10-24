"""Bundle the engine and web assets; users do not need Python installed.

An AssemblyAI key, from ASSEMBLYAI_API_KEY or an assemblyai.key file at the
repository root, is built in so the app can transcribe recordings. It is only
masked: anyone who takes the executable apart can recover it. Neither the key
nor the generated module is ever committed.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
target = subprocess.check_output(['rustc', '--print', 'host-tuple'], text=True).strip()
build = root / 'build' / 'sidecar'
command = [
    sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile',
    '--name', 'paperweight-engine', '--paths', str(root / 'backend'),
    '--add-data', f'{root / "web"}{os.pathsep}web',
    '--distpath', str(build / 'dist'), '--workpath', str(build / 'work'),
    '--specpath', str(build), str(root / 'scripts' / 'engine_entry.py'),
]
key_file, module = root / 'assemblyai.key', root / 'backend' / 'paperweight' / '_key.py'
key = os.environ.get('ASSEMBLYAI_API_KEY', '').strip() or (key_file.read_text(encoding='utf-8-sig').strip() if key_file.is_file() else '')
if key:
    secret = key.encode()
    mask = os.urandom(len(secret))
    module.write_text(f'KEY = {bytes(s ^ m for s, m in zip(secret, mask))!r}\nMASK = {mask!r}\n', encoding='utf-8')
    command[-1:-1] = ['--hidden-import', 'paperweight._key']
environment = os.environ.copy()
environment.pop('PYTHONPATH', None)
environment.pop('PYTHONHOME', None)
environment.pop('ASSEMBLYAI_API_KEY', None)
try:
    subprocess.run(command, cwd=root, check=True, env=environment)
finally:
    module.unlink(missing_ok=True)
extension = '.exe' if sys.platform == 'win32' else ''
destination = root / 'src-tauri' / 'binaries' / f'paperweight-engine-{target}{extension}'
destination.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(build / 'dist' / f'paperweight-engine{extension}', destination)
print(f'Sidecar ready: {destination.name} ({destination.stat().st_size / 1e6:.1f} MB)')
print('Recordings: transcribed with the built-in AssemblyAI key.' if key else
      'Recordings: no AssemblyAI key built in (set ASSEMBLYAI_API_KEY or add assemblyai.key), so recordings will not open.')
