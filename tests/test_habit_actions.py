"""Hermetic tests exercise the production callback with a transactional DB double.
No Telegram credentials, network or production database needed.
"""
import ast
import copy
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from types import SimpleNamespace

class HTTPException(Exception):
    def __init__(self, status_code, detail): self.detail=detail; self.status_code=status_code

class Router:
    def __init__(self): self.routes={}
    def __getattr__(self, name):
        def decorator(path, **kw):
            def register(f): self.routes[(name,path)]=f;return f
            return register
        return decorator

class Cursor:
    def __init__(self, db): self.db=db; self.result=None
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def execute(self, sql, params=()):
        if sql.startswith('SELECT pg_advisory_xact_lock'): self.result=None
        elif sql.startswith('SELECT id FROM public.users'): self.result={'id':1}
        elif sql.startswith('SELECT result FROM public.qadam_habit_requests'):
            result=self.db.requests.get(params[2]);self.result=(result,) if result else None
        elif sql.startswith('SELECT count(*) FROM public.qadam_habits'): self.result=(self.db.created,)
        elif sql.startswith('INSERT INTO public.qadam_habits'):
            self.db.created+=1;self.result=(self.db.created,)
        elif sql.startswith('INSERT INTO public.qadam_habit_requests'):
            self.db.requests[params[2]]=params[3]
        elif sql.startswith('SELECT * FROM public.qadam_habit_days'):
            self.result=copy.deepcopy(self.db.row) if params[:3]==(1,'123',101) else None
        elif sql.startswith('SELECT active,kind'): self.result={'active':self.db.active,'kind':'zikr','weekdays':list(range(7))}
        elif sql.startswith('UPDATE public.qadam_habit_days'):
            r=self.db.row
            if 'status=previous_status' not in sql:
                r['previous_snooze_at']=r['snooze_at'];r['previous_reminder_state']=r['reminder_state']
            if 'status=previous_status' in sql:
                r['status']=r['previous_status'];r['previous_status']=None;r['snooze_at']=r.get('previous_snooze_at');r['reminder_state']=r.get('previous_reminder_state')
            elif 'snooze_count=snooze_count+1' in sql:
                r['snooze_at']=params[0];r['snooze_count']+=1;r['previous_status']='pending';r['reminder_state']=None
            else:
                r['previous_status']=r['status'];r['status']=params[0];r['marked_at']=self.db.now;r['snooze_at']=None
            r['revision']+=1
        else: raise AssertionError(sql)
    def fetchone(self): return self.result

class DB:
    def __init__(self, now):
        self.now=now;self.active=True;self.requests={};self.created=0
        self.row=dict(id=1,scope='123',chat_id=101,kind='prayer',ref='bomdod',day=now.date(),title='Bomdod',due_at=now-timedelta(hours=1),status='pending',revision=0,previous_status=None,item_kind=None,snooze_count=0,snooze_at=None,reminder_state='sent',marked_at=None)
    def cursor(self,**kw): return Cursor(self)
    def commit(self): pass
    def rollback(self): pass

