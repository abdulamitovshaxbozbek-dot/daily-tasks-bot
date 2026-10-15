"""Plan builder ("Rejalar"): split a big goal into daily steps.

The conversation (questions, preview, coach chat) lives in the Mini App.
The bot chat only receives short reminders with buttons.
Integrates through install() like habits.py, without changing existing handlers.
"""
import html
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import List, Literal, Optional
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from psycopg2.extras import RealDictCursor, Json

import ai
import plan_logic as L

ZONE = ZoneInfo("Asia/Tashkent")
MAX_ACTIVE_PLANS = 5
AI_DAILY_LIMIT = 30
CHAT_DAILY_LIMIT = 15
SLOT_TIMES = {"morning": "07:30", "noon": "13:00", "evening": "19:00", "night": "21:30"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS public.qadam_plans (
 id BIGSERIAL PRIMARY KEY, scope TEXT NOT NULL, chat_id BIGINT NOT NULL,
 title TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('volume','stages','ramp')),
 unit TEXT, total INTEGER, mode TEXT NOT NULL DEFAULT 'tasks' CHECK(mode IN ('tasks','habit')),
 habit_id BIGINT, reminder_time TIME NOT NULL DEFAULT '19:00',
 weekdays INTEGER[] NOT NULL, daily_minutes INTEGER, answers JSONB NOT NULL DEFAULT '{}'::jsonb,
 start_day DATE NOT NULL, end_day DATE NOT NULL,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','done','archived')),
 last_milestone INTEGER NOT NULL DEFAULT 0, unanswered INTEGER NOT NULL DEFAULT 0,
 request_id TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS qadam_plans_owner ON public.qadam_plans(scope, chat_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS qadam_plans_request ON public.qadam_plans(scope, chat_id, request_id)
 WHERE request_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS public.qadam_plan_steps (
 id BIGSERIAL PRIMARY KEY, plan_id BIGINT NOT NULL REFERENCES public.qadam_plans(id) ON DELETE CASCADE,
 day DATE NOT NULL, position INTEGER NOT NULL, title TEXT NOT NULL, units INTEGER, minutes INTEGER,
 is_buffer BOOLEAN NOT NULL DEFAULT FALSE,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','done','skipped')),
 carried BOOLEAN NOT NULL DEFAULT FALSE, task_id TEXT, done_at TIMESTAMPTZ
);
ALTER TABLE public.qadam_plan_steps ADD COLUMN IF NOT EXISTS unit_start INTEGER;
ALTER TABLE public.qadam_plan_steps ADD COLUMN IF NOT EXISTS unit_end INTEGER;
CREATE INDEX IF NOT EXISTS qadam_plan_steps_plan ON public.qadam_plan_steps(plan_id, day);
CREATE INDEX IF NOT EXISTS qadam_plan_steps_day ON public.qadam_plan_steps(day) WHERE status='pending';
CREATE TABLE IF NOT EXISTS public.qadam_plan_chat (
 id BIGSERIAL PRIMARY KEY, plan_id BIGINT NOT NULL REFERENCES public.qadam_plans(id) ON DELETE CASCADE,
 role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS qadam_plan_chat_plan ON public.qadam_plan_chat(plan_id, id);
CREATE TABLE IF NOT EXISTS public.qadam_ai_usage (
 scope TEXT NOT NULL, chat_id BIGINT NOT NULL, day DATE NOT NULL, kind TEXT NOT NULL DEFAULT 'all',
 count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(scope, chat_id, day, kind)
);
"""
_ready = False
_schema_lock = threading.Lock()


def initialize(conn):
    global _ready
    with _schema_lock:
        if not _ready:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(716240923)")
                cur.execute(SCHEMA)
            conn.commit()
            _ready = True


def now_local():
    return datetime.now(ZONE)


class DraftIn(BaseModel):
    goal: str = Field(min_length=3, max_length=300)


class PreviewIn(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    kind: Literal["volume", "stages", "ramp"]
    total: Optional[int] = Field(default=None, ge=2, le=L.MAX_TOTAL)
    unit: Optional[str] = Field(default=None, max_length=20)
    answers: dict = Field(default_factory=dict)


class StepIn(BaseModel):
    day: date
    title: str = Field(min_length=1, max_length=120)
    units: Optional[int] = Field(default=None, ge=0, le=L.MAX_TOTAL)
    minutes: Optional[int] = Field(default=None, ge=0, le=1440)
    is_buffer: bool = False


class CreateIn(PreviewIn):
    mode: Literal["tasks", "habit"] = "tasks"
    request_id: Optional[str] = Field(default=None, min_length=16, max_length=64)
    steps: List[StepIn] = Field(min_length=1, max_length=130)


class ReplanIn(BaseModel):
    extend_days: int = Field(default=0, ge=0, le=60)


class StatusIn(BaseModel):
    action: Literal["pause", "resume", "archive"]


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=500)


def clean_answers(kind, raw):
    """Validate the questionnaire answers; raise 422 with a readable message."""
    def number(key, low, high, default=None, cast=int):
        value = raw.get(key, default)
        if value in (None, ""):
            return default
        try:
            value = cast(value)
        except (TypeError, ValueError):
            raise HTTPException(422, "Javob noto‘g‘ri: " + key)
        if not low <= value <= high:
            raise HTTPException(422, "Javob oralig‘i noto‘g‘ri: " + key)
        return value

    days = number("days", 1, L.MAX_DAYS, 30)
    slot = raw.get("slot") or "evening"
    if slot not in SLOT_TIMES:
        raise HTTPException(422, "Vaqt noto‘g‘ri.")
    weekdays = raw.get("weekdays") or list(range(7))
    if not isinstance(weekdays, list) or not weekdays or any(
            not isinstance(d, int) or d not in range(7) for d in weekdays):
        raise HTTPException(422, "Kamida bitta hafta kunini tanlang.")
    out = {"days": days, "slot": slot, "weekdays": sorted(set(weekdays))}
    if kind == "ramp":
        out["start_minutes"] = number("start_minutes", 5, 300, 10)
        out["target_minutes"] = number("target_minutes", 5, 600, max(out["start_minutes"], 30))
        out["minutes"] = out["target_minutes"]
    else:
        out["minutes"] = number("minutes", 5, 600, 30)
    if kind == "volume":
        out["mpu"] = number("mpu", 0.1, 600, None, cast=float)
    if kind == "stages":
        out["level"] = str(raw.get("level") or "boshlovchi")[:20]
        out["blocker"] = str(raw.get("blocker") or "")[:200]
    return out


def mpu_options(unit):
    if unit == "bet":
        return [1, 2, 3, 5]
    if unit in ("dars", "video", "mavzu", "bob"):
        return [20, 30, 45, 60]
    if unit in ("test", "savol", "so‘z"):
        return [1, 2, 3, 5]
    return [5, 10, 20, 30]


def question_sets(unit):
    days = {"id": "days", "text": "Necha kunda tugatmoqchisiz?", "options": [7, 14, 21, 30, 45, 60, 90],
            "suffix": "kun", "min": 1, "max": L.MAX_DAYS, "default": 30}
    minutes = {"id": "minutes", "text": "Kuniga qancha vaqt ajrata olasiz?", "options": [15, 30, 45, 60, 90, 120],
               "suffix": "daqiqa", "min": 5, "max": 600, "default": 30}
    slot = {"id": "slot", "text": "Qaysi vaqtda qulay?", "type": "labeled", "default": "evening",
            "options": [{"value": "morning", "label": "🌅 Ertalab"}, {"value": "noon", "label": "☀️ Kunduzi"},
                        {"value": "evening", "label": "🌆 Kechqurun"}, {"value": "night", "label": "🌙 Tunda"}]}
    weekdays = {"id": "weekdays", "text": "Qaysi kunlari shug‘ullanasiz?", "type": "labeled",
                "default": list(range(7)),
                "options": [{"value": list(range(7)), "label": "Har kuni"},
                            {"value": [0, 1, 2, 3, 4, 5], "label": "Yakshanbasiz"},
                            {"value": [0, 1, 2, 3, 4], "label": "Du–Ju"},
                            {"value": [5, 6], "label": "Dam olish kunlari"}]}
    return {
        "volume": [
            {"id": "total", "text": "Jami qancha?", "type": "number", "core": True, "min": 2, "max": L.MAX_TOTAL},
            {"id": "unit", "text": "Nimani sanaymiz? (bet, dars, so‘z...)", "type": "text", "core": True},
            days,
            {"id": "mpu", "text": "Bitta %s uchun taxminan necha daqiqa ketadi?" % (unit or "birlik"),
             "options": mpu_options(unit), "suffix": "daqiqa", "min": 0.5, "max": 600,
             "default": L.DEFAULT_MPU.get(unit, 10), "step": 0.5},
            minutes, slot, weekdays],
        "stages": [
            days, minutes,
            {"id": "level", "text": "Hozir qaysi darajadasiz?", "type": "labeled", "default": "boshlovchi",
             "options": [{"value": "boshlovchi", "label": "Yangi boshlayman"},
                         {"value": "orta", "label": "Biroz bilaman"},
                         {"value": "yuqori", "label": "Yaxshi bilaman"}]},
            {"id": "blocker", "text": "Eng qiyin tomoni nima? (ixtiyoriy)", "type": "text", "optional": True},
            slot, weekdays],
        "ramp": [
            days,
            {"id": "start_minutes", "text": "Hozir kuniga necha daqiqa qila olasiz?", "options": [5, 10, 15, 20],
             "suffix": "daqiqa", "min": 5, "max": 300, "default": 10},
            {"id": "target_minutes", "text": "Maqsad: kuniga necha daqiqa?", "options": [20, 30, 45, 60],
             "suffix": "daqiqa", "min": 5, "max": 600, "default": 30},
            slot, weekdays],
    }


def install(router, get_connection, auth, bot_id, require_joined, send, answer, activity,
            get_today, get_user, transition_task, sync_task_result, refresh_checklist,
            claim, plans_url):
    """Wire the plan builder into the existing app. Returns the bot-facing handlers."""

    def scope():
        return str(bot_id() or "default")

    @contextmanager
    def connection():
        with get_connection() as conn:
            from habits import initialize as initialize_habits
            initialize_habits(conn)
            initialize(conn)
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def owner(cur, chat_id):
        cur.execute("""SELECT id, state FROM public.users WHERE telegram_chat_id=%s
            AND (%s=false OR active_bot_id=%s)""", (chat_id, require_joined(), bot_id()))
        row = cur.fetchone()
        if not row or (row[1] if not isinstance(row, dict) else row["state"]) == "blocked":
            raise HTTPException(403, "Avval shu botda /start tugmasini bosing.")
        return row[0] if not isinstance(row, dict) else row["id"]

    def ai_failure(error):
        return JSONResponse(status_code=503, content=ai.error_payload(error))

    def spend_ai(chat_id, kind="all", limit=AI_DAILY_LIMIT):
        today = get_today()
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO public.qadam_ai_usage(scope,chat_id,day,kind,count) VALUES(%s,%s,%s,%s,1)
                    ON CONFLICT(scope,chat_id,day,kind) DO UPDATE SET count=qadam_ai_usage.count+1
                    RETURNING count""", (scope(), chat_id, today, kind))
                used = cur.fetchone()[0]
        if used > limit:
            raise HTTPException(429, "Bugungi AI so‘rovlar limiti tugadi. Ertaga davom eting.")

    # ---------------------------------------------------------------- state sync
    def sync(cur, chat_id=None):
        """Derive step status from the linked task / habit day. Returns plan ids with new progress."""
        progressed = set()
        cur = cur.connection.cursor()
        cur.execute("""UPDATE public.qadam_plan_steps s SET
                status=CASE WHEN t.status='completed' THEN 'done' ELSE 'skipped' END,
                done_at=CASE WHEN t.status='completed' THEN now() ELSE NULL END
            FROM public.tasks t, public.qadam_plans p
            WHERE p.id=s.plan_id AND p.scope=%s AND s.task_id=t.id::text AND s.status='pending'
              AND t.status IN ('completed','failed') AND (%s::bigint IS NULL OR p.chat_id=%s)
            RETURNING s.plan_id, s.status""", (scope(), chat_id, chat_id))
        progressed.update(r[0] for r in cur.fetchall() if r[1] == "done")
        # Deleting the task in the Mini App means "skip this step".
        cur.execute("""UPDATE public.qadam_plan_steps s SET status='skipped'
            FROM public.qadam_plans p
            WHERE p.id=s.plan_id AND p.scope=%s AND p.mode='tasks' AND s.task_id IS NOT NULL
              AND s.status='pending' AND (%s::bigint IS NULL OR p.chat_id=%s)
              AND NOT EXISTS (SELECT 1 FROM public.tasks t WHERE t.id::text=s.task_id)""",
                    (scope(), chat_id, chat_id))
        cur.execute("""UPDATE public.qadam_plan_steps s SET
                status=CASE d.status WHEN 'done' THEN 'done' ELSE 'skipped' END,
                done_at=CASE WHEN d.status='done' THEN now() ELSE NULL END
            FROM public.qadam_plans p, public.qadam_habit_days d
            WHERE p.id=s.plan_id AND p.scope=%s AND p.mode='habit' AND p.habit_id IS NOT NULL
              AND d.scope=p.scope AND d.chat_id=p.chat_id AND d.kind='habit' AND d.ref=p.habit_id::text
              AND d.day=s.day AND s.status='pending' AND d.status IN ('done','missed')
              AND (%s::bigint IS NULL OR p.chat_id=%s)
            RETURNING s.plan_id, s.status""", (scope(), chat_id, chat_id))
        progressed.update(r[0] for r in cur.fetchall() if r[1] == "done")
        if progressed:
            cur.execute("UPDATE public.qadam_plans SET unanswered=0 WHERE id=ANY(%s)", (list(progressed),))
        return progressed

    def load_plans(cur, chat_id, statuses=("active", "paused", "done")):
        cur.execute("""SELECT * FROM public.qadam_plans WHERE scope=%s AND chat_id=%s AND status=ANY(%s)
            ORDER BY (status='active') DESC, id DESC""", (scope(), chat_id, list(statuses)))
        plans = [dict(r) for r in cur.fetchall()]
        for plan in plans:
            cur.execute("""SELECT id, day, position, title, units, minutes, is_buffer, status, carried, task_id, unit_start, unit_end
                FROM public.qadam_plan_steps WHERE plan_id=%s ORDER BY day, id""", (plan["id"],))
            plan["steps"] = [dict(r) for r in cur.fetchall()]
        return plans

    def load_plan(cur, chat_id, plan_id, lock=False):
        cur.execute("SELECT * FROM public.qadam_plans WHERE id=%s AND scope=%s AND chat_id=%s" +
                    (" FOR UPDATE" if lock else ""), (plan_id, scope(), chat_id))
        plan = cur.fetchone()
        if not plan:
            raise HTTPException(404, "Reja topilmadi.")
        plan = dict(plan)
        cur.execute("""SELECT id, day, position, title, units, minutes, is_buffer, status, carried, task_id, unit_start, unit_end
            FROM public.qadam_plan_steps WHERE plan_id=%s ORDER BY day, id""", (plan_id,))
        plan["steps"] = [dict(r) for r in cur.fetchall()]
        return plan

    def serialize(plan, today):
        steps = plan["steps"]
        percent = L.progress_percent(plan["kind"], plan["total"], steps)
        today_step = L.today_step(steps, today)
        buffer_today = any(s["day"] == today and s["is_buffer"] for s in steps)
        real = [s for s in steps if not s["is_buffer"] and not s.get("carried")]
        return {
            "id": plan["id"], "title": plan["title"], "kind": plan["kind"], "unit": plan["unit"],
            "total": plan["total"], "mode": plan["mode"], "status": plan["status"],
            "start_day": plan["start_day"].isoformat(), "end_day": plan["end_day"].isoformat(),
            "weekdays": plan["weekdays"], "daily_minutes": plan["daily_minutes"],
            "reminder_time": str(plan["reminder_time"])[:5],
            "progress": percent, "streak": L.streak(steps, today),
            "done_steps": sum(s["status"] == "done" for s in real), "total_steps": len(real),
            "missed": len(L.unanswered_past(steps, today)),
            "today_step": ({"id": today_step["id"], "title": today_step["title"], "status": today_step["status"],
                            "minutes": today_step["minutes"]} if today_step else None),
            "buffer_today": buffer_today,
            "steps": [{"id": s["id"], "day": s["day"].isoformat(), "title": s["title"], "status": s["status"],
                       "units": s["units"], "minutes": s["minutes"], "is_buffer": s["is_buffer"]} for s in steps],
        }

    # ---------------------------------------------------------------- planning
    def make_steps(payload, answers, start):
        days_list = L.schedule_days(start, answers["days"], answers["weekdays"])
        if not days_list:
            raise HTTPException(422, "Tanlangan kunlar shu muddatga tushmayapti. Muddatni uzaytiring.")
        kind, title = payload.kind, payload.title.strip()
        study = L.study_day_count(start, answers["days"], answers["weekdays"])
        feas, stages = None, None
        if kind == "volume":
            if not payload.total:
                raise HTTPException(422, "Jami miqdorni kiriting.")
            unit = (payload.unit or "ta").strip()
            mpu = answers.get("mpu") or L.DEFAULT_MPU.get(unit, 10)
            steps = L.build_volume(title, payload.total, unit, days_list, mpu=mpu)
            feas = L.feasibility("volume", answers["minutes"], days_list, start, answers["weekdays"],
                                 total=payload.total, mpu=mpu)
        elif kind == "ramp":
            steps = L.build_ramp(title, days_list, answers["start_minutes"], answers["target_minutes"])
            feas = L.feasibility("ramp", answers["minutes"], days_list, start, answers["weekdays"],
                                 target_minutes=answers["target_minutes"])
        else:
            stages = ai_stages(title, answers, min(study, 20))
            steps = L.spread_stages(stages, days_list)
            feas = L.feasibility("stages", answers["minutes"], days_list, start, answers["weekdays"], stages=stages)
        real = [s for s in steps if not s["is_buffer"] and not s.get("carried")]
        summary = {"start_day": days_list[0].isoformat(), "end_day": steps[-1]["day"].isoformat(),
                   "active_days": len(days_list), "study_days": len(real),
                   "buffer_days": len(steps) - len(real),
                   "per_day": (max(s["units"] for s in real) if kind == "volume" else
                               max((s["minutes"] or 0) for s in real) if real else None)}
        return steps, feas, summary

    def ai_stages(title, answers, count):
        prompt = (
            "Siz reja tuzuvchi yordamchisiz. Faqat o‘zbek tilida (lotin yozuvida) yozing.\n"
            "Maqsad: %s\nMuddat: %d kun, kuniga ~%d daqiqa vaqt bor.\nFoydalanuvchi darajasi: %s\n"
            "Eng qiyin tomoni: %s\n\n"
            "Maqsadga erishish uchun aynan %d ta ketma-ket, aniq, bajariladigan bosqich tuzing. "
            "Har bir bosqich qisqa (60 belgigacha) bo‘lsin va natijasi aniq bo‘lsin. "
            "Har bosqich uchun taxminiy vaqtni daqiqada yozing (10–480).\n"
            "FAQAT JSON qaytaring: {\"stages\":[{\"title\":\"...\",\"minutes\":45}]}"
        ) % (title, answers["days"], answers["minutes"], answers.get("level", "boshlovchi"),
             answers.get("blocker") or "ko‘rsatilmagan", count)
        text = ai.chat([{"role": "user", "content": prompt}], where="plan stages", max_tokens=3000)
        data = ai.extract_json(text) or {}
        out = []
        for item in (data.get("stages") or [])[:30]:
            if not isinstance(item, dict) or not str(item.get("title") or "").strip():
                continue
            try:
                minutes = int(item.get("minutes") or 45)
            except (TypeError, ValueError):
                minutes = 45
            out.append({"title": str(item["title"]).strip()[:120], "minutes": min(480, max(10, minutes))})
        if len(out) < 2:
            raise HTTPException(502, "AI javobi tushunarsiz bo‘ldi. Qayta urinib ko‘ring.")
        return out

    def plan_start():
        today = get_today()
        return today if now_local().hour < 20 else today + timedelta(days=1)

    # ---------------------------------------------------------------- tasks
    def materialize(only_chat=None):
        """Create today's task for every active task-mode plan step. Returns created count."""
        today = get_today()
        created = 0
        with connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""SELECT s.id AS step_id, s.title, p.chat_id, p.reminder_time
                    FROM public.qadam_plan_steps s JOIN public.qadam_plans p ON p.id=s.plan_id
                    WHERE p.scope=%s AND p.status='active' AND p.mode='tasks' AND s.status='pending'
                      AND s.task_id IS NULL AND s.is_buffer=false AND s.day=%s
                      AND (%s::bigint IS NULL OR p.chat_id=%s) ORDER BY p.chat_id, s.id""",
                            (scope(), today, only_chat, only_chat))
                due = cur.fetchall()
        touched = {}
        for row in due:
            with connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("""SELECT id, state, live_checklist_message_id, live_checklist_date
                        FROM public.users WHERE telegram_chat_id=%s AND (%s=false OR active_bot_id=%s)
                        FOR UPDATE""", (row["chat_id"], require_joined(), bot_id()))
                    user = cur.fetchone()
                    if not user or user["state"] == "blocked":
                        continue
                    cur.execute("SELECT task_id FROM public.qadam_plan_steps WHERE id=%s FOR UPDATE", (row["step_id"],))
                    if cur.fetchone()["task_id"] is not None:
                        continue
                    reminder = row["reminder_time"]
                    remind_at = datetime.combine(today, reminder, ZONE)
                    use_reminder = remind_at > now_local() + timedelta(minutes=5)
                    cur.execute("""INSERT INTO public.tasks(user_id, task_text, task_date, status, reminder_time)
                        VALUES(%s,%s,%s,'pending',%s) RETURNING id""",
                                (user["id"], "🎯 " + row["title"], today, reminder if use_reminder else None))
                    task_id = cur.fetchone()["id"]
                    cur.execute("UPDATE public.qadam_plan_steps SET task_id=%s WHERE id=%s", (str(task_id), row["step_id"]))
                    cur.execute("UPDATE public.users SET last_completion_notified_date=NULL WHERE id=%s", (user["id"],))
                    created += 1
                    if user["live_checklist_message_id"] and user["live_checklist_date"] == today:
                        touched[row["chat_id"]] = user
        for chat_id in touched:
            try:
                fresh = get_user(chat_id)
                refresh_checklist(chat_id, fresh)
            except Exception as error:
                print("Plan checklist refresh failed:", type(error).__name__)
        return created

    def remove_pending_tasks(cur, task_ids, today):
        ids = [t for t in task_ids if t]
        if ids:
            cur.execute("""DELETE FROM public.tasks WHERE id::text=ANY(%s) AND status='pending' AND task_date>=%s""",
                        (ids, today))

    def set_habit_active(cur, plan, active):
        if plan.get("habit_id"):
            cur.execute("UPDATE public.qadam_habits SET active=%s WHERE id=%s AND scope=%s AND chat_id=%s",
                        (active, plan["habit_id"], scope(), plan["chat_id"]))

    # ---------------------------------------------------------------- replan
    def do_replan(chat_id, plan_id, extend_days=0, start=None):
        today = get_today()
        with connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (chat_id,))
                sync(cur, chat_id)
                plan = load_plan(cur, chat_id, plan_id, lock=True)
                if plan["status"] == "archived":
                    raise HTTPException(409, "Reja arxivlangan.")
                steps = plan["steps"]
                if start is None:
                    today_open = any(s["day"] == today and s["status"] == "pending" and not s["is_buffer"] for s in steps)
                    start = today if today_open or now_local().hour < 20 else today + timedelta(days=1)
                weekdays = plan["weekdays"]
                answers = plan["answers"] or {}
                # 1. steps that move: pending from `start`, pending in the past, and uncarried skips
                moving = [s for s in steps if not s["is_buffer"] and (
                    (s["status"] == "pending") or (s["status"] == "skipped" and not s["carried"]))]
                future_pending = [s for s in steps if s["status"] == "pending" and s["day"] >= start]
                remove_pending_tasks(cur, [s["task_id"] for s in steps if s["status"] == "pending"
                                           and s["day"] >= today], today)
                cur.execute("UPDATE public.qadam_plan_steps SET status='skipped' WHERE plan_id=%s AND status='pending' AND day<%s",
                            (plan_id, start))
                cur.execute("DELETE FROM public.qadam_plan_steps WHERE id=ANY(%s)",
                            ([s["id"] for s in future_pending],))
                cur.execute("UPDATE public.qadam_plan_steps SET carried=true WHERE plan_id=%s AND status='skipped'",
                            (plan_id,))
                end_day = plan["end_day"] + timedelta(days=extend_days)
                if end_day < start:
                    end_day = start + timedelta(days=max(extend_days, 6))
                span = (end_day - start).days + 1
                days_list = L.schedule_days(start, span, weekdays)
                cur.execute("SELECT COALESCE(MAX(position),0) FROM public.qadam_plan_steps WHERE plan_id=%s", (plan_id,))
                position = cur.fetchone()["coalesce"]
                feas, extended = None, False
                if plan["kind"] == "volume":
                    done_units = sum((s["units"] or 0) for s in steps if s["status"] == "done")
                    remaining = (plan["total"] or 0) - done_units
                    if remaining <= 0:
                        cur.execute("UPDATE public.qadam_plans SET status='done' WHERE id=%s", (plan_id,))
                        set_habit_active(cur, plan, False)
                        return {"done": True}
                    mpu = answers.get("mpu") or L.DEFAULT_MPU.get(plan["unit"], 10)
                    while not days_list or L.study_day_count(start, span, weekdays) < 1:
                        span += 7
                        days_list = L.schedule_days(start, span, weekdays)
                        extended = True
                    try:
                        ranges = L.remaining_ranges(steps, plan["total"], plan["unit"])
                    except ValueError:
                        raise HTTPException(409, "Eski rejadagi miqdorlar oralig‘i noaniq. Yangi reja tuzing.")
                    new_steps = L.build_remaining_volume(answers.get("base_title", plan["title"]), ranges,
                                                         plan["unit"], days_list, mpu=mpu)
                    feas = L.feasibility("volume", plan["daily_minutes"], days_list, start, weekdays,
                                         total=remaining, mpu=mpu)
                else:
                    queue = sorted(moving, key=lambda s: (s["position"], s["id"]))
                    if not queue:
                        cur.execute("UPDATE public.qadam_plans SET status='done' WHERE id=%s", (plan_id,))
                        set_habit_active(cur, plan, False)
                        return {"done": True}
                    needed = len(queue)
                    if len(days_list) < needed:
                        span = L.extend_span(start, weekdays, needed, span, buffer=False)
                        days_list = L.schedule_days(start, span, weekdays)
                        extended = True
                    new_steps = L.spread_stages([{"title": s["title"], "minutes": s["minutes"]} for s in queue],
                                                days_list, dense=plan["kind"] == "ramp")
                    for old, new in zip(queue, sorted(new_steps, key=lambda s: s["day"])):
                        new["position"] = old["position"]
                for i, step in enumerate(new_steps):
                    cur.execute("""INSERT INTO public.qadam_plan_steps(plan_id,day,position,title,units,minutes,is_buffer,unit_start,unit_end)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                                (plan_id, step["day"], step.get("position", position + 1 + i), step["title"],
                                 step["units"], step["minutes"], step["is_buffer"], step.get("unit_start"), step.get("unit_end")))
                new_end = max(s["day"] for s in new_steps)
                final_end = new_end if (extended or extend_days) else max(new_end, plan["end_day"])
                cur.execute("""UPDATE public.qadam_plans SET end_day=%s, unanswered=0,
                    status=CASE WHEN status='paused' THEN 'active' ELSE status END WHERE id=%s""",
                            (final_end, plan_id))
                if plan["status"] == "paused":
                    set_habit_active(cur, plan, True)
        created = materialize(chat_id) if plan["mode"] == "tasks" else 0
        real = [s for s in new_steps if not s["is_buffer"]]
        per_day = (max(s["units"] for s in real) if plan["kind"] == "volume" else
                   max((s["minutes"] or 0) for s in real)) if real else None
        return {"done": False, "added": len(new_steps), "per_day": per_day, "end_day": final_end.isoformat(),
                "extended": extended or extend_days > 0, "feasibility": feas, "materialized": created}

    # ---------------------------------------------------------------- marking
    def mark_done(chat_id, step_id):
        today = get_today()
        with connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""SELECT s.*, p.mode, p.status AS plan_status, p.id AS pid FROM public.qadam_plan_steps s
                    JOIN public.qadam_plans p ON p.id=s.plan_id
                    WHERE s.id=%s AND p.scope=%s AND p.chat_id=%s""", (step_id, scope(), chat_id))
                step = cur.fetchone()
                if not step:
                    raise HTTPException(404, "Qadam topilmadi.")
                if step["status"] == "done":
                    return {"ok": True, "already": True, "plan_id": step["pid"]}
                if step["carried"] or step["plan_status"] != "active":
                    raise HTTPException(409, "Bu qadam boshqa kunga ko‘chirilgan yoki reja faol emas.")
                if step["day"] > today:
                    raise HTTPException(409, "Bu qadamning kuni hali kelmagan.")
        task_synced = False
        if step["mode"] == "tasks" and step["task_id"]:
            user = get_user(chat_id)
            result = transition_task(user, step["task_id"], "completed")
            if result.get("conflict") or result["task"]["status"] != "completed":
                raise HTTPException(409, "Vazifa boshqa natija bilan yopilgan. Rejani qayta taqsimlang.")
            sync_task_result(chat_id, user, result["task"])
            task_synced = True
        with connection() as conn:
            with conn.cursor() as cur:
                if step["mode"] == "habit":
                    cur.execute("""UPDATE public.qadam_habit_days d SET status='done', marked_at=now()
                        FROM public.qadam_plans p WHERE p.id=%s AND d.scope=p.scope AND d.chat_id=p.chat_id
                        AND d.kind='habit' AND d.ref=p.habit_id::text AND d.day=%s AND d.status='pending'""",
                                (step["pid"], step["day"]))
                cur.execute("""UPDATE public.qadam_plan_steps s SET status='done', done_at=now()
                    FROM public.qadam_plans p WHERE s.id=%s AND p.id=s.plan_id
                    AND p.status='active' AND NOT s.carried AND s.status<>'done' RETURNING s.id""",
                            (step_id,))
                if not cur.fetchone():
                    raise HTTPException(409, "Qadam o‘zgargan. Rejani yangilang.")
                cur.execute("UPDATE public.qadam_plans SET unanswered=0 WHERE id=%s", (step["pid"],))
        activity(chat_id)
        send_milestones(chat_id)
        return {"ok": True, "plan_id": step["pid"], "task_synced": task_synced}

    def change_status(chat_id, plan_id, action):
        with connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (chat_id,))
                plan = load_plan(cur, chat_id, plan_id, lock=True)
                today = get_today()
                if action == "pause" and plan["status"] == "active":
                    cur.execute("UPDATE public.qadam_plans SET status='paused' WHERE id=%s", (plan_id,))
                    remove_pending_tasks(cur, [s["task_id"] for s in plan["steps"]
                                               if s["status"] == "pending" and s["day"] >= today], today)
                    set_habit_active(cur, plan, False)
                elif action == "archive" and plan["status"] != "archived":
                    cur.execute("UPDATE public.qadam_plans SET status='archived' WHERE id=%s", (plan_id,))
                    remove_pending_tasks(cur, [s["task_id"] for s in plan["steps"]
                                               if s["status"] == "pending" and s["day"] >= today], today)
                    set_habit_active(cur, plan, False)
                elif action == "resume" and plan["status"] != "paused":
                    return {"ok": True, "unchanged": True}
        if action == "resume":
            return {"ok": True, **do_replan(chat_id, plan_id, 0, None)}
        return {"ok": True}

    # ---------------------------------------------------------------- messages
    def open_button(text="🎯 Rejalarim", plan_id=None, coach=False):
        url = plans_url
        if plan_id:
            url += ("&" if "?" in url else "?") + "plan=%s" % plan_id + ("&coach=1" if coach else "")
        return {"text": text, "web_app": {"url": url}}

    def deliver(chat_id, text, rows):
        try:
            send(chat_id, text, {"inline_keyboard": rows}, "HTML")
            return True
        except Exception as error:
            print("Plan message failed:", type(error).__name__)
            return False

    def short(text, limit=24):
        return text if len(text) <= limit else text[:limit - 1] + "…"

    MILESTONE_TEXT = {
        25: "🌱 <b>Yaxshi boshlanish!</b> Rejaning choragi bajarildi.",
        50: "🔥 <b>Yarmiga keldingiz!</b> Shu sur’atda davom eting.",
        75: "🚀 <b>Oz qoldi!</b> Rejaning to‘rtdan uch qismi tayyor.",
        100: "🎉 <b>Tabriklayman!</b> Reja to‘liq bajarildi.",
    }

    def send_milestones(only_chat=None):
        sent = 0
        with connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""SELECT * FROM public.qadam_plans WHERE scope=%s AND status='active'
                    AND (%s::bigint IS NULL OR chat_id=%s)""", (scope(), only_chat, only_chat))
                plans = [dict(r) for r in cur.fetchall()]
                for plan in plans:
                    cur.execute("SELECT day, units, status, is_buffer, carried FROM public.qadam_plan_steps WHERE plan_id=%s",
                                (plan["id"],))
                    steps = [dict(r) for r in cur.fetchall()]
                    percent = L.progress_percent(plan["kind"], plan["total"], steps)
                    milestone = L.next_milestone(percent, plan["last_milestone"])
                    if not milestone:
                        continue
                    cur.execute("""UPDATE public.qadam_plans SET last_milestone=%s,
                        status=CASE WHEN %s=100 THEN 'done' ELSE status END
                        WHERE id=%s AND last_milestone<%s RETURNING id""",
                                (milestone, milestone, plan["id"], milestone))
                    if not cur.fetchone():
                        continue
                    if milestone == 100:
                        set_habit_active(cur, plan, False)
                    plan["_m"], plan["_p"] = milestone, percent
        for plan in plans:
            if "_m" not in plan:
                continue
            text = "%s\n\n🎯 %s\n%s %s%%" % (MILESTONE_TEXT[plan["_m"]], html.escape(plan["title"]),
                                            L.progress_bar(plan["_p"]), plan["_p"])
            if plan["_m"] == 100:
                text += "\n\nYangi maqsad qo‘yamizmi?"
            if deliver(plan["chat_id"], text, [[open_button("🎯 Rejalarim", plan["id"])]]):
                sent += 1
        return sent

    def run_daily():
        created = materialize()
        with connection() as conn:
            with conn.cursor() as cur:
                sync(cur)
        return {"created": created, "milestones": send_milestones()}

    def run_checkin():
        today = get_today()
        report = {"checkin": 0, "comeback": 0, "paused": 0}
        with connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                sync(cur)
                cur.execute("SELECT DISTINCT chat_id FROM public.qadam_plans WHERE scope=%s AND status='active'", (scope(),))
                chats = [r["chat_id"] for r in cur.fetchall()]
        for chat_id in chats:
            user = get_user(chat_id)
            if not user or user["state"] == "blocked" or (require_joined() and user.get("active_bot_id") != bot_id()):
                continue
            with connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    plans = load_plans(cur, chat_id, ("active",))
            # Stop nagging: 3 unanswered nudges pause the plan.
            live = []
            for plan in plans:
                if plan["unanswered"] >= 3:
                    change_status(chat_id, plan["id"], "pause")
                    deliver(chat_id, "⏸ <b>%s</b> rejasini vaqtincha to‘xtatdim — bezovta qilmayman.\nQaytishga tayyor bo‘lsangiz, "
                                     "rejani oching va davom ettiring." % html.escape(plan["title"]),
                            [[open_button("▶️ Davom ettirish", plan["id"])]])
                    report["paused"] += 1
                else:
                    live.append(plan)
            if not live or not claim(get_connection, scope(), str(user["id"]), "plan_msg", str(today)):
                continue
            behind = next((p for p in live if len(L.unanswered_past(p["steps"], today)) >= 2), None)
            if behind:
                count = len(L.unanswered_past(behind["steps"], today))
                text = ("🌿 <b>%s</b>\n\n%d ta qadam orqada qoldi — hammamizda shunday bo‘ladi. "
                        "Qolgan qismni qolgan kunlarga qayta taqsimlaymizmi?") % (html.escape(behind["title"]), count)
                rows = [[{"text": "🔄 Qayta taqsimlash", "callback_data": "plan|replan|%s" % behind["id"]},
                         {"text": "⏸ To‘xtatish", "callback_data": "plan|pause|%s" % behind["id"]}],
                        [open_button("💬 Murabbiy bilan gaplashish", behind["id"], True)]]
                if deliver(chat_id, text, rows):
                    report["comeback"] += 1
                    with connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute("UPDATE public.qadam_plans SET unanswered=unanswered+1 WHERE id=%s", (behind["id"],))
                continue
            due = []
            for plan in live:
                step = L.today_step(plan["steps"], today)
                if plan["mode"] == "tasks" and step and step["status"] == "pending":
                    due.append((plan, step))
            if not due:
                continue
            lines, rows = ["🎯 <b>Bugungi qadam</b>"], []
            for plan, step in due[:3]:
                streak = L.streak(plan["steps"], today)
                percent = L.progress_percent(plan["kind"], plan["total"], plan["steps"])
                line = "\n📌 %s" % html.escape(step["title"])
                if step["minutes"]:
                    line += "\n⏱ ~%d daqiqa" % step["minutes"]
                if streak:
                    line += "\n🔥 %d kun ketma-ket" % streak
                line += "\n%s %d%%" % (L.progress_bar(percent), percent)
                lines.append(line)
                rows.append([{"text": "✅ " + short(plan["title"], 18), "callback_data": "plan|done|%s" % step["id"]},
                             {"text": "⏭ Ertaga", "callback_data": "plan|later|%s" % step["id"]}])
            rows.append([open_button("💬 Murabbiy", due[0][0]["id"], True)])
            if deliver(chat_id, "\n".join(lines), rows):
                report["checkin"] += 1
                with connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("UPDATE public.qadam_plans SET unanswered=unanswered+1 WHERE id=ANY(%s)",
                                    ([p["id"] for p, _ in due[:3]],))
        return report

    # ---------------------------------------------------------------- bot callbacks
    def handle_callback(chat_id, data, callback_query_id=None, message_id=None):
        parts = data.split("|")
        reply = None
        try:
            if len(parts) != 3 or not parts[2].isdigit():
                reply = "Tugma eskirgan."
            elif parts[1] == "done":
                result = mark_done(chat_id, int(parts[2]))
                reply = "✅ Barakalla!" if not result.get("already") else "Bu qadam allaqachon belgilangan."
            elif parts[1] == "later":
                with connection() as conn:
                    with conn.cursor(cursor_factory=RealDictCursor) as cur:
                        cur.execute("""SELECT s.id, s.task_id, s.status, p.id AS pid, p.kind, p.unit
                            FROM public.qadam_plan_steps s JOIN public.qadam_plans p ON p.id=s.plan_id
                            WHERE s.id=%s AND p.scope=%s AND p.chat_id=%s""", (int(parts[2]), scope(), chat_id))
                        step = cur.fetchone()
                if not step or step["status"] != "pending":
                    reply = "Bu qadam allaqachon yopilgan."
                else:
                    with connection() as conn:
                        with conn.cursor() as cur:
                            remove_pending_tasks(cur, [step["task_id"]], get_today())
                            cur.execute("UPDATE public.qadam_plan_steps SET status='skipped' WHERE id=%s", (step["id"],))
                    result = do_replan(chat_id, step["pid"], 0, get_today() + timedelta(days=1))
                    reply = "🔄 Qayta taqsimlandi"
                    if not result.get("done") and result.get("per_day"):
                        unit = step["unit"] if step["kind"] == "volume" else "daqiqa"
                        deliver(chat_id, "🔄 Qolgan qismni qayta taqsimladim: endi kuniga ~%s %s. "
                                         "Bir kun qolib ketsa ham, reja saqlanadi." % (result["per_day"], unit or ""),
                                [[open_button("🎯 Rejani ko‘rish", step["pid"])]])
            elif parts[1] == "replan":
                result = do_replan(chat_id, int(parts[2]), 0, None)
                reply = "🔄 Qayta taqsimlandi"
            elif parts[1] == "pause":
                change_status(chat_id, int(parts[2]), "pause")
                reply = "⏸ To‘xtatildi"
            else:
                reply = "Tugma eskirgan."
        except HTTPException as error:
            reply = str(error.detail)[:150]
        if callback_query_id:
            answer(callback_query_id, reply)
        return {"ok": True, "route": "plan_callback"}

    def send_plans_link(chat_id):
        send(chat_id, "🎯 Katta maqsadni kunlarga bo‘lib beraman. Mini Appni oching va maqsadingizni yozing.",
             {"inline_keyboard": [[open_button("🎯 Reja tuzish")]]})
        return {"ok": True, "route": "plans_link"}

    # ---------------------------------------------------------------- Mini App API
    @router.get("/miniapp/plans")
    def list_plans(chat_id: int = Depends(auth)):
        today = get_today()
        with connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                owner(cur, chat_id)
                sync(cur, chat_id)
                cur.execute("UPDATE public.qadam_plans SET unanswered=0 WHERE scope=%s AND chat_id=%s AND status='active'",
                            (scope(), chat_id))
                plans = load_plans(cur, chat_id)
        return {"today": today.isoformat(), "plans": [serialize(p, today) for p in plans],
                "ai": {"available": ai.available(), "admin_url": ai.ADMIN_URL},
                "limits": {"active": MAX_ACTIVE_PLANS, "max_days": L.MAX_DAYS}}

    @router.post("/miniapp/plans/draft")
    def draft(payload: DraftIn, chat_id: int = Depends(auth)):
        with connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                owner(cur, chat_id)
        guess = L.parse_goal(payload.goal)
        refined = False
        if guess["kind"] is None and ai.available():
            try:
                spend_ai(chat_id)
                text = ai.chat([{"role": "user", "content": (
                    "Maqsad: «%s». Uni turlang. FAQAT JSON: {\"kind\":\"volume|stages|ramp\",\"total\":son yoki null,"
                    "\"unit\":\"bet|dars|video|so‘z|km|masala|test|... yoki null\"}. volume — aniq miqdori bor ish; "
                    "stages — ketma-ket bosqichli ish; ramp — yangi odatni asta-sekin boshlash." % payload.goal)}],
                    where="plan draft", max_tokens=800, temperature=0)
                data = ai.extract_json(text) or {}
                if data.get("kind") in ("volume", "stages", "ramp"):
                    guess["kind"] = data["kind"]
                    refined = True
                    if data["kind"] == "volume":
                        try:
                            total = int(data.get("total"))
                            if 2 <= total <= L.MAX_TOTAL:
                                guess["total"], guess["unit"] = total, str(data.get("unit") or "")[:20] or None
                        except (TypeError, ValueError):
                            pass
            except ai.AIUnavailable:
                pass
        return {"title": guess["title"], "kind": guess["kind"], "total": guess["total"], "unit": guess["unit"],
                "needs_kind": guess["kind"] is None, "ai_refined": refined,
                "ai": {"available": ai.available(), "admin_url": ai.ADMIN_URL},
                "questions": question_sets(guess["unit"])}

    @router.post("/miniapp/plans/preview")
    def preview(payload: PreviewIn, chat_id: int = Depends(auth)):
        with connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                owner(cur, chat_id)
        answers = clean_answers(payload.kind, payload.answers)
        start = plan_start()
        try:
            if payload.kind == "stages":
                spend_ai(chat_id)
            steps, feas, summary = make_steps(payload, answers, start)
        except ai.AIUnavailable as error:
            return ai_failure(error)
        except ValueError:
            raise HTTPException(422, "Reja tuzib bo‘lmadi. Muddat yoki kunlarni o‘zgartiring.")
        return {"steps": [{"day": s["day"].isoformat(), "title": s["title"], "units": s["units"],
                           "minutes": s["minutes"], "is_buffer": s["is_buffer"]} for s in steps],
                "feasibility": feas, "summary": summary, "answers": answers,
                "habit_ok": payload.kind in ("volume", "ramp")}

    @router.post("/miniapp/plans")
    def create_plan(payload: CreateIn, chat_id: int = Depends(auth)):
        answers = clean_answers(payload.kind, payload.answers)
        today = get_today()
        real = [s for s in payload.steps if not s.is_buffer]
        if not real:
            raise HTTPException(422, "Reja bo‘sh.")
        if any(s.day < today or s.day > today + timedelta(days=L.MAX_DAYS + 7) for s in payload.steps):
            raise HTTPException(422, "Reja sanalari noto‘g‘ri. Rejani qayta tuzing.")
        if payload.kind == "volume" and payload.total and sum(s.units or 0 for s in real) != payload.total:
            raise HTTPException(422, "Reja yig‘indisi jami miqdorga teng emas. Rejani qayta tuzing.")
        if payload.mode == "habit" and payload.kind == "stages":
            raise HTTPException(422, "Bosqichli rejani odat qilib bo‘lmaydi.")
        steps = sorted(payload.steps, key=lambda s: s.day)
        with connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                owner(cur, chat_id)
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (chat_id,))
                if payload.request_id:
                    cur.execute("SELECT id FROM public.qadam_plans WHERE scope=%s AND chat_id=%s AND request_id=%s",
                                (scope(), chat_id, payload.request_id))
                    existing = cur.fetchone()
                    if existing:
                        return {"ok": True, "id": existing["id"], "duplicate": True}
                cur.execute("SELECT count(*) FROM public.qadam_plans WHERE scope=%s AND chat_id=%s AND status IN ('active','paused')",
                            (scope(), chat_id))
                if cur.fetchone()["count"] >= MAX_ACTIVE_PLANS:
                    raise HTTPException(422, "Bir vaqtda %d tagacha faol reja bo‘lishi mumkin." % MAX_ACTIVE_PLANS)
                title = payload.title.strip()
                habit_id = None
                reminder = SLOT_TIMES[answers["slot"]]
                if payload.mode == "habit":
                    cur.execute("SELECT count(*) FROM public.qadam_habits WHERE scope=%s AND chat_id=%s AND active",
                                (scope(), chat_id))
                    if cur.fetchone()["count"] >= 30:
                        raise HTTPException(422, "Odatlar soni limitga yetgan.")
                    if payload.kind == "volume":
                        per = max(s.units or 0 for s in real)
                        habit_title = "%s: kuniga ~%s %s" % (title, per, payload.unit or "ta")
                    else:
                        habit_title = "%s (kuniga %s→%s daqiqa)" % (title, answers["start_minutes"], answers["target_minutes"])
                    cur.execute("""INSERT INTO public.qadam_habits(scope,chat_id,title,kind,reminder_time,weekdays)
                        VALUES(%s,%s,%s,'habit',%s::time,%s) RETURNING id""",
                                (scope(), chat_id, habit_title[:120], reminder, answers["weekdays"]))
                    habit_id = cur.fetchone()["id"]
                stored = dict(answers)
                stored["base_title"] = title
                cur.execute("""INSERT INTO public.qadam_plans(scope,chat_id,title,kind,unit,total,mode,habit_id,reminder_time,
                    weekdays,daily_minutes,answers,start_day,end_day,request_id)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::time,%s,%s,%s,%s,%s,%s) RETURNING id""",
                            (scope(), chat_id, title, payload.kind, payload.unit, payload.total, payload.mode, habit_id,
                             reminder, answers["weekdays"], answers["minutes"], Json(stored),
                             steps[0].day, steps[-1].day, payload.request_id))
                plan_id = cur.fetchone()["id"]
                unit_cursor = 1
                for position, step in enumerate(steps, 1):
                    unit_start = unit_cursor if payload.kind == "volume" and not step.is_buffer else None
                    unit_end = unit_cursor + (step.units or 0) - 1 if unit_start is not None else None
                    if unit_end is not None:
                        unit_cursor = unit_end + 1
                    cur.execute("""INSERT INTO public.qadam_plan_steps(plan_id,day,position,title,units,minutes,is_buffer,unit_start,unit_end)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                                (plan_id, step.day, position, step.title.strip(), step.units, step.minutes, step.is_buffer, unit_start, unit_end))
        activity(chat_id)
        created = materialize(chat_id) if payload.mode == "tasks" else 0
        return {"ok": True, "id": plan_id, "materialized": created}

    @router.post("/miniapp/plans/{plan_id}/replan")
    def replan(plan_id: int, payload: ReplanIn, chat_id: int = Depends(auth)):
        with connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                owner(cur, chat_id)
        result = do_replan(chat_id, plan_id, payload.extend_days, None)
        send_milestones(chat_id)
        return {"ok": True, **result}

    @router.post("/miniapp/plans/{plan_id}/status")
    def plan_status(plan_id: int, payload: StatusIn, chat_id: int = Depends(auth)):
        with connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                owner(cur, chat_id)
        return change_status(chat_id, plan_id, payload.action)

    @router.delete("/miniapp/plans/{plan_id}")
    def delete_plan(plan_id: int, chat_id: int = Depends(auth)):
        today = get_today()
        with connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                owner(cur, chat_id)
                plan = load_plan(cur, chat_id, plan_id, lock=True)
                remove_pending_tasks(cur, [s["task_id"] for s in plan["steps"]
                                           if s["status"] == "pending" and s["day"] >= today], today)
                set_habit_active(cur, plan, False)
                cur.execute("DELETE FROM public.qadam_plans WHERE id=%s", (plan_id,))
        return {"ok": True}

    @router.post("/miniapp/plans/steps/{step_id}/done")
    def step_done(step_id: int, chat_id: int = Depends(auth)):
        return mark_done(chat_id, step_id)

    @router.get("/miniapp/plans/{plan_id}/chat")
    def chat_history(plan_id: int, chat_id: int = Depends(auth)):
        with connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                owner(cur, chat_id)
                load_plan(cur, chat_id, plan_id)
                cur.execute("""SELECT role, content FROM (SELECT id, role, content FROM public.qadam_plan_chat
                    WHERE plan_id=%s ORDER BY id DESC LIMIT 30) t ORDER BY id""", (plan_id,))
                return {"messages": [dict(r) for r in cur.fetchall()]}

    @router.post("/miniapp/plans/{plan_id}/chat")
    def chat(plan_id: int, payload: ChatIn, chat_id: int = Depends(auth)):
        today = get_today()
        with connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                owner(cur, chat_id)
                sync(cur, chat_id)
                plan = load_plan(cur, chat_id, plan_id)
                cur.execute("""SELECT role, content FROM (SELECT id, role, content FROM public.qadam_plan_chat
                    WHERE plan_id=%s ORDER BY id DESC LIMIT 8) t ORDER BY id""", (plan_id,))
                history = [dict(r) for r in cur.fetchall()]
        spend_ai(chat_id, "chat", CHAT_DAILY_LIMIT)
        steps = plan["steps"]
        percent = L.progress_percent(plan["kind"], plan["total"], steps)
        today_step = L.today_step(steps, today)
        remaining_days = max(0, (plan["end_day"] - today).days)
        context = ("Reja: %s\nTuri: %s\nBajarilgan: %d%%\nKetma-ket kunlar: %d\nOrqada qolgan qadamlar: %d\n"
                   "Tugash sanasi: %s (%d kun qoldi)\nBugungi qadam: %s\nReja holati: %s") % (
            plan["title"], plan["kind"], percent, L.streak(steps, today), len(L.unanswered_past(steps, today)),
            plan["end_day"].isoformat(), remaining_days,
            ("%s [%s]" % (today_step["title"], today_step["status"])) if today_step else "yo‘q",
            plan["status"])
        system = (
            "Siz Qadam botidagi do‘stona murabbiysiz. O‘zbek tilida (lotin yozuvida), qisqa (80 so‘zgacha), iliq va aniq javob bering. "
            "Foydalanuvchini ayblamang, kichik keyingi qadamni taklif qiling. Tibbiy yoki moliyaviy maslahat bermang. "
            "Rejani o‘zgartirish kerak bo‘lsa, buni faqat taklif qiling — o‘zingiz o‘zgartira olmaysiz.\n"
            "FAQAT JSON qaytaring: {\"reply\":\"...\",\"action\":\"none|replan|pause\"}. "
            "action=replan — foydalanuvchi orqada qolgan yoki vaqti kamaygan; pause — dam olish kerak.\n\n" + context)
        messages = [{"role": "system", "content": system}] + [
            {"role": h["role"], "content": h["content"]} for h in history
        ] + [{"role": "user", "content": payload.message.strip()}]
        try:
            text = ai.chat(messages, where="plan chat", max_tokens=1500, temperature=0.6)
        except ai.AIUnavailable as error:
            return ai_failure(error)
        data = ai.extract_json(text)
        reply = (data or {}).get("reply") if data else None
        if not isinstance(reply, str) or not reply.strip():
            reply = text.strip() or "Hozir javob topa olmadim. Yana bir bor yozing."
        action = (data or {}).get("action") if data else "none"
        reply = reply.strip()[:1200]
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO public.qadam_plan_chat(plan_id, role, content) VALUES(%s,'user',%s),(%s,'assistant',%s)",
                            (plan_id, payload.message.strip(), plan_id, reply))
                cur.execute("""DELETE FROM public.qadam_plan_chat WHERE plan_id=%s AND id NOT IN
                    (SELECT id FROM public.qadam_plan_chat WHERE plan_id=%s ORDER BY id DESC LIMIT 60)""", (plan_id, plan_id))
        suggest = []
        if action == "replan":
            suggest.append({"action": "replan", "label": "🔄 Rejani qayta taqsimlash"})
        if action == "pause":
            suggest.append({"action": "pause", "label": "⏸ Rejani to‘xtatib turish"})
        return {"reply": reply, "suggest": suggest}

    return {"daily": run_daily, "checkin": run_checkin}, handle_callback, send_plans_link
