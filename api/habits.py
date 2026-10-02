"""Opt-in daily habits and prayer journal. All dates are Asia/Tashkent.

Reminder claims are committed before Telegram sends: at most one send attempt
per occurrence. Failed/uncertain sends remain visible for operator review.
"""
import html
import logging
import re
import threading
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from typing import Literal, Optional
from zoneinfo import ZoneInfo

import requests
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from psycopg2.extras import RealDictCursor, Json

ZONE = ZoneInfo("Asia/Tashkent")
LOG = logging.getLogger(__name__)
SOURCE = "https://namoz-vaqti.uz/index.php"
PRAYERS = {"bomdod": "Bomdod", "peshin": "Peshin", "asr": "Asr", "shom": "Shom", "xufton": "Xufton"}
REGIONS = {
    "toshkent-shahri": "Toshkent shahri", "toshkent-viloyati": "Toshkent viloyati",
    "andijon-viloyati": "Andijon viloyati", "buxoro-viloyati": "Buxoro viloyati",
    "fargona-viloyati": "Farg‘ona viloyati", "jizzax-viloyati": "Jizzax viloyati",
    "namangan-viloyati": "Namangan viloyati", "navoiy-viloyati": "Navoiy viloyati",
    "qashqadaryo-viloyati": "Qashqadaryo viloyati", "qoraqalpogiston": "Qoraqalpog‘iston Respublikasi",
    "samarqand-viloyati": "Samarqand viloyati", "sirdaryo-viloyati": "Sirdaryo viloyati",
    "surxondaryo-viloyati": "Surxondaryo viloyati", "xorazm-viloyati": "Xorazm viloyati",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS public.qadam_habit_settings (
 scope TEXT NOT NULL, chat_id BIGINT NOT NULL, region TEXT NOT NULL,
 enabled BOOLEAN NOT NULL DEFAULT TRUE, enabled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(scope, chat_id)
);
CREATE TABLE IF NOT EXISTS public.qadam_habits (
 id BIGSERIAL PRIMARY KEY, scope TEXT NOT NULL, chat_id BIGINT NOT NULL,
 title TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('habit','zikr')),
 reminder_time TIME NOT NULL, active BOOLEAN NOT NULL DEFAULT TRUE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS qadam_habits_owner ON public.qadam_habits(scope, chat_id);
CREATE TABLE IF NOT EXISTS public.qadam_habit_days (
 id BIGSERIAL PRIMARY KEY, scope TEXT NOT NULL, chat_id BIGINT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('habit','prayer')), ref TEXT NOT NULL,
 day DATE NOT NULL, title TEXT NOT NULL, due_at TIMESTAMPTZ NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','done','missed','qaza_done')),
 reminder_state TEXT CHECK(reminder_state IN ('started','sent','failed','skipped')),
 marked_at TIMESTAMPTZ, UNIQUE(scope,chat_id,kind,ref,day)
);
CREATE INDEX IF NOT EXISTS qadam_habit_days_owner ON public.qadam_habit_days(scope,chat_id,day);
CREATE TABLE IF NOT EXISTS public.qadam_prayer_cache (
 region TEXT NOT NULL, day DATE NOT NULL, times JSONB NOT NULL,
 PRIMARY KEY(region,day)
);
"""
_ready = False
_schema_lock = threading.Lock()
_fetch_lock = threading.Lock()
_retry_after = {}


def now_local():
    return datetime.now(ZONE)


def initialize(conn):
    global _ready
    with _schema_lock:
        if not _ready:
            with conn.cursor() as cur:
                # Serializes startup DDL across Uvicorn processes.
                cur.execute("SELECT pg_advisory_xact_lock(716240922)")
                cur.execute(SCHEMA)
            conn.commit()
            _ready = True


def validate_times(payload, region, today):
    meta = payload.get("meta", {})
    if meta.get("date") != today.isoformat() or meta.get("region", {}).get("slug") != region:
        raise ValueError("Prayer source date/region mismatch")
    times = payload.get("today", {}).get("times", {})
    keys = ["bomdod", "quyosh", "peshin", "asr", "shom", "xufton"]
    values = [times.get(k, "") for k in keys]
    if any(not isinstance(v, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", v) for v in values):
        raise ValueError("Invalid prayer times")
    if values != sorted(values) or len(set(values)) != 6:
        raise ValueError("Prayer times out of order")
    return {k: times[k] for k in keys}


def streak_stats(rows, today):
    """Today may still be pending; yesterday keeps an ongoing streak alive."""
    done = {r["day"] for r in rows if r["status"] == "done" and r["day"] <= today}
    current = best = run = 0
    previous = None
    for day in sorted(done):
        run = run + 1 if previous == day - timedelta(days=1) else 1
        best = max(best, run)
        previous = day
    cursor = today if today in done else today - timedelta(days=1)
    while cursor in done:
        current += 1
        cursor -= timedelta(days=1)
    if any(r["day"] == today and r["status"] == "missed" for r in rows):
        current = 0
    return {"current": current, "best": best}


class PrayerSettings(BaseModel):
    region: str
    enabled: bool = True


class HabitInput(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    kind: Literal["habit", "zikr"] = "habit"
    reminder_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class MarkInput(BaseModel):
    status: Literal["done", "missed", "qaza_done"]


def install(router, get_connection, auth, bot_id, require_joined, send, answer):
    """Integrates without importing routes or changing its existing handlers."""
    def scope():
        return str(bot_id() or "default")

    @contextmanager
    def connection():
        with get_connection() as conn:
            initialize(conn)
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def owner(cur, chat_id):
        cur.execute("""SELECT id FROM public.users WHERE telegram_chat_id=%s
            AND (%s=false OR active_bot_id=%s)""", (chat_id, require_joined(), bot_id()))
        if not cur.fetchone():
            raise HTTPException(403, "Avval shu botda /start tugmasini bosing.")

    def prayer_times(region, today):
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT times FROM public.qadam_prayer_cache WHERE region=%s AND day=%s", (region, today))
                cached = cur.fetchone()
        if cached:
            return cached[0]
        with _fetch_lock:
            if _retry_after.get(region, 0) > datetime.now().timestamp():
                raise HTTPException(503, "Namoz vaqtlarini hozir olib bo‘lmadi. Birozdan keyin yangilang.")
            try:
                response = requests.get(SOURCE, params={"region": region, "lang": "lotin", "period": "today", "format": "json"}, timeout=(3, 8))
                response.raise_for_status()
                times = validate_times(response.json(), region, today)
            except (requests.RequestException, ValueError, TypeError, AttributeError):
                _retry_after[region] = datetime.now().timestamp() + 120
                raise HTTPException(503, "Namoz vaqtlarini hozir olib bo‘lmadi. Birozdan keyin yangilang.") from None
            with connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""INSERT INTO public.qadam_prayer_cache(region,day,times) VALUES(%s,%s,%s)
                        ON CONFLICT(region,day) DO UPDATE SET times=EXCLUDED.times""", (region, today, Json(times)))
            return times

    def seed(chat_id, at):
        today = at.date()
        with connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM public.qadam_habit_settings WHERE scope=%s AND chat_id=%s", (scope(), chat_id))
                settings = cur.fetchone()
        times, warning = {}, None
        if settings and settings["enabled"]:
            try:
                times = prayer_times(settings["region"], today)
            except HTTPException as exc:
                warning = exc.detail
        with connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Settings and habit edits serialize with today's materialization.
                cur.execute("SELECT * FROM public.qadam_habit_settings WHERE scope=%s AND chat_id=%s FOR UPDATE", (scope(), chat_id))
                fresh = cur.fetchone()
                if fresh and settings and fresh["region"] == settings["region"] and fresh["enabled"] and times:
                    for key, title in PRAYERS.items():
                        due = datetime.combine(today, time.fromisoformat(times[key]), ZONE)
                        cur.execute("""INSERT INTO public.qadam_habit_days(scope,chat_id,kind,ref,day,title,due_at,reminder_state)
                            VALUES(%s,%s,'prayer',%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                            (scope(), chat_id, key, today, title, due, "skipped" if due < fresh["enabled_at"] else None))
                cur.execute("SELECT * FROM public.qadam_habits WHERE scope=%s AND chat_id=%s AND active FOR UPDATE", (scope(), chat_id))
                for habit in cur.fetchall():
                    # Backfill unmarked history after downtime; only today may send.
                    created_day = habit["created_at"].astimezone(ZONE).date()
                    cur.execute("""INSERT INTO public.qadam_habit_days(scope,chat_id,kind,ref,day,title,due_at,reminder_state)
                        SELECT %s,%s,'habit',%s,d::date,%s,
                            (d::date + %s::time) AT TIME ZONE 'Asia/Tashkent',
                            CASE WHEN d::date<%s OR (d::date + %s::time) AT TIME ZONE 'Asia/Tashkent'<%s
                                THEN 'skipped' ELSE NULL END
                        FROM generate_series(%s::date,%s::date,interval '1 day') AS d
                        ON CONFLICT DO NOTHING""",
                        (scope(), chat_id, str(habit["id"]), habit["title"], habit["reminder_time"], today,
                         habit["reminder_time"], habit["created_at"], created_day, today))
        return settings, times, warning

    @router.get("/miniapp/habits")
    def dashboard(chat_id: int = Depends(auth)):
        at = now_local()
        with connection() as conn:
            with conn.cursor() as cur:
                owner(cur, chat_id)
        settings, times, warning = seed(chat_id, at)
        with connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM public.qadam_habits WHERE scope=%s AND chat_id=%s AND active ORDER BY id", (scope(), chat_id))
                habits = cur.fetchall()
                cur.execute("SELECT * FROM public.qadam_habit_days WHERE scope=%s AND chat_id=%s ORDER BY day,id", (scope(), chat_id))
                history = cur.fetchall()
        today_entries = []
        for row in history:
            if row["day"] == at.date():
                today_entries.append({"id": row["id"], "kind": row["kind"], "ref": row["ref"], "title": row["title"],
                    "status": row["status"], "time": row["due_at"].astimezone(ZONE).strftime("%H:%M"),
                    "available": row["kind"] == "habit" or row["due_at"] <= at})
        habit_list = []
        for habit in habits:
            rows = [r for r in history if r["kind"] == "habit" and r["ref"] == str(habit["id"])]
            habit_list.append({"id": habit["id"], "title": habit["title"], "kind": habit["kind"],
                "reminder_time": str(habit["reminder_time"])[:5], "streak": streak_stats(rows, at.date())})
        qaza = []
        for key, title in PRAYERS.items():
            debts = [r for r in history if r["kind"] == "prayer" and r["ref"] == key and r["status"] == "missed"]
            qaza.append({"key": key, "title": title, "count": len(debts), "oldest_id": debts[0]["id"] if debts else None})
        calendar = []
        for delta in range(27, -1, -1):
            day = at.date() - timedelta(days=delta)
            rows = [r for r in history if r["day"] == day and r["kind"] == "habit"]
            calendar.append({"date": day.isoformat(), "done": sum(r["status"] == "done" for r in rows), "total": len(rows)})
        stats = {}
        for label, start in (("daily", at.date()), ("weekly", at.date() - timedelta(days=at.weekday())), ("monthly", at.date().replace(day=1))):
            rows = [r for r in history if start <= r["day"] <= at.date()]
            stats[label] = {kind: {"done": sum(r["status"] == "done" for r in rows if r["kind"] == kind),
                                  "total": sum(r["kind"] == kind for r in rows)} for kind in ("habit", "prayer")}
        return {"date": at.date().isoformat(), "now": at.strftime("%H:%M"), "regions": REGIONS,
            "settings": {"region": settings["region"], "enabled": settings["enabled"]} if settings else None,
            "times": times, "warning": warning, "entries": today_entries, "habits": habit_list,
            "qaza": qaza, "calendar": calendar, "stats": stats,
            "qaza_completed": sum(r["status"] == "qaza_done" for r in history)}

    @router.put("/miniapp/habits/prayer-settings")
    def save_settings(payload: PrayerSettings, chat_id: int = Depends(auth)):
        if payload.region not in REGIONS:
            raise HTTPException(422, "Hududni ro‘yxatdan tanlang.")
        at = now_local()
        with connection() as conn:
            with conn.cursor() as cur:
                owner(cur, chat_id)
        # Fetch before saving; an unavailable provider must not enable a false schedule.
        if payload.enabled:
            prayer_times(payload.region, at.date())
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO public.qadam_habit_settings(scope,chat_id,region,enabled,enabled_at)
                    VALUES(%s,%s,%s,%s,%s) ON CONFLICT(scope,chat_id) DO UPDATE SET
                    enabled_at=CASE WHEN qadam_habit_settings.region<>EXCLUDED.region OR
                        (NOT qadam_habit_settings.enabled AND EXCLUDED.enabled) THEN EXCLUDED.enabled_at
                        ELSE qadam_habit_settings.enabled_at END,
                    region=EXCLUDED.region, enabled=EXCLUDED.enabled""", (scope(), chat_id, payload.region, payload.enabled, at))
                # Retain marked and claimed occurrences so changing region cannot resend them.
                cur.execute("""DELETE FROM public.qadam_habit_days WHERE scope=%s AND chat_id=%s AND kind='prayer'
                    AND day=%s AND status='pending' AND (reminder_state IS NULL OR reminder_state='skipped')""", (scope(), chat_id, at.date()))
        return {"ok": True}

    @router.post("/miniapp/habits")
    def create_habit(payload: HabitInput, chat_id: int = Depends(auth)):
        title = payload.title.strip()
        if not title:
            raise HTTPException(422, "Odat nomini yozing.")
        with connection() as conn:
            with conn.cursor() as cur:
                owner(cur, chat_id)
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (chat_id,))
                cur.execute("SELECT count(*) FROM public.qadam_habits WHERE scope=%s AND chat_id=%s AND active", (scope(), chat_id))
                if cur.fetchone()[0] >= 30:
                    raise HTTPException(422, "Hozircha 30 tagacha faol odat qo‘shish mumkin.")
                cur.execute("""INSERT INTO public.qadam_habits(scope,chat_id,title,kind,reminder_time)
                    VALUES(%s,%s,%s,%s,%s::time) RETURNING id""", (scope(), chat_id, title, payload.kind, payload.reminder_time))
                result = cur.fetchone()[0]
        return {"ok": True, "id": result}

    @router.put("/miniapp/habits/{habit_id}")
    def edit_habit(habit_id: int, payload: HabitInput, chat_id: int = Depends(auth)):
        if not payload.title.strip():
            raise HTTPException(422, "Odat nomini yozing.")
        with connection() as conn:
            with conn.cursor() as cur:
                owner(cur, chat_id)
                cur.execute("""UPDATE public.qadam_habits SET title=%s,kind=%s,reminder_time=%s::time
                    WHERE id=%s AND scope=%s AND chat_id=%s AND active RETURNING id""",
                    (payload.title.strip(), payload.kind, payload.reminder_time, habit_id, scope(), chat_id))
                if not cur.fetchone():
                    raise HTTPException(404, "Odat topilmadi.")
                due = datetime.combine(now_local().date(), time.fromisoformat(payload.reminder_time), ZONE)
                cur.execute("""UPDATE public.qadam_habit_days SET title=%s,due_at=%s,
                    reminder_state=CASE WHEN reminder_state='skipped' AND %s>now() THEN NULL ELSE reminder_state END
                    WHERE scope=%s AND chat_id=%s AND kind='habit' AND ref=%s AND day=%s
                    AND status='pending' AND (reminder_state IS NULL OR reminder_state='skipped')""",
                    (payload.title.strip(), due, due, scope(), chat_id, str(habit_id), due.date()))
        return {"ok": True}

    @router.delete("/miniapp/habits/{habit_id}")
    def archive_habit(habit_id: int, chat_id: int = Depends(auth)):
        with connection() as conn:
            with conn.cursor() as cur:
                owner(cur, chat_id)
                cur.execute("UPDATE public.qadam_habits SET active=false WHERE id=%s AND scope=%s AND chat_id=%s RETURNING id", (habit_id, scope(), chat_id))
                if not cur.fetchone():
                    raise HTTPException(404, "Odat topilmadi.")
        return {"ok": True}

    def mark(chat_id, entry_id, status):
        at = now_local()
        with connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                owner(cur, chat_id)
                cur.execute("SELECT * FROM public.qadam_habit_days WHERE id=%s AND scope=%s AND chat_id=%s FOR UPDATE", (entry_id, scope(), chat_id))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(404, "Yozuv topilmadi.")
                if status == row["status"]:
                    return {"ok": True, "unchanged": True}
                if status == "qaza_done":
                    if row["kind"] != "prayer" or row["status"] != "missed":
                        raise HTTPException(409, "Bu namoz qazo ro‘yxatida yo‘q.")
                elif row["status"] == "qaza_done" or row["day"] > at.date() or (row["kind"] == "habit" and row["day"] != at.date()):
                    raise HTTPException(409, "Bu kun yakunlangan. Qazo bo‘limidan foydalaning.")
                elif row["kind"] == "prayer" and row["due_at"] > at:
                    raise HTTPException(409, "Bu namoz vaqti hali kirmagan.")
                cur.execute("UPDATE public.qadam_habit_days SET status=%s,marked_at=now() WHERE id=%s", (status, entry_id))
                cur.execute("UPDATE public.users SET last_active_date=%s WHERE telegram_chat_id=%s", (at.date(), chat_id))
        return {"ok": True}

    @router.put("/miniapp/habit-entries/{entry_id}")
    def mark_endpoint(entry_id: int, payload: MarkInput, chat_id: int = Depends(auth)):
        raise HTTPException(403, "Holatni bot chatidagi tugmalar orqali belgilang.")

    def callback(chat_id, data, callback_id):
        try:
            _, raw_id, status = data.split("|")
            if status not in ("done", "missed", "qaza_done"):
                raise ValueError()
            result = mark(chat_id, int(raw_id), status)
            text = "Avval belgilangan" if result.get("unchanged") else ("Bajarildi ✅" if status == "done" else "Belgilandi")
        except (ValueError, HTTPException) as exc:
            text = exc.detail if isinstance(exc, HTTPException) else "So‘rov noto‘g‘ri."
        answer(callback_id, text)
        return {"ok": True}

    def chat_summary(chat_id, section):
        data = dashboard(chat_id)
        lines, buttons = [], []
        if section == "qaza":
            lines.append("<b>🕌 Qazo namozlar hisobi</b>")
            for prayer in data["qaza"]:
                lines.append(f"{prayer['title']}: <b>{prayer['count']} ta</b>")
                if prayer["oldest_id"]:
                    buttons.append([{"text": f"✅ {prayer['title']} — 1 ta qazo o‘qildi",
                        "callback_data": f"habit|{prayer['oldest_id']}|qaza_done"}])
            lines.append("Qazo o‘qilgach belgilang. Har bir tugma bitta yozuvga tegishli; yangi hisob uchun /qazo yuboring.")
        else:
            prayer = section == "prayers"
            lines.append("<b>🕌 Bugungi namozlar</b>" if prayer else "<b>👣 Bugungi odatlar</b>")
            active_refs = {str(h["id"]) for h in data["habits"]}
            entries = [e for e in data["entries"] if e["kind"] == ("prayer" if prayer else "habit")
                       and (prayer or e["ref"] in active_refs)]
            for i, entry in enumerate(entries, 1):
                if len(lines) == 16:
                    send(chat_id, "\n\n".join(lines), {"inline_keyboard": buttons}, parse_mode="HTML")
                    lines, buttons = [lines[0]], []
                state = {"pending": "⏳", "done": "✅", "missed": "❌", "qaza_done": "✅ Qazosi o‘qildi"}[entry["status"]]
                lines.append(f"{i}. {html.escape(entry['title'])} · {entry['time']} · {state}")
                if entry["available"] and entry["status"] != "qaza_done":
                    buttons.append([
                        {"text": f"✅ {i} " + ("O‘qidim" if prayer else "Bajarildi"), "callback_data": f"habit|{entry['id']}|done"},
                        {"text": f"❌ {i} " + ("O‘qimadim" if prayer else "Bajarilmadi"), "callback_data": f"habit|{entry['id']}|missed"}])
            if not entries:
                lines.append("Mini App’da hududingizni tanlang yoki odat qo‘shing.")
            if data["warning"]:
                lines.append(html.escape(data["warning"]))
        send(chat_id, "\n\n".join(lines), {"inline_keyboard": buttons}, parse_mode="HTML")
        return {"ok": True}

    def reminders():
        at = now_local()
        results = []
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT DISTINCT u.telegram_chat_id FROM public.users u WHERE u.state<>'blocked'
                    AND (%s=false OR u.active_bot_id=%s) AND (
                    EXISTS(SELECT 1 FROM public.qadam_habit_settings s WHERE s.scope=%s AND s.chat_id=u.telegram_chat_id AND s.enabled)
                    OR EXISTS(SELECT 1 FROM public.qadam_habits h WHERE h.scope=%s AND h.chat_id=u.telegram_chat_id AND h.active))""",
                    (require_joined(), bot_id(), scope(), scope()))
                chats = [r[0] for r in cur.fetchall()]
        for chat_id in chats:
            try:
                _, _, warning = seed(chat_id, at)
                if warning:
                    results.append({"chat_id": chat_id, "sent": False, "reason": "prayer_source_unavailable"})
                # One claim per occurrence across all scheduler/endpoint workers.
                with connection() as conn:
                    with conn.cursor(cursor_factory=RealDictCursor) as cur:
                        cur.execute("""UPDATE public.qadam_habit_days d SET reminder_state='started'
                            WHERE d.scope=%s AND d.chat_id=%s AND d.day=%s AND d.status='pending'
                            AND d.reminder_state IS NULL AND d.due_at<=%s AND (
                              (d.kind='prayer' AND EXISTS(SELECT 1 FROM public.qadam_habit_settings s
                                WHERE s.scope=d.scope AND s.chat_id=d.chat_id AND s.enabled)) OR
                              (d.kind='habit' AND EXISTS(SELECT 1 FROM public.qadam_habits h
                                WHERE h.id::text=d.ref AND h.scope=d.scope AND h.chat_id=d.chat_id AND h.active)))
                            RETURNING d.*""", (scope(), chat_id, at.date(), at))
                        due_rows = cur.fetchall()
                    conn.commit()
                for row in due_rows:
                    prayer = row["kind"] == "prayer"
                    title = html.escape(row["title"])
                    day_label = row["day"].strftime("%d.%m.%Y")
                    text = (f"<b>🕌 {title} vaqti kirdi.</b>\n{day_label}\n\nO‘qib bo‘lgach, quyida belgilashingiz mumkin."
                            if prayer else f"<b>⏰ {title}</b>\n\nBugungi kichik qadamingiz uchun vaqt ajrating. 👣")
                    keyboard = {"inline_keyboard": [[
                        {"text": "✅ O‘qidim" if prayer else "✅ Bajarildi", "callback_data": f"habit|{row['id']}|done"},
                        {"text": "❌ O‘qimadim" if prayer else "❌ Bajarilmadi", "callback_data": f"habit|{row['id']}|missed"}]]}
                    state = "sent"
                    try:
                        send(chat_id, text, keyboard, parse_mode="HTML")
                    except Exception as exc:
                        state = "failed"
                        detail = str(getattr(exc, "detail", "")).lower()
                        if "403" in detail or "bot was blocked" in detail:
                            with connection() as conn:
                                with conn.cursor() as cur:
                                    cur.execute("UPDATE public.users SET state='blocked' WHERE telegram_chat_id=%s", (chat_id,))
                        LOG.warning("Habit send failed (%s)", type(exc).__name__)
                    with connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute("UPDATE public.qadam_habit_days SET reminder_state=%s WHERE id=%s", (state, row["id"]))
                    results.append({"id": row["id"], "sent": state == "sent"})
            except Exception as exc:
                LOG.warning("Habit scheduling failed (%s)", type(exc).__name__)
                results.append({"chat_id": chat_id, "sent": False})
        return {"results": results}

    return reminders, callback, chat_summary
