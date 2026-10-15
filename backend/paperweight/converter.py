"""Turn presentations into PDFs with an office suite already on this computer.

Paperweight bundles no slide renderer. It drives Microsoft PowerPoint through
its automation interface on Windows, or LibreOffice when that is installed.
The source file is only read.
"""
import base64
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

PRESENTATIONS = {'.pptx', '.ppt', '.pptm', '.ppsx', '.pps', '.ppsm', '.potx', '.pot', '.odp'}
HIDDEN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

# Opens the deck read-only without a window and saves it as PDF (format 32).
# PowerPoint is closed afterwards only if it was not already open.
POWERPOINT_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$running = [bool](Get-Process POWERPNT -ErrorAction SilentlyContinue)
$app = New-Object -ComObject PowerPoint.Application
try {
    $deck = $app.Presentations.Open($env:PW_SOURCE, -1, 0, 0)
    try { $deck.SaveAs($env:PW_TARGET, 32) } finally { $deck.Close() }
} finally {
    if (-not $running) { $app.Quit() }
    [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($app)
}
'''


def is_presentation(name):
    return Path(name).suffix.lower() in PRESENTATIONS


def powerpoint_installed():
    if os.name != 'nt':
        return False
    import winreg
    try:
        winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r'PowerPoint.Application\CLSID').Close()
        return True
    except OSError:
        return False


def libreoffice():
    """Path of LibreOffice's command-line program, or None."""
    found = shutil.which('soffice') or shutil.which('libreoffice')
    if found:
        return found
    for root in (os.environ.get('ProgramFiles'), os.environ.get('ProgramFiles(x86)'), '/Applications/LibreOffice.app/Contents/MacOS'):
        if root:
            for name in ('LibreOffice/program/soffice.exe', 'soffice'):
                path = Path(root) / name
                if path.is_file():
                    return str(path)
    return None


def available():
    """'PowerPoint', 'LibreOffice' or None: what will convert presentations."""
    if powerpoint_installed():
        return 'PowerPoint'
    if libreoffice():
        return 'LibreOffice'
    return None


def convert(source, destination, started=lambda process: None, timeout=600):
    """Write a PDF of the presentation `source` to `destination`.

    `started` receives the converter process so a caller can stop it.
    Raises ValueError with a message for people when conversion fails."""
    tool = available()
    if tool is None:
        raise ValueError('Opening presentations needs Microsoft PowerPoint or LibreOffice on this computer.')
    source, destination = Path(source).resolve(), Path(destination).resolve()
    with tempfile.TemporaryDirectory(prefix='paperweight-convert-') as scratch:
        if tool == 'PowerPoint':
            script = base64.b64encode(POWERPOINT_SCRIPT.encode('utf-16-le')).decode()
            command = ['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', script]
            env = dict(os.environ, PW_SOURCE=str(source), PW_TARGET=str(destination))
            output = destination
        else:
            # A profile of its own, so a LibreOffice window the person has open is not disturbed.
            profile = Path(scratch, 'profile').as_uri()
            command = [libreoffice(), f'-env:UserInstallation={profile}', '--headless', '--convert-to', 'pdf', '--outdir', scratch, str(source)]
            env, output = None, Path(scratch, source.stem + '.pdf')
        process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=HIDDEN)
        started(process)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise ValueError(f'{tool} took too long to convert this presentation.')
        if process.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            raise ValueError(f'{tool} could not convert this presentation. If it is password protected, open an unlocked copy.')
        if output != destination:
            shutil.move(output, destination)
    return tool
