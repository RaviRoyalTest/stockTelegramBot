import http.client
conn = http.client.HTTPConnection('127.0.0.1', 14410, timeout=10)
conn.request('GET', '/')
res = conn.getresponse()
print(res.status, res.reason)
print(res.getheader('content-type'))
body = res.read().decode('utf-8',errors='replace')
print(body[:1000])
