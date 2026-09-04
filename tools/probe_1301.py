import requests
from concurrent.futures import ThreadPoolExecutor

def call(url):
    try:
        r = requests.get(url, timeout=10)
        return url, r.status_code, len(r.content)
    except Exception as e:
        return url, 'ERROR', str(e)

urls = [
    'http://127.0.0.1:1301/api/screener?universe=nifty500&limit=50',
    'http://127.0.0.1:1301/api/watchlist',
    'http://127.0.0.1:1301/api/fundamentals?symbol=RELIANCE',
    'http://127.0.0.1:1301/api/corporate_actions',
]

with ThreadPoolExecutor(max_workers=8) as ex:
    futs = [ex.submit(call, u) for u in urls*3]
    for f in futs:
        print(f.result())
