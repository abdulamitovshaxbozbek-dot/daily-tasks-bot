"""Tashkent schedules; persistent claims prevent duplicate worker execution.

Claims are committed before side effects. Interrupted/failed jobs need operator
review rather than automatic replay, since Telegram sends cannot be rolled back.
"""
import logging
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
ZONE = ZoneInfo("Asia/Tashkent")


def due_jobs(now):
    local = now.astimezone(ZONE)
    jobs = ["day_cycle"]
    if local.minute == 0:
        if 2 <= local.hour <= 11:
            jobs.append("reminders")
        if local.hour == 14:
            jobs.append("midday")
        if local.hour == 23:
            jobs.append("evening")
    return jobs


def initialize(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS public.qadam_schedule_runs (
            job TEXT NOT NULL, slot TIMESTAMPTZ NOT NULL,
            status TEXT NOT NULL DEFAULT 'started',
            finished_at TIMESTAMPTZ,
            PRIMARY KEY (job, slot)
        )""")
    conn.commit()


def claim(conn, job, slot):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO public.qadam_schedule_runs (job, slot)
            VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING job""", (job, slot))
        acquired = cur.fetchone() is not None
    conn.commit()
    return acquired


def run_tick(conn, now, handlers):
    slot = now.replace(second=0, microsecond=0)
    for job in due_jobs(now):
        if not claim(conn, job, slot):
            continue
        status = 'complete'
        try:
            result = handlers[job]()
            if isinstance(result, dict) and any(
                item.get('sent') is False for item in result.get('results', [])
            ):
                status = 'partial_failure'
        except Exception as error:
            status = 'failed'
            # Avoid exception strings containing Telegram request URLs/tokens.
            logger.error('Schedule %s failed (%s)', job, type(error).__name__)
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.qadam_schedule_runs
                SET status=%s, finished_at=now() WHERE job=%s AND slot=%s""",
                (status, job, slot))
        conn.commit()
        logger.info('Schedule %s: %s', job, status)


def worker(stop):
    import psycopg2
    from routes import handle_day_cycle, handle_reminders, handle_live_checklist_reminders
    handlers = {
        'day_cycle': handle_day_cycle,
        'reminders': handle_reminders,
        'midday': lambda: handle_live_checklist_reminders('midday'),
        'evening': lambda: handle_live_checklist_reminders('evening'),
    }
    while not stop.is_set():
        conn = None
        try:
            conn = psycopg2.connect(os.environ['DATABASE_URL'], connect_timeout=10)
            # Only one process initializes/runs the scheduler at a time.
            with conn.cursor() as cur:
                cur.execute('SELECT pg_try_advisory_lock(716240921)')
                leader = cur.fetchone()[0]
            conn.commit()
            if leader:
                initialize(conn)
                while not stop.is_set():
                    run_tick(conn, datetime.now(ZONE), handlers)
                    stop.wait(5)
        except Exception as error:
            logger.error('Scheduler connection failed (%s)', type(error).__name__)
        finally:
            if conn is not None:
                conn.close()
        stop.wait(5)


def start_scheduler():
    stop = threading.Event()
    thread = None
    if os.getenv('SCHEDULER_ENABLED', 'false').lower() == 'true':
        thread = threading.Thread(target=worker, args=(stop,), daemon=True)
        thread.start()
    return stop, thread
