"""Admin-only aggregate insights. No user text or identifiers leave this module."""
import os
import json
import time
import threading
import requests
from fastapi import Depends, Query, HTTPException
from psycopg2.extras import RealDictCursor

_cache = {}
_lock = threading.Lock()


def local_report(snapshot):
    u, t = snapshot['users'], snapshot['tasks']
    facts = [f"Davrda {u['planners']} foydalanuvchining vazifasi mavjud; {u['markers']} tasida kamida bitta yakuniy status bor.",
             f"{t['total']} vazifadan {t['completed']} tasi bajarilgan, {t['failed']} tasi bajarilmagan, {t['pending']} tasi kutilmoqda."]
    return {'facts': facts, 'hypotheses': ['Belgilanmagan vazifalar foydalanishdagi qiyinchilik yoki natijani qayd etmaslik bilan bog‘liq bo‘lishi mumkin. Ketish sababi tasdiqlanmagan.'] if t['pending'] else [],
            'experiments': ['Vazifa qo‘shgan guruhlarda keyingi kun qaytishini alohida o‘lchang.', 'Birinchi vazifani belgilash yo‘lini kichik guruhda soddalashtirib, qaytish bilan solishtiring.'] if u['planners'] else ['Birinchi vazifani qo‘shish yo‘lini kuzatishni boshlang.']}


def validate_report(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid report')
    result = {}
    for key in ('facts', 'hypotheses', 'experiments'):
        items = value.get(key)
        if not isinstance(items, list) or len(items) > 6 or any(not isinstance(x, str) or len(x) > 1200 for x in items):
            raise ValueError('Invalid report section')
        result[key] = items
    return result


def install(router, verify_admin, get_connection, context):
    def snapshot(days):
        with get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                p, cte, meta = context(cur)
                from datetime import timedelta
                p['start'] = p['today'] - timedelta(days=days - 1)
                # One statement: counts share a database snapshot; no writes/migrations.
                cur.execute(cte + """SELECT
                    (SELECT COUNT(*) FROM scoped_users) AS total_users,
                    (SELECT COUNT(*) FROM scoped_users WHERE state='blocked') AS blocked,
                    (SELECT COUNT(DISTINCT user_id) FROM recorded_activity WHERE activity_date BETWEEN %(start)s AND %(today)s) AS active,
                    (SELECT COUNT(DISTINCT user_id) FROM scoped_tasks WHERE task_date BETWEEN %(start)s AND %(today)s) AS planners,
                    (SELECT COUNT(DISTINCT user_id) FROM scoped_tasks WHERE task_date BETWEEN %(start)s AND %(today)s AND status IN ('completed','failed')) AS markers,
                    (SELECT COUNT(*) FROM scoped_tasks WHERE task_date BETWEEN %(start)s AND %(today)s) AS total_tasks,
                    (SELECT COUNT(*) FROM scoped_tasks WHERE task_date BETWEEN %(start)s AND %(today)s AND status='completed') AS completed,
                    (SELECT COUNT(*) FROM scoped_tasks WHERE task_date BETWEEN %(start)s AND %(today)s AND status='failed') AS failed,
                    (SELECT COUNT(*) FROM scoped_tasks WHERE task_date BETWEEN %(start)s AND %(today)s AND status='pending') AS pending,
                    (SELECT COUNT(*) FROM scoped_tasks WHERE task_date BETWEEN %(start)s AND %(today)s AND status='failed' AND fail_reason IS NULL) AS missing_reason
                """, p)
                r = dict(cur.fetchone())
        return {'start_date': p['start'].isoformat(), 'end_date': p['today'].isoformat(),
                'users': {k: int(r[k]) for k in ('total_users','blocked','active','planners','markers')},
                'tasks': {k: int(r['total_tasks' if k=='total' else k]) for k in ('total','completed','failed','pending','missing_reason')},
                'limitations': ['Bu funnel emas; bir xil davrdagi mavjud yozuvlar hisoboti.', 'Mini Appni shunchaki ochish faollik hisoblanmaydi.', 'O‘chirilgan vazifalar va tugmalar ketma-ketligi mavjud emas.', 'Davrning bugungi kuni hali tugamagan.', 'Ro‘yxatdan o‘tish sanasi botga ko‘chish sanasi emas.'],
                'generated_at': meta['generated_at']}

    @router.get('/insights')
    def insights(days: int = Query(7, ge=1, le=30), _: int = Depends(verify_admin)):
        data = snapshot(days)
        enabled = os.getenv('ADMIN_INSIGHTS_AI_ENABLED','false').lower() == 'true'
        return {'ok': True, 'snapshot': data, 'report': local_report(data), 'source': 'local', 'ai_available': enabled and bool(os.getenv('GROQ_API_KEY'))}

    @router.post('/insights/analyze')
    def analyze(days: int = Query(7, ge=1, le=30), _: int = Depends(verify_admin)):
        if os.getenv('ADMIN_INSIGHTS_AI_ENABLED','false').lower() != 'true':
            raise HTTPException(503, 'SI tahlili hali yoqilmagan. Lokal hisobot mavjud.')
        token = os.getenv('GROQ_API_KEY')
        if not token:
            raise HTTPException(503, 'SI kaliti serverda sozlanmagan.')
        data = snapshot(days)
        safe = {k: data[k] for k in ('start_date','end_date','users','tasks','limitations')}
        model = os.getenv('ADMIN_INSIGHTS_MODEL','openai/gpt-oss-20b')
        key = json.dumps([model, safe], sort_keys=True)
        # Serialize requests to avoid duplicate paid calls. Cache only aggregates.
        with _lock:
            cached = _cache.get(key)
            if cached and time.monotonic()-cached[0] < 1800:
                return {'ok': True, 'snapshot': data, 'report': cached[1], 'source': 'ai', 'cached': True}
            try:
                response = requests.post('https://api.groq.com/openai/v1/chat/completions',
                    headers={'Authorization': 'Bearer '+token}, timeout=(5, 35),
                    json={'model': model, 'temperature': 0.2, 'max_tokens': 1600, 'response_format': {'type':'json_object'},
                          'messages': [{'role':'system','content':'O‘zbekcha mahsulot tahlilchisi. Faqat berilgan raqamlarga tayan. Ketish sababini fakt deb aytma. Guruhlarni funnel deb aytma. JSON: facts, hypotheses, experiments — har biri 0–3 qisqa matn ro‘yxati. Maxrajlarni ko‘rsat; tajribalar taklif, avtomatik amal emas.'},
                                       {'role':'user','content':json.dumps(safe, ensure_ascii=False)}]})
                response.raise_for_status()
                report = validate_report(json.loads(response.json()['choices'][0]['message']['content']))
            except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
                return {'ok': True, 'snapshot': data, 'report': local_report(data), 'source': 'local', 'warning': 'SI javobi olinmadi. Lokal hisobot saqlandi.'}
            if len(_cache) >= 32:
                _cache.clear()
            _cache[key] = (time.monotonic(), report)
        return {'ok': True, 'snapshot': data, 'report': report, 'source': 'ai', 'cached': False}
