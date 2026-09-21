"""Keep task history while safely changing the Telegram bot identity."""
import hmac
import os
from fastapi import HTTPException
from database import get_connection


def bot_id():
    return os.environ.get('TELEGRAM_TOKEN', '').split(':', 1)[0]


def require_joined():
    return os.getenv('REQUIRE_BOT_START', 'false').lower() == 'true'


def verify_webhook(received):
    expected = os.getenv('TELEGRAM_WEBHOOK_SECRET', '')
    if expected and not hmac.compare_digest(expected, received or ''):
        raise HTTPException(401, 'Invalid webhook secret')
    if require_joined() and not expected:
        raise HTTPException(503, 'New bot webhook is not configured')


def initialize_identity():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(716240922)')
            cur.execute('ALTER TABLE public.users ADD COLUMN IF NOT EXISTS active_bot_id TEXT')
            cur.execute('''CREATE TABLE IF NOT EXISTS public.qadam_bot_migrations (
                user_id TEXT NOT NULL, bot_id TEXT NOT NULL,
                old_user JSONB NOT NULL, migrated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (user_id, bot_id)
            )''')
        conn.commit()


def register_chat(chat_id):
    if not require_joined():
        return
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id FROM public.users WHERE telegram_chat_id=%s FOR UPDATE', (chat_id,))
            row = cur.fetchone()
            if row is None:
                return
            cur.execute('''INSERT INTO public.qadam_bot_migrations (user_id, bot_id, old_user)
                SELECT id::text, %s, to_jsonb(u) FROM public.users u
                WHERE telegram_chat_id=%s AND active_bot_id IS DISTINCT FROM %s
                ON CONFLICT DO NOTHING''', (bot_id(), chat_id, bot_id()))
            # Old message IDs refer to a different Telegram chat with the old bot.
            # Keep tasks, dates and completion history; retain the old user snapshot.
            cur.execute('''UPDATE public.users SET active_bot_id=%s,
                live_checklist_message_id=NULL, live_checklist_date=NULL,
                state=CASE WHEN state='blocked' THEN
                    CASE WHEN morning_time IS NULL THEN 'waiting_morning_time' ELSE 'active' END
                    ELSE state END
                WHERE telegram_chat_id=%s AND active_bot_id IS DISTINCT FROM %s''',
                (bot_id(), chat_id, bot_id()))
        conn.commit()