class Actions(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,3,10,tzinfo=ZoneInfo('Asia/Tashkent'))
        self.db=DB(self.now);self.sent=[];self.deleted=[];self.answers=[];self.activity=[];self.edits=[]
        @contextmanager
        def connection(): yield self.db
        tree=ast.parse((Path(__file__).resolve().parents[1]/'api/habits.py').read_text())
        install=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='install')
        ns=dict(contextmanager=contextmanager,initialize=lambda c:None,RealDictCursor=None,Depends=lambda x:None,
            HTTPException=HTTPException,PrayerSettings=object,HabitInput=object,MarkInput=object,Optional=object,
            re=__import__('re'),Json=lambda x:x,now_local=lambda:self.now,datetime=datetime,timedelta=timedelta,html=__import__('html'),LOG=SimpleNamespace(warning=lambda *a:None))
        self.streak=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='streak_stats')
        exec(compile(ast.Module(body=[self.streak,install],type_ignores=[]),'habits.py','exec'),ns)
        self.streak_fn=ns['streak_stats']
        self.router=Router()
        _,self.callback,_=ns['install'](self.router,connection,lambda:101,lambda:'123',lambda:True,
            lambda *a,**kw:self.sent.append((a,kw)),lambda *a:self.answers.append(a),
            lambda *a:self.deleted.append(a),lambda *a:self.activity.append(a),lambda *a:self.edits.append(a))
    def test_duplicate_create_request_does_not_add_second_habit(self):
        create=self.router.routes[('post','/miniapp/habits')]
        payload=SimpleNamespace(title='Kitob',kind='habit',reminder_time='08:00',request_id='unique-request-123',weekdays=list(range(7)))
        self.assertEqual(create(payload,101),create(payload,101));self.assertEqual(self.db.created,1)
    def call(self,action,rev=0): self.callback(101,f'habit|1|{action}|{rev}','cb',88)
    def test_miniapp_mark_then_chat_stale_callback(self):
        endpoint=self.router.routes[('put','/miniapp/habit-entries/{entry_id}')]
        result=endpoint(1,SimpleNamespace(status='done',revision=0),101)
        self.assertIn('qabul qilsin',result['message']);self.assertEqual(self.sent,[])
        self.call('done');self.assertEqual(self.sent,[]);self.assertEqual(self.db.row['revision'],1)
    def test_web_conflict_is_reported(self):
        self.db.row['due_at']=self.now+timedelta(hours=1)
        with self.assertRaises(HTTPException):self.callback(101,'habit|1|done|0',None,web=True)
    def test_streak_skips_unplanned_weekend(self):
        monday=self.now.date()+timedelta(days=2)
        friday=monday-timedelta(days=3)
        result=self.streak_fn([{'day':friday,'status':'done'},{'day':monday,'status':'done'}],monday,[0,2,4])
        self.assertEqual(result,{'current':2,'best':2})
    def test_empty_weekdays_rejected(self):
        create=self.router.routes[('post','/miniapp/habits')]
        with self.assertRaises(HTTPException):create(SimpleNamespace(weekdays=[]),101)
    def test_prayer_success_duplicate_only_one_message(self):
        self.call('done');self.call('done')
        self.assertEqual(self.db.row['status'],'done');self.assertEqual(len(self.sent),1)
        self.assertIn('Bomdod namozi o‘qildi',self.sent[0][0][1]);self.assertIn('qabul qilsin',self.sent[0][0][1]);self.assertEqual(self.deleted,[(101,88)])
    def test_qaza_and_undo_duplicate(self):
        self.call('missed');self.assertIn('qazo namozlaringizga',self.sent[0][0][1])
        self.call('undo',1);self.call('undo',1)
        self.assertEqual(self.db.row['status'],'pending');self.assertEqual(len(self.sent),2)
        self.assertEqual(self.db.row['reminder_state'],'sent')
    def test_stale_button_after_undo(self):
        self.call('done');self.call('undo',1);self.call('missed',0)
        self.assertEqual(self.db.row['status'],'pending');self.assertEqual(len(self.sent),2)
    def test_qaza_completion_and_undo(self):
        self.call('missed');self.call('qaza_done',1);self.call('undo',2)
        self.assertEqual(self.db.row['status'],'missed')
    def test_future_prayer_cannot_mark(self):
        self.db.row['due_at']=self.now+timedelta(hours=1);self.call('done')
        self.assertEqual(self.sent,[]);self.assertEqual(self.db.row['revision'],0)
    def zikr(self): self.db.row.update(kind='habit',ref='2',title='Ertalabki zikr',item_kind='zikr')
    def test_snooze_double_click_and_undo(self):
        self.zikr();self.call('s30');self.call('s60')
        self.assertEqual(self.db.row['snooze_count'],1);self.assertEqual(self.db.row['snooze_at'],self.now+timedelta(minutes=30));self.assertEqual(len(self.sent),1)
        self.call('undo',1);self.assertIsNone(self.db.row['snooze_at'])
    def test_later_edits_instead_of_sending(self):
        self.zikr();self.call('later');self.call('later');self.assertEqual(self.sent,[]);self.assertEqual(self.db.row['revision'],0)
        self.assertEqual(len(self.edits),2);self.assertEqual(len(self.edits[0][3]['inline_keyboard'][0]),3)
    def test_zikr_completion(self):
        self.zikr();self.call('done');self.assertIn('qabul qilsin',self.sent[0][0][1])
    def test_archived_cannot_mark(self):
        self.zikr();self.db.active=False;self.call('done');self.assertEqual(self.sent,[])
    def test_cross_owner_cannot_mark(self):
        self.callback(999,'habit|1|done|0','cb',88);self.assertEqual(self.sent,[])
    def test_snoozed_zikr_can_complete_after_midnight(self):
        self.zikr();self.db.row.update(day=self.now.date()-timedelta(days=1),snooze_count=1)
        self.call('done');self.assertEqual(self.db.row['status'],'done')

if __name__=='__main__': unittest.main()
