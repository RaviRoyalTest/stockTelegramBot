import http.client
ports=[11587,10954,6269,2483,14410,2483,8000,8001]
for p in ports:
    try:
        conn=http.client.HTTPConnection('127.0.0.1',p,timeout=3)
        conn.request('GET','/api/health')
        r=conn.getresponse()
        print(p,'->',r.status)
    except Exception as e:
        print(p,'->',type(e).__name__)
