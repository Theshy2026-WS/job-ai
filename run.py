"""Manual job digest. Never imports the application/browser automation code."""
from pathlib import Path
import importlib.util
import subprocess
import sys

if __name__ == '__main__':
    root = Path(__file__).resolve().parent
    if importlib.util.find_spec('pypdf') is None:
        local = root / 'job_hunter' / '.venv' / 'Scripts' / 'python.exe'
        if local.exists() and Path(sys.executable).resolve() != local.resolve():
            raise SystemExit(subprocess.call([str(local), '-X', 'utf8', str(root / 'run.py'), *sys.argv[1:]], cwd=root))
        raise SystemExit('Install dependencies first: python -m pip install -r requirements.txt')
    from job_hunter.digest import main
    raise SystemExit(main(root=root))
