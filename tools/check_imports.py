"""Import check for the data-source modules. Run with: python tools/check_imports.py"""
import importlib
import pathlib
import sys

# Running the file directly puts tools/ on sys.path, not the repo root - so
# `import corporate_actions...` fails unless PYTHONPATH is set. Bootstrap it.
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

mods = [
    'corporate_actions.sources.nse',
    'corporate_actions.sources.bse',
    'corporate_actions.sources.screener',
    'corporate_actions.sources.fundamentals',
    'corporate_actions.sources.quotes',
    'corporate_actions.sources.async_api',
]
ok = True
for m in mods:
    try:
        importlib.import_module(m)
        print(m, 'OK')
    except Exception as e:
        print(m, 'ERR', repr(e))
        ok = False
sys.exit(0 if ok else 2)
