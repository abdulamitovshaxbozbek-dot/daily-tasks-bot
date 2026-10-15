"""Opt-in integration tests: set TEST_DATABASE_URL to an empty throw-away PostgreSQL database
(its name must contain "test"). Creates minimal users/tasks tables if missing and TRUNCATES
users, tasks and the plan tables before every test. Telegram and Groq are stubbed.
"""
import os
import sys
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'api'))

CHAT = 7001
ZONE = ZoneInfo('Asia/Tashkent')


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'TEST_DATABASE_URL required')
class PlansDbTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        from psycopg2.extras import RealDictCursor
        from fastapi import APIRouter, FastAPI, Header
        from fastapi.testclient import TestClient
        import plans, ai, retention
        cls.plans, cls.ai, cls.psycopg2, cls.RDC = plans, ai, psycopg2, RealDictCursor
        cls.dsn = os.environ['TEST_DATABASE_URL']
        if 'test' not in (psycopg2.extensions.parse_dsn(cls.dsn).get('dbname') or '').lower():
            raise unittest.SkipTest('database name must contain "test": this suite truncates tables')
        c = psycopg2.connect(cls.dsn)
        with c.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS public.users (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), telegram_chat_id bigint UNIQUE,
              state text DEFAULT 'active', active_bot_id text, morning_time time, live_checklist_message_id bigint,
              live_checklist_date date, last_completion_notified_date date, last_active_date date, first_name text);
            CREATE TABLE IF NOT EXISTS public.tasks (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid, task_text text,
              task_date date, status text DEFAULT 'pending', reminder_time time, reminder_sent_at timestamptz);
            """)
        c.commit()
        cls.conn = c

        @contextmanager
        def get_connection():
            conn = psycopg2.connect(cls.dsn)
            try:
                yield conn
            finally:
                conn.rollback()
                conn.close()
        cls.get_connection = staticmethod(get_connection)
        import habits
        with get_connection() as conn:
            retention.initialize(conn)
            habits.initialize(conn)
            plans.initialize(conn)
        cls.sent, cls.answers, cls.refreshed = [], [], []
        cls.today = date(2026, 10, 15)
        cls.hour = 9

        def get_user(chat_id):
            with get_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("SELECT * FROM public.users WHERE telegram_chat_id=%s", (chat_id,))
                    return cur.fetchone()

        def transition_task(user, task_id, status, **kw):
            with get_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("UPDATE public.tasks SET status=%s WHERE id::text=%s AND status='pending' RETURNING *",
                                (status, str(task_id)))
                    row = cur.fetchone()
                conn.commit()
            return {'changed': True, 'task': row}

        router = APIRouter(prefix='/api')

        def auth(x_chat: int = Header(default=CHAT)):
            return x_chat
        handlers, callback, link = plans.install(
            router, get_connection, auth, lambda: '123', lambda: False,
            lambda chat, text, markup, parse_mode=None: cls.sent.append((chat, text, markup)) or {'ok': True},
            lambda cq, text=None: cls.answers.append(text), lambda chat: None,
            lambda: cls.today, get_user, transition_task, lambda *a: None,
            lambda chat, user: cls.refreshed.append(chat),
            retention.claim, 'https://example.test/plans-miniapp')
        cls.jobs, cls.callback, cls.link = handlers, staticmethod(callback), staticmethod(link)
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)
        plans.now_local = lambda: datetime.combine(cls.today, datetime.min.time(), ZONE).replace(hour=cls.hour)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def setUp(self):
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE public.tasks, public.users CASCADE; TRUNCATE public.qadam_plans CASCADE; "
                            "TRUNCATE public.qadam_habits, public.qadam_habit_days, public.qadam_notification_claims, "
                            "public.qadam_ai_usage CASCADE")
                cur.execute("INSERT INTO public.users(telegram_chat_id, active_bot_id) VALUES(%s,'123')", (CHAT,))
            conn.commit()
        self.__class__.today = date(2026, 10, 15)
        self.__class__.hour = 9
        self.sent.clear(); self.answers.clear(); self.refreshed.clear()
        self.ai._cooldown_until = 0
        self.ai._last_alert.clear()

    # helpers
    def q(self, sql, params=()):
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=self.RDC) as cur:
                cur.execute(sql, params)
                return cur.fetchall() if cur.description else []

    def x(self, sql, params=()):
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()

    ANSWERS = {'days': 25, 'mpu': 2, 'minutes': 60, 'slot': 'night', 'weekdays': list(range(7))}

    def create_book(self, mode='tasks', total=500, days=25, request_id='r' * 16):
        body = {'title': 'Kitob o‘qish', 'kind': 'volume', 'total': total, 'unit': 'bet',
                'answers': dict(self.ANSWERS, days=days)}
        steps = self.client.post('/api/miniapp/plans/preview', json=body).json()['steps']
        res = self.client.post('/api/miniapp/plans', json=dict(body, mode=mode, steps=steps, request_id=request_id))
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()['id']

    def plan(self):
        return self.client.get('/api/miniapp/plans').json()['plans'][0]

    # tests
    def test_draft_and_preview(self):
        d = self.client.post('/api/miniapp/plans/draft', json={'goal': "500 betli kitob o'qimoqchiman"}).json()
        self.assertEqual((d['kind'], d['total'], d['unit']), ('volume', 500, 'bet'))
        self.assertEqual(set(d['questions']), {'volume', 'stages', 'ramp'})
        p = self.client.post('/api/miniapp/plans/preview', json={
            'title': d['title'], 'kind': 'volume', 'total': 500, 'unit': 'bet', 'answers': self.ANSWERS}).json()
        self.assertEqual(sum(s['units'] or 0 for s in p['steps']), 500)
        self.assertTrue(p['feasibility']['ok'])
        self.assertEqual(p['summary']['buffer_days'], 3)

    def test_impossible_plan_is_reported_honestly(self):
        p = self.client.post('/api/miniapp/plans/preview', json={
            'title': 'Kitob', 'kind': 'volume', 'total': 500, 'unit': 'bet',
            'answers': dict(self.ANSWERS, days=7, minutes=30)}).json()
        self.assertFalse(p['feasibility']['ok'])
        self.assertGreater(p['feasibility']['suggest_days'], 7)

    def test_create_materializes_todays_task_once_and_is_idempotent(self):
        pid = self.create_book()
        again = self.client.post('/api/miniapp/plans', json={
            'title': 'Kitob o‘qish', 'kind': 'volume', 'total': 500, 'unit': 'bet', 'answers': self.ANSWERS,
            'steps': [{'day': '2026-10-15', 'title': 'x', 'units': 500}], 'request_id': 'r' * 16}).json()
        self.assertEqual(again['id'], pid)
        tasks = self.q("SELECT task_text, task_date, reminder_time FROM public.tasks")
        self.assertEqual(len(tasks), 1)
        self.assertTrue(tasks[0]['task_text'].startswith('🎯 Kitob o‘qish: 1–'))
        self.assertEqual(str(tasks[0]['reminder_time'])[:5], '21:30')
        self.jobs['daily']()
        self.assertEqual(len(self.q("SELECT 1 FROM public.tasks")), 1)

    def test_completed_task_updates_progress_and_sends_milestone(self):
        self.create_book(total=100, days=10)
        self.x("UPDATE public.tasks SET status='completed'")
        plan = self.plan()
        self.assertEqual(plan['today_step']['status'], 'done')
        self.assertGreaterEqual(plan['progress'], 10)
        self.__class__.today += timedelta(days=1)
        self.jobs['daily']()
        self.__class__.today += timedelta(days=1)
        self.jobs['daily']()
        self.x("UPDATE public.tasks SET status='completed'")
        self.jobs['daily']()
        texts = ' '.join(m[1] for m in self.sent)
        self.assertIn('Yaxshi boshlanish', texts)
        # a milestone is never repeated
        self.sent.clear()
        self.jobs['daily']()
        self.assertEqual(self.sent, [])

    def test_checkin_sends_one_message_per_day_with_buttons(self):
        self.create_book()
        self.jobs['checkin']()
        self.assertEqual(len(self.sent), 1)
        chat, text, markup = self.sent[0]
        self.assertIn('Bugungi qadam', text)
        data = [b.get('callback_data') for row in markup['inline_keyboard'] for b in row]
        self.assertTrue(any(d and d.startswith('plan|done|') for d in data))
        self.jobs['checkin']()
        self.assertEqual(len(self.sent), 1)

    def test_done_button_completes_task_and_step(self):
        self.create_book()
        step_id = self.plan()['today_step']['id']
        self.callback(CHAT, 'plan|done|%s' % step_id, 'cq1')
        self.assertEqual(self.q("SELECT status FROM public.tasks")[0]['status'], 'completed')
        self.assertEqual(self.plan()['today_step']['status'], 'done')
        self.assertIn('✅ Barakalla!', self.answers)

    def test_two_missed_days_trigger_comeback_and_replan_conserves_total(self):
        self.create_book(total=300, days=12)
        for _ in range(3):
            self.__class__.today += timedelta(days=1)
            self.jobs['daily']()
        self.sent.clear()
        self.jobs['checkin']() if False else None
        self.__class__.hour = 20
        self.jobs['checkin']()
        texts = ' '.join(m[1] for m in self.sent)
        self.assertIn('orqada qoldi', texts)
        res = self.client.post('/api/miniapp/plans/%s/replan' % self.plan()['id'], json={'extend_days': 0}).json()
        self.assertTrue(res['ok'])
        steps = self.plan()['steps']
        done = sum(s['units'] or 0 for s in steps if s['status'] == 'done')
        pending = sum(s['units'] or 0 for s in steps if s['status'] == 'pending' and not s['is_buffer'])
        self.assertEqual(done + pending, 300)
        self.assertTrue(all(s['day'] >= '2026-10-18' for s in steps if s['status'] == 'pending'))
        # pending tasks of removed steps are gone; today's new task exists exactly once
        todays = self.q("SELECT 1 FROM public.tasks WHERE task_date=%s AND status='pending'", (self.today,))
        self.assertEqual(len(todays), 1)

    def test_later_button_skips_and_redistributes(self):
        self.create_book(total=200, days=10)
        step_id = self.plan()['today_step']['id']
        self.callback(CHAT, 'plan|later|%s' % step_id, 'cq2')
        plan = self.plan()
        self.assertEqual(plan['steps'][0]['status'], 'skipped')
        pending = sum(s['units'] or 0 for s in plan['steps'] if s['status'] == 'pending' and not s['is_buffer'])
        self.assertEqual(pending, 200)
        self.assertEqual(self.q("SELECT 1 FROM public.tasks WHERE status='pending'"), [])  # nothing left for today

    def test_stop_nagging_after_three_unanswered(self):
        self.create_book()
        self.x("UPDATE public.qadam_plans SET unanswered=3")
        self.jobs['checkin']()
        self.assertEqual(self.plan()['status'], 'paused')
        self.assertIn('to‘xtatdim', self.sent[0][1])

    def test_pause_and_resume(self):
        pid = self.create_book()
        self.assertEqual(len(self.q("SELECT 1 FROM public.tasks")), 1)
        self.client.post('/api/miniapp/plans/%s/status' % pid, json={'action': 'pause'})
        self.assertEqual(self.q("SELECT 1 FROM public.tasks"), [])
        self.__class__.today += timedelta(days=3)
        res = self.client.post('/api/miniapp/plans/%s/status' % pid, json={'action': 'resume'}).json()
        self.assertTrue(res['ok'])
        self.assertEqual(self.plan()['status'], 'active')
        self.assertEqual(len(self.q("SELECT 1 FROM public.tasks WHERE status='pending'")), 1)

    def test_habit_mode_creates_habit_not_tasks(self):
        pid = self.create_book(mode='habit')
        self.assertEqual(self.q("SELECT 1 FROM public.tasks"), [])
        habits = self.q("SELECT title, active, reminder_time FROM public.qadam_habits")
        self.assertEqual(len(habits), 1)
        self.assertIn('kuniga ~', habits[0]['title'])
        self.client.post('/api/miniapp/plans/%s/status' % pid, json={'action': 'pause'})
        self.assertFalse(self.q("SELECT active FROM public.qadam_habits")[0]['active'])

    def test_stages_need_ai_and_use_it(self):
        body = {'title': 'IELTS ga tayyorlanish', 'kind': 'stages', 'answers': dict(self.ANSWERS, days=14)}
        with patch.object(self.ai, 'chat', side_effect=self.ai.AIUnavailable('rate_limit')):
            res = self.client.post('/api/miniapp/plans/preview', json=body)
        self.assertEqual(res.status_code, 503)
        payload = res.json()
        self.assertTrue(payload['ai_unavailable'])
        self.assertIn('@Shakhbozbek_Abdulamitov', payload['detail'])
        fake = '{"stages":[{"title":"Diagnostik test","minutes":60},{"title":"Reading mashqlari","minutes":45},' \
               '{"title":"Writing Task 1","minutes":50},{"title":"Speaking amaliyoti","minutes":40}]}'
        with patch.object(self.ai, 'chat', return_value=fake):
            res = self.client.post('/api/miniapp/plans/preview', json=body)
        self.assertEqual(res.status_code, 200, res.text)
        titles = [s['title'] for s in res.json()['steps']]
        self.assertEqual(titles, ['Diagnostik test', 'Reading mashqlari', 'Writing Task 1', 'Speaking amaliyoti'])

    def test_ramp_plan_and_habit(self):
        body = {'title': 'Yugurish', 'kind': 'ramp', 'answers': {'days': 28, 'start_minutes': 10, 'target_minutes': 30,
                                                                  'slot': 'morning', 'weekdays': [0, 2, 4]}}
        p = self.client.post('/api/miniapp/plans/preview', json=body).json()
        self.assertEqual(p['steps'][0]['minutes'], 10)
        self.assertEqual(p['steps'][-1]['minutes'], 30)
        self.assertTrue(all(datetime.fromisoformat(s['day']).weekday() in (0, 2, 4) for s in p['steps']))

    def test_coach_chat_and_daily_cap(self):
        pid = self.create_book()
        reply = '{"reply":"Bugun faqat 5 daqiqa o‘qing.","action":"replan"}'
        with patch.object(self.ai, 'chat', return_value=reply):
            res = self.client.post('/api/miniapp/plans/%s/chat' % pid, json={'message': 'Charchadim'}).json()
            self.assertEqual(res['suggest'][0]['action'], 'replan')
            hist = self.client.get('/api/miniapp/plans/%s/chat' % pid).json()['messages']
            self.assertEqual([m['role'] for m in hist], ['user', 'assistant'])
            for _ in range(14):
                self.assertEqual(self.client.post('/api/miniapp/plans/%s/chat' % pid, json={'message': 'yana'}).status_code, 200)
            self.assertEqual(self.client.post('/api/miniapp/plans/%s/chat' % pid, json={'message': 'yana'}).status_code, 429)

    def test_other_users_cannot_touch_plans(self):
        pid = self.create_book()
        self.x("INSERT INTO public.users(telegram_chat_id, active_bot_id) VALUES(7002,'123')")
        h = {'x-chat': '7002'}
        self.assertEqual(self.client.post('/api/miniapp/plans/%s/replan' % pid, json={}, headers=h).status_code, 404)
        self.assertEqual(self.client.delete('/api/miniapp/plans/%s' % pid, headers=h).status_code, 404)
        self.assertEqual(self.client.get('/api/miniapp/plans', headers=h).json()['plans'], [])

    def test_deleting_the_task_skips_the_step(self):
        self.create_book()
        self.x("DELETE FROM public.tasks")
        self.assertEqual(self.plan()['today_step']['status'], 'skipped')
        self.jobs['daily']()
        self.assertEqual(self.q("SELECT 1 FROM public.tasks"), [])

    def test_ai_failure_alerts_admin_once_per_hour(self):
        notes = []
        self.ai.configure(notes.append, __import__('retention').claim.__get__(None, None) if False else
                          lambda kind, key: __import__('retention').claim(self.get_connection, '123', 'admin', kind, key))

        class R:
            status_code = 429
            headers = {'retry-after': '30'}
        for _ in range(3):
            with self.assertRaises(self.ai.AIUnavailable) as ctx:
                self.ai.check_response(R(), 'test')
        self.assertEqual(ctx.exception.reason, 'rate_limit')
        self.assertEqual(len(notes), 1)
        self.assertIn('limiti tugagan', notes[0])
        self.assertFalse(self.ai.available())


if __name__ == '__main__':
    unittest.main()
