"""Best-effort event journal, without task text, names or Telegram IDs."""
import uuid
import queue
import threading
import time
from contextvars import ContextVar
from datetime import timedelta
from fastapi import Depends, Query
from pydantic import BaseModel, Field
from psycopg2.extras import RealDictCursor

session = ContextVar('journey_session', default=None)
_events = queue.Queue(maxsize=2000)
_ready = False
_dropped = 0


def valid_session(value):
    try: return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError): return None


def record(user_id, event, source=None, event_id=None):
    global _dropped
    if not _ready or not user_id:
        return
    try:
        _events.put_nowait((str(user_id), event, source or ('miniapp' if session.get() else 'chat'), session.get(), event_id or str(uuid.uuid4())))
    except queue.Full:
        _dropped += 1


def start(get_connection, bot_id):
    global _ready
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('''CREATE TABLE IF NOT EXISTS public.qadam_journey_events (
                    event_id UUID PRIMARY KEY, bot_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    session_id UUID, event TEXT NOT NULL, source TEXT NOT NULL,
                    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now());
                    CREATE INDEX IF NOT EXISTS qadam_journey_bot_time ON public.qadam_journey_events(bot_id,recorded_at);
                    CREATE INDEX IF NOT EXISTS qadam_journey_user_time ON public.qadam_journey_events(bot_id,user_id,recorded_at);''')
            conn.commit()
        _ready = True
    except Exception:
        return lambda: None
    stop = threading.Event()
    def worker():
        global _dropped
        while not stop.is_set():
            try: row = _events.get(timeout=1)
            except queue.Empty: continue
            try:
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute('''INSERT INTO public.qadam_journey_events(event_id,bot_id,user_id,event,source,session_id)
                            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''', (row[4],str(bot_id()),row[0],row[1],row[2],row[3]))
                    conn.commit()
            except Exception:
                _dropped += 1
            finally: _events.task_done()
    threading.Thread(target=worker, daemon=True, name='qadam-journey').start()
    return stop.set


class ClientEvent(BaseModel):
    event: str = Field(pattern='^(miniapp_open|task_form_open|task_submit_attempt|task_form_cancel|task_request_error)$')
    session_id: uuid.UUID
    event_id: uuid.UUID


def install(router, admin_router, get_chat_id, get_user, verify_admin, get_connection, bot_id):
    @router.post('/miniapp/events')
    def client_event(payload: ClientEvent, chat_id: int = Depends(get_chat_id)):
        user = get_user(chat_id)
        if not user or user.get('state') == 'blocked':
            from fastapi import HTTPException
            raise HTTPException(403, 'Avval botni boshlang.')
        token = session.set(str(payload.session_id))
        try: record(user['id'],payload.event,'miniapp',str(payload.event_id))
        finally: session.reset(token)
        return {'ok':True,'tracking_available':_ready}

    @admin_router.get('/journey')
    def report(days: int = Query(7, ge=1, le=30), _: int = Depends(verify_admin)):
        if not _ready:
            return {'ok':True,'available':False,'note':'Qadamlar jurnali ishga tushmagan. Bot funksiyalari ishlashda davom etadi.'}
        with get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Tashkent')::date AS today")
                today=cur.fetchone()['today']; start_date=today-timedelta(days=days-1)
                params=(str(bot_id()),start_date,today)
                cur.execute('''SELECT event,source,COUNT(*) AS occurrences,COUNT(DISTINCT user_id) AS users
                    FROM public.qadam_journey_events WHERE bot_id=%s
                    AND (recorded_at AT TIME ZONE 'Asia/Tashkent')::date BETWEEN %s AND %s
                    GROUP BY event,source ORDER BY event,source''',params)
                counts=[dict(r) for r in cur.fetchall()]
                # Form openings are mature only after 24h. Sessions keep unrelated saves apart.
                cur.execute('''WITH forms AS (
                    SELECT user_id,session_id,MIN(recorded_at) AS opened FROM public.qadam_journey_events
                    WHERE bot_id=%s AND event='task_form_open'
                    AND (recorded_at AT TIME ZONE 'Asia/Tashkent')::date BETWEEN %s AND %s
                    GROUP BY user_id,session_id
                ), outcomes AS (
                    SELECT f.*,EXISTS(SELECT 1 FROM public.qadam_journey_events e WHERE e.bot_id=%s
                    AND e.user_id=f.user_id AND e.session_id=f.session_id AND e.event='task_saved'
                    AND e.recorded_at>=f.opened AND e.recorded_at<f.opened+interval '24 hours') AS saved
                    FROM forms f WHERE f.opened<now()-interval '24 hours'
                ) SELECT COUNT(*) AS mature_sessions,COUNT(*) FILTER(WHERE saved) AS saved_sessions,
                    COUNT(*) FILTER(WHERE NOT saved) AS without_save FROM outcomes''',(*params,str(bot_id())))
                funnel=dict(cur.fetchone())
        return {'ok':True,'available':True,'start_date':str(start_date),'end_date':str(today),'counts':counts,
            'form_outcomes':funnel,'dropped_events_this_worker':_dropped,
            'limitations':['Kuzatuv faqat ushbu versiya ishga tushganidan keyingi amallarni ko‘rsatadi.',
            'Oyna ochish va urinishlar mijoz qaydi; saqlash va belgilash serverdagi muvaffaqiyatdan keyin qayd qilinadi.',
            '24 soatda shu sessiyada saqlamaganlik botni butunlay tark etganlik emas.',
            'Tarmoq uzilishi, boshqa sessiyadan davom etish va jurnal yo‘qotishlari natijaga ta’sir qiladi.']}

    return report
