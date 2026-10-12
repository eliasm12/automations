"""Report the installed image and import state before starting the poller."""

import importlib.util
from pathlib import Path
import runpy
import sys


app = Path(__file__).resolve().parent
print(f"SPC Bluetooth Logger 0.2.6: app={app}", flush=True)
try:
    print(f"SPC app files: {', '.join(sorted(p.name for p in app.iterdir()))}", flush=True)
except PermissionError as error:
    print(f"SPC app listing denied: {error}", flush=True)
print(f"SPC Python path: {sys.path}", flush=True)
decoder = app / "decoder.py"
print(f"SPC decoder.py: exists={decoder.exists()} readable={decoder.is_file()}", flush=True)
if importlib.util.find_spec("decoder") is None:
    raise RuntimeError("decoder import unavailable; inspect the app file list above")

runpy.run_path(str(app / "poller.py"), run_name="__main__")
