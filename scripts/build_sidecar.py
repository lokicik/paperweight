"""Bundle the engine and web assets; users do not need Python installed."""
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
environment = os.environ.copy()
environment.pop('PYTHONPATH', None)
environment.pop('PYTHONHOME', None)
subprocess.run(command, cwd=root, check=True, env=environment)
extension = '.exe' if sys.platform == 'win32' else ''
destination = root / 'src-tauri' / 'binaries' / f'paperweight-engine-{target}{extension}'
destination.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(build / 'dist' / f'paperweight-engine{extension}', destination)
print(f'Sidecar ready: {destination.name} ({destination.stat().st_size / 1e6:.1f} MB)')
