import importlib
import sys
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
