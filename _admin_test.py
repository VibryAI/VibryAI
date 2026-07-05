"""Admin panel & API quick test"""
import urllib.request, json

B = 'http://localhost:9999'

# Admin page
r = urllib.request.urlopen(B+'/admin', timeout=5)
print(f'1. Admin page: {r.status} {len(r.read())}bytes')

# Login
d = json.dumps({'password':'vibry2024'}).encode()
req = urllib.request.Request(B+'/admin/api/login', data=d, method='POST')
req.add_header('Content-Type','application/json')
resp = json.loads(urllib.request.urlopen(req,timeout=5).read())
print(f'2. Login: {resp["ok"]}')
tok = resp['token']

h = lambda t: {'Authorization':f'Bearer {t}'} if t else {}

for label, path in [
    ('3. Stats', '/admin/api/stats'),
    ('4. Billing', '/admin/api/billing'),
    ('5. Config', '/admin/api/config'),
    ('6. Logs', '/admin/api/logs?lines=10'),
]:
    req = urllib.request.Request(B+path, headers={'Authorization':f'Bearer {tok}'})
    resp = json.loads(urllib.request.urlopen(req,timeout=5).read())
    if 'lines' in resp:
        print(f'{label}: {len(resp["lines"])} lines')
    elif 'summary' in resp:
        print(f'{label}: {resp["summary"]["total_calls"]}calls RMB{resp["summary"]["total_cost_rmb"]}')
    elif 'upstream_model' in resp:
        print(f'{label}: model={resp["upstream_model"]} asr={resp["asr_mode"]}')
    else:
        print(f'{label}: {json.dumps(resp,ensure_ascii=False)[:80]}')

# Config save
d = json.dumps({'asr_mode':'local','memory_top_k':5}).encode()
req = urllib.request.Request(B+'/admin/api/config', data=d, method='POST', headers={'Content-Type':'application/json','Authorization':f'Bearer {tok}'})
resp = json.loads(urllib.request.urlopen(req,timeout=5).read())
print(f'7. Save config: {resp["ok"]}')

print('\nALL ADMIN API PASSED!')
print('Open http://localhost:9999/admin in browser')
print('Password: vibry2024')
