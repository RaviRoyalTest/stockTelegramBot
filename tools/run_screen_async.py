import asyncio,traceback
from corporate_actions import screener_service

async def run():
    try:
        rows = await screener_service.screen_universe_async('nifty500', {}, 'market_cap', False, limit=50)
        print('OK rows', len(rows))
    except Exception as e:
        print('ERROR', repr(e))
        traceback.print_exc()

asyncio.run(run())
