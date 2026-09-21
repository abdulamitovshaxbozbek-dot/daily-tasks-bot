"""Opt-in tests use temporary tables only; production records are never read."""
import os, sys, unittest
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'api'))

@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'TEST_DATABASE_URL required')
class MigrationTests(unittest.TestCase):
    def setUp(self):
        import psycopg2
        os.environ['DATABASE_URL'] = os.environ['TEST_DATABASE_URL']
        os.environ['TELEGRAM_TOKEN'] = '123:test-only-not-a-real-token'
        os.environ['REQUIRE_BOT_START'] = 'true'
        import routes, bot_identity, scheduler
        self.r, self.b, self.s = routes, bot_identity, scheduler
        self.conn = psycopg2.connect(os.environ['TEST_DATABASE_URL'], sslmode='require')
        with self.conn.cursor() as c:
            for table in ('users', 'tasks', 'pending_voice_tasks'):
                c.execute(f'CREATE TEMP TABLE {table} (LIKE public.{table} INCLUDING ALL)')
        raw=self.conn
        class Cursor:
            def __init__(self, **kw): self.c=raw.cursor(**kw)
            def __enter__(self): return self
            def __exit__(self,*args): self.c.close()
            def execute(self, q, params=None): return self.c.execute(q.replace('public.', 'pg_temp.'),params)
            def __getattr__(self,k):return getattr(self.c,k)
        class Connection:
            def cursor(self,**kw):return Cursor(**kw)
            def commit(self):pass # All test mutations remain in the rollback-only transaction.
        self.proxy=Connection()
        @contextmanager
        def conn():yield self.proxy
        self.patches=[patch.object(routes,'get_connection',conn),patch.object(bot_identity,'get_connection',conn)]
        for p in self.patches:p.start()
        self.b.initialize_identity()
        with self.conn.cursor() as c:
            c.execute("""INSERT INTO pg_temp.users (telegram_chat_id,first_name,state,morning_time,live_checklist_message_id,live_checklist_date)
                VALUES (101,'Test A','active','08:00',999,CURRENT_DATE),(102,'Test B','active','08:00',998,CURRENT_DATE)""")
        self.sent=[]
        self.patches += [patch.object(routes,'telegram_send_message',lambda chat,*a:self.sent.append(chat) or {'ok':True}),patch.object(routes,'telegram_send_message_with_keyboard',lambda chat,*a:self.sent.append(chat) or {'ok':True})]
        for p in self.patches[2:]:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.conn.rollback();self.conn.close()
    def test_registration_preserves_history_and_resets_only_once(self):
        self.b.register_chat(101)
        with self.conn.cursor() as c:
            c.execute('SELECT active_bot_id,live_checklist_message_id FROM pg_temp.users WHERE telegram_chat_id=101')
            self.assertEqual(c.fetchone(),('123',None))
            c.execute('UPDATE pg_temp.users SET live_checklist_message_id=55 WHERE telegram_chat_id=101')
        self.b.register_chat(101)
        with self.conn.cursor() as c:
            c.execute('SELECT live_checklist_message_id FROM pg_temp.users WHERE telegram_chat_id=101')
            self.assertEqual(c.fetchone()[0],55)
            c.execute('SELECT count(*) FROM pg_temp.qadam_bot_migrations')
            self.assertEqual(c.fetchone()[0],1)
    def test_unjoined_users_are_not_reminded(self):
        self.b.register_chat(101)
        self.r.handle_reminders()
        self.assertEqual(self.sent,[101])
        self.r.handle_reminders()
        self.assertEqual(self.sent,[101])
    def test_checklist_filters_nonjoined_users(self):
        self.b.register_chat(101)
        with self.conn.cursor() as c:
            c.execute("INSERT INTO pg_temp.tasks (user_id,task_date,task_text,status) SELECT id, (now() AT TIME ZONE 'Asia/Tashkent')::date, 'Test', 'pending' FROM pg_temp.users")
        with patch.object(self.r,'refresh_live_checklist',lambda chat,*a,**kw:self.sent.append(chat) or {'pending':1}):
            self.r.handle_live_checklist_reminders('midday')
        self.assertEqual(self.sent,[101])
    def test_persistent_claim_does_not_replay_same_slot(self):
        self.s.initialize(self.proxy)
        now=datetime(2026,9,21,11,tzinfo=self.s.ZONE)
        calls=[]
        handlers={k:lambda k=k:calls.append(k) for k in ['day_cycle','reminders','midday','evening']}
        self.s.run_tick(self.proxy,now,handlers)
        self.s.run_tick(self.proxy,now+timedelta(seconds=25),handlers)
        self.assertEqual(calls,['day_cycle','reminders'])
