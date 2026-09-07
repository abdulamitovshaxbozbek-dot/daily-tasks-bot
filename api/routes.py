from datetime import date, timedelta
from typing import Optional
import os
import requests
import re

from fastapi import APIRouter, HTTPException, Header, Depends, Query
from pydantic import BaseModel, Field
from psycopg2.extras import RealDictCursor

from database import get_connection


router = APIRouter(prefix="/api")


# =========================================================
# CONFIG
# =========================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

LEGACY_BACKEND_URL = os.environ.get(
    "LEGACY_BACKEND_URL"
)

ADMIN_CHAT_ID = "8908985083"

TIMEZONE = "Asia/Tashkent"


# =========================================================
# API KEY
# =========================================================

def verify_api_key(
    x_api_key: Optional[str] = Header(default=None)
):
    expected = os.environ.get("API_KEY")

    if expected and x_api_key != expected:
        raise HTTPException(
            status_code=401,
            detail="Invalid API key"
        )


# =========================================================
# MODELS
# =========================================================

class StartUserRequest(BaseModel):
    telegram_chat_id: int
    telegram_username: Optional[str] = ""
    first_name: str = "Do'st"


class MorningTimeRequest(BaseModel):
    morning_time: str = Field(
        pattern=r"^(0[2-9]|10):00$"
    )


class CreateTasksRequest(BaseModel):
    telegram_chat_id: int
    tasks: list[str]


class TaskStatusRequest(BaseModel):
    telegram_chat_id: int
    status: str


# =========================================================
# HELPERS
# =========================================================

def get_user_by_chat_id(chat_id: int):

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT *
                FROM public.users
                WHERE telegram_chat_id = %s
                """,
                (chat_id,)
            )

            return cur.fetchone()


def get_today():

    with get_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    (
                        CURRENT_TIMESTAMP
                        AT TIME ZONE 'Asia/Tashkent'
                    )::date
                """
            )

            return cur.fetchone()[0]


def calculate_stats(tasks):

    total = len(tasks)

    completed = sum(
        1
        for task in tasks
        if task["status"] == "completed"
    )

    failed = sum(
        1
        for task in tasks
        if task["status"] == "failed"
    )

    pending = sum(
        1
        for task in tasks
        if task["status"] == "pending"
    )

    percent = (
        round(completed / total * 100)
        if total
        else 0
    )

    return {
        "total": total,
        "completed": completed,
        "failed": failed,
        "pending": pending,
        "percent": percent
    }


def is_admin(chat_id: int):

    return str(chat_id) == ADMIN_CHAT_ID


# =========================================================
# TELEGRAM HELPERS
# =========================================================

def telegram_send_message(
    chat_id: int,
    text: str
):

    if not TELEGRAM_TOKEN:

        raise HTTPException(
            status_code=500,
            detail="TELEGRAM_TOKEN is not configured"
        )

    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text
        },
        timeout=15
    )

    if not response.ok:

        try:
            telegram_error = response.json()
        except Exception:
            telegram_error = response.text

        raise HTTPException(
            status_code=500,
            detail=f"Telegram sendMessage failed: {telegram_error}"
        )

    return response.json()
# =========================================================
# TELEGRAM /START
# =========================================================

def telegram_send_message_with_keyboard(
    chat_id: int,
    text: str,
    reply_markup: dict
):
    if not TELEGRAM_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="TELEGRAM_TOKEN is not configured"
        )

    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "reply_markup": reply_markup
        },
        timeout=15
    )

    if not response.ok:
        try:
            telegram_error = response.json()
        except Exception:
            telegram_error = response.text

        raise HTTPException(
            status_code=500,
            detail=f"Telegram sendMessage failed: {telegram_error}"
        )
        
    return response.json()
    
def telegram_send_morning_keyboard(
    chat_id: int,
    first_name: str
):

    keyboard = []

    times = [
        "02:00",
        "03:00",
        "04:00",
        "05:00",
        "06:00",
        "07:00",
        "08:00",
        "09:00",
        "10:00"
    ]

    row = []

    for time in times:

        row.append({
            "text": time,
            "callback_data": f"morning_time|{time}"
        })

        if len(row) == 3:

            keyboard.append(row)
            row = []

    if row:

        keyboard.append(row)

    return telegram_send_message_with_keyboard(
        chat_id,
        f"""🌅 Assalomu alaykum, {first_name}!

📋 Kunlik vazifalar botiga xush kelibsiz.

Tizim ishga tushishi uchun savolga javob bering:

🕐 Kuningizni soat nechchida rejalashtirasiz?""",
        keyboard
    )


def handle_telegram_start(
    chat_id: int,
    first_name: str,
    username: Optional[str] = None
):

    user = get_user_by_chat_id(chat_id)

    # -----------------------------------------------------
    # MAVJUD USER
    # -----------------------------------------------------

    if user:

        with get_connection() as conn:

            with conn.cursor() as cur:

                cur.execute(
                    """
                    UPDATE public.users
                    SET
                        last_active_date = %s,
                        first_name = %s,
                        telegram_username = %s
                    WHERE telegram_chat_id = %s
                    """,
                    (
                        get_today(),
                        first_name,
                        username or "",
                        chat_id
                    )
                )

            conn.commit()

        telegram_send_message(
            chat_id,
            f"""👋 Assalomu alaykum, {first_name}!

Siz allaqachon ro'yxatdan o'tgansiz. ✅

📋 Vazifalaringizni yuborishni davom ettirishingiz mumkin."""
        )

        return {
            "ok": True,
            "route": "start",
            "handled_by": "fastapi",
            "existing_user": True
        }

    # -----------------------------------------------------
    # YANGI USER
    # -----------------------------------------------------

    with get_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                INSERT INTO public.users (
                    telegram_chat_id,
                    telegram_username,
                    first_name,
                    timezone,
                    state,
                    subscription_status,
                    last_active_date
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    'Asia/Tashkent',
                    'waiting_morning_time',
                    'trial',
                    %s
                )
                RETURNING id
                """,
                (
                    chat_id,
                    username or "",
                    first_name,
                    get_today()
                )
            )

            user_id = cur.fetchone()[0]

        conn.commit()

    telegram_send_morning_keyboard(
        chat_id,
        first_name
    )

    return {
        "ok": True,
        "route": "start",
        "handled_by": "fastapi",
        "existing_user": False,
        "user_id": str(user_id)
    }


# =========================================================
# MORNING TIME CALLBACK
# =========================================================

def handle_telegram_morning_time(
    chat_id: int,
    callback_query_id: str,
    callback_data: str
):

    parts = callback_data.split("|", 1)

    if len(parts) != 2:

        telegram_answer_callback(
            callback_query_id,
            "❌ Noto'g'ri vaqt."
        )

        return {
            "ok": True,
            "route": "morning_time",
            "handled_by": "fastapi",
            "error": "Invalid callback data"
        }

    morning_time = parts[1]

    if not re.match(
        r"^(0[2-9]|10):00$",
        morning_time
    ):

        telegram_answer_callback(
            callback_query_id,
            "❌ Noto'g'ri vaqt."
        )

        return {
            "ok": True,
            "route": "morning_time",
            "handled_by": "fastapi",
            "error": "Invalid morning time"
        }

    user = get_user_by_chat_id(chat_id)

    if not user:

        telegram_answer_callback(
            callback_query_id,
            "❌ Avval /start bosing."
        )

        return {
            "ok": True,
            "route": "morning_time",
            "handled_by": "fastapi",
            "error": "User not found"
        }

    if user["state"] != "waiting_morning_time":

        telegram_answer_callback(
            callback_query_id,
            "✅ Vaqt allaqachon tanlangan."
        )

        telegram_send_message(
            chat_id,
            """👋 Sizning ertalabki eslatma vaqtingiz allaqachon tanlangan.

📋 Vazifalaringizni yuborishingiz mumkin."""
        )

        return {
            "ok": True,
            "route": "morning_time",
            "handled_by": "fastapi",
            "already_set": True
        }

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                UPDATE public.users
                SET
                    morning_time = %s::time,
                    state = 'active',
                    last_active_date = %s
                WHERE id = %s
                RETURNING *
                """,
                (
                    morning_time,
                    get_today(),
                    user["id"]
                )
            )

            updated_user = cur.fetchone()

        conn.commit()

    telegram_answer_callback(
        callback_query_id,
        "✅ Vaqt saqlandi!"
    )

    telegram_send_message(
        chat_id,
        f"""✅ Ajoyib, {updated_user["first_name"]}!

⏰ Ertalabki eslatma vaqtingiz: {morning_time}

📋 Endi kunlik vazifalaringizni yuborishingiz mumkin."""
    )

    return {
        "ok": True,
        "route": "morning_time",
        "handled_by": "fastapi",
        "morning_time": morning_time
    }


# =========================================================
# /YAKUNLADIM
# =========================================================

def telegram_send_task_status_keyboard(
    chat_id: int,
    task
):

    keyboard = [
        [
            {
                "text": "✅ Bajarildi",
                "callback_data": (
                    f"task_status|completed|{task['id']}"
                )
            },
            {
                "text": "❌ Bajarilmadi",
                "callback_data": (
                    f"task_status|failed|{task['id']}"
                )
            }
        ]
    ]

    return telegram_send_message_with_keyboard(
        chat_id,
        f"📌 {task['task_text']}",
        keyboard
    )


def handle_telegram_finish_day(
    chat_id: int
):

    user = get_user_by_chat_id(chat_id)

    if not user:

        telegram_send_message(
            chat_id,
            "⚠️ Avval /start buyrug'ini bering."
        )

        return {
            "ok": True,
            "route": "finish_day",
            "handled_by": "fastapi",
            "error": "User not found"
        }

    if user["state"] == "completed":

        telegram_send_message(
            chat_id,
            """🏁 Siz bugungi vazifalarni allaqachon yakunlagansiz.

📊 Natijani ko'rish uchun /hisobot yuboring."""
        )

        return {
            "ok": True,
            "route": "finish_day",
            "handled_by": "fastapi",
            "already_completed": True
        }

    today = get_today()

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT *
                FROM public.tasks
                WHERE user_id = %s
                  AND task_date = %s
                  AND status = 'pending'
                ORDER BY created_at ASC
                """,
                (
                    user["id"],
                    today
                )
            )

            pending_tasks = cur.fetchall()

            if not pending_tasks:

                telegram_send_message(
                    chat_id,
                    """📋 Bugun uchun bajarilmagan vazifalar qolmagan.

🎉 Ajoyib!"""
                )

                return {
                    "ok": True,
                    "route": "finish_day",
                    "handled_by": "fastapi",
                    "finished": False,
                    "reason": "no_pending_tasks"
                }

            cur.execute(
                """
                UPDATE public.users
                SET
                    state = 'completed',
                    last_active_date = %s
                WHERE id = %s
                RETURNING *
                """,
                (
                    today,
                    user["id"]
                )
            )

            updated_user = cur.fetchone()

        conn.commit()

    sent_tasks = []

    for task in pending_tasks:

        telegram_send_task_status_keyboard(
            chat_id,
            task
        )

        sent_tasks.append(
            str(task["id"])
        )

    return {
        "ok": True,
        "route": "finish_day",
        "handled_by": "fastapi",
        "finished": True,
        "date": today,
        "pending_count": len(pending_tasks),
        "pending_task_ids": sent_tasks,
        "user_state": updated_user["state"]
    }


# =========================================================
# TASK STATUS CALLBACK
# =========================================================

def handle_telegram_task_status(
    chat_id: int,
    callback_query_id: str,
    callback_data: str,
    callback_message_id: Optional[int] = None
):

    parts = callback_data.split("|")

    if len(parts) != 3:

        telegram_answer_callback(
            callback_query_id,
            "❌ Noto'g'ri ma'lumot."
        )

        return {
            "ok": True,
            "route": "task_status",
            "handled_by": "fastapi",
            "error": "Invalid callback data"
        }

    _, status, task_id = parts

    if status not in (
        "completed",
        "failed"
    ):

        telegram_answer_callback(
            callback_query_id,
            "❌ Noto'g'ri status."
        )

        return {
            "ok": True,
            "route": "task_status",
            "handled_by": "fastapi",
            "error": "Invalid status"
        }

    user = get_user_by_chat_id(chat_id)

    if not user:

        telegram_answer_callback(
            callback_query_id,
            "❌ User topilmadi."
        )

        return {
            "ok": True,
            "route": "task_status",
            "handled_by": "fastapi",
            "error": "User not found"
        }

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                UPDATE public.tasks
                SET status = %s
                WHERE id = %s
                  AND user_id = %s
                  AND status = 'pending'
                RETURNING *
                """,
                (
                    status,
                    task_id,
                    user["id"]
                )
            )

            task = cur.fetchone()

            if not task:

                telegram_answer_callback(
                    callback_query_id,
                    "⚠️ Bu vazifa allaqachon belgilangan."
                )

                return {
                    "ok": True,
                    "route": "task_status",
                    "handled_by": "fastapi",
                    "already_processed": True
                }

            today = get_today()

            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM public.tasks
                WHERE user_id = %s
                  AND task_date = %s
                  AND status = 'pending'
                """,
                (
                    user["id"],
                    today
                )
            )

            pending_count = cur.fetchone()["count"]

            completion_notification_claimed = False

            if pending_count == 0:

                cur.execute(
                    """
                    UPDATE public.users
                    SET
                        last_completion_notified_date = %s,
                        last_active_date = %s
                    WHERE id = %s
                      AND last_completion_notified_date
                          IS DISTINCT FROM %s
                    RETURNING id
                    """,
                    (
                        today,
                        today,
                        user["id"],
                        today
                    )
                )

                claimed = cur.fetchone()

                if claimed:

                    completion_notification_claimed = True

            else:

                cur.execute(
                    """
                    UPDATE public.users
                    SET last_active_date = %s
                    WHERE id = %s
                    """,
                    (
                        today,
                        user["id"]
                    )
                )

        conn.commit()

    if status == "completed":

        telegram_answer_callback(
            callback_query_id,
            "✅ Bajarildi!"
        )

    else:

        telegram_answer_callback(
            callback_query_id,
            "❌ Bajarilmadi."
        )

    if callback_message_id:

        telegram_delete_message(
            chat_id,
            callback_message_id
        )

    if completion_notification_claimed:

        telegram_send_message(
            chat_id,
            """🎉 Barcha vazifalar belgilandi!

📊 Natijangizni ko'rish uchun /hisobot yuboring."""
        )

    return {
        "ok": True,
        "route": "task_status",
        "handled_by": "fastapi",
        "task_id": str(task["id"]),
        "status": status,
        "pending_count": pending_count,
        "completion_notification_sent":
            completion_notification_claimed
    }

# =========================================================
# REMINDERS
# =========================================================
def handle_reminders():

    today = get_today()
    results = []

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT
                    u.id,
                    u.telegram_chat_id,
                    u.first_name,
                    u.morning_time,
                    u.state,
                    u.last_reminder_sent_date,
                    (
                        u.created_at
                        AT TIME ZONE 'Asia/Tashkent'
                    )::date AS created_date,
                    MAX(t.task_date) AS last_task_date
                FROM public.users u
                LEFT JOIN public.tasks t
                    ON t.user_id = u.id
                WHERE u.telegram_chat_id IS NOT NULL
                GROUP BY u.id
                HAVING
                    (
                        (
                            u.morning_time IS NULL
                            OR u.state = 'waiting_morning_time'
                        )
                        AND
                        (
                            %s - (
                                u.created_at
                                AT TIME ZONE 'Asia/Tashkent'
                            )::date
                        ) BETWEEN 1 AND 3
                    )
                    OR
                    (
                        u.morning_time IS NOT NULL
                        AND
                        (
                            MAX(t.task_date) IS NULL
                            OR MAX(t.task_date) <= %s - 2
                        )
                    )
                """,
                (
                    today,
                    today
                )
            )

            candidates = cur.fetchall()

    for user in candidates:

        chat_id = user["telegram_chat_id"]

        if user["last_reminder_sent_date"] == today:
            continue

        first_name = user["first_name"] or "Do'st"

        if (
            user["morning_time"] is None
            or user["state"] == "waiting_morning_time"
        ):

            text = f"""👋 Assalomu alaykum, {first_name}!

⏰ Kuningizni rejalashtirish uchun ertalabki vaqtingizni tanlang.

📋 Vazifalaringizni tartibli boshlash uchun /start buyrug'ini bosing."""

        else:

            text = f"""👋 Salom, {first_name}!

📋 Bir necha kundan beri yangi vazifa qo'shilmagan.

Bugungi rejalaringizni yozib, kuningizni tartibli boshlang. 💪

✍️ Vazifalaringizni shu yerga yuboring."""

        try:

            telegram_send_message(
                chat_id,
                text
            )

            with get_connection() as update_conn:

                with update_conn.cursor() as update_cur:

                    update_cur.execute(
                        """
                        UPDATE public.users
                        SET last_reminder_sent_date = %s
                        WHERE id = %s
                          AND last_reminder_sent_date
                              IS DISTINCT FROM %s
                        """,
                        (
                            today,
                            user["id"],
                            today
                        )
                    )

                update_conn.commit()

            results.append({
                "chat_id": chat_id,
                "sent": True
            })

        except Exception as e:

            error_text = str(e)

            is_blocked = (
                "error_code': 403" in error_text
                and "bot was blocked by the user" in error_text
            )

            if is_blocked:

                with get_connection() as update_conn:

                    with update_conn.cursor() as update_cur:

                        update_cur.execute(
                            """
                            UPDATE public.users
                            SET state = 'blocked'
                            WHERE id = %s
                            """,
                            (user["id"],)
                        )

                    update_conn.commit()

            results.append({
                "chat_id": chat_id,
                "sent": False,
                "blocked": is_blocked,
                "error": error_text
            })

    return {
        "ok": True,
        "date": today,
        "checked": len(candidates),
        "results": results
    }


@router.post("/reminders/run")
def run_reminders(
    _: None = Depends(verify_api_key)
):

    return handle_reminders()

# =========================================================
# ADMIN
# =========================================================

def handle_telegram_admin(
    chat_id: int
):

    if not is_admin(chat_id):

        telegram_send_message(
            chat_id,
            "⛔ Sizda admin huquqi yo'q."
        )

        return {
            "ok": True,
            "route": "admin",
            "handled_by": "fastapi",
            "authorized": False
        }

    today = get_today()

    yesterday = today - timedelta(days=1)

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            # -------------------------------------------------
            # USER STATISTICS
            # -------------------------------------------------

            cur.execute(
                """
                SELECT
                    COUNT(*) AS total_users,

                    COUNT(*) FILTER (
                        WHERE (
                            created_at
                            AT TIME ZONE 'Asia/Tashkent'
                        )::date = %s
                    ) AS added_today,

                    COUNT(*) FILTER (
                        WHERE (
                            created_at
                            AT TIME ZONE 'Asia/Tashkent'
                        )::date = %s
                    ) AS added_yesterday,

                    COUNT(*) FILTER (
                        WHERE last_active_date >= %s
                    ) AS active_last_2_days,

                    COUNT(*) FILTER (
                        WHERE last_active_date = %s
                    ) AS active_today

                FROM public.users
                """,
                (
                    today,
                    yesterday,
                    yesterday,
                    today
                )
            )

            user_stats = cur.fetchone()

            # -------------------------------------------------
            # TODAY TASK STATISTICS
            # -------------------------------------------------

            cur.execute(
                """
                SELECT
                    COUNT(*) AS total_tasks,

                    COUNT(*) FILTER (
                        WHERE status = 'completed'
                    ) AS completed_tasks,

                    COUNT(*) FILTER (
                        WHERE status = 'failed'
                    ) AS failed_tasks,

                    COUNT(*) FILTER (
                        WHERE status = 'pending'
                    ) AS pending_tasks

                FROM public.tasks
                WHERE task_date = %s
                """,
                (today,)
            )

            task_stats = cur.fetchone()

    total_tasks = task_stats["total_tasks"]

    completed_tasks = task_stats["completed_tasks"]

    completion_percent = (
        round(
            completed_tasks /
            total_tasks *
            100
        )
        if total_tasks
        else 0
    )

    text = f"""👑 ADMIN PANEL

📅 {today.strftime("%d.%m.%Y")}

👥 FOYDALANUVCHILAR
├ Jami: {user_stats["total_users"]} ta
├ Bugun qo'shilgan: {user_stats["added_today"]} ta
└ Kecha qo'shilgan: {user_stats["added_yesterday"]} ta

📈 FAOLLIK
├ Bugun foydalangan: {user_stats["active_today"]} ta
└ Oxirgi 2 kunda foydalangan: {user_stats["active_last_2_days"]} ta

📋 BUGUNGI VAZIFALAR
├ Jami: {task_stats["total_tasks"]} ta
├ Bajarilgan: {task_stats["completed_tasks"]} ta
├ Bajarilmagan: {task_stats["pending_tasks"]} ta
├ Muvaffaqiyatsiz: {task_stats["failed_tasks"]} ta
└ Bajarilish darajasi: {completion_percent}%

📢 Broadcast uchun:
/xabar <matn>
"""

    telegram_send_message(
        chat_id,
        text
    )

    return {
        "ok": True,
        "route": "admin",
        "handled_by": "fastapi",
        "authorized": True,
        "stats": {
            "total_users": user_stats["total_users"],
            "added_today": user_stats["added_today"],
            "added_yesterday": user_stats["added_yesterday"],
            "active_today": user_stats["active_today"],
            "active_last_2_days": user_stats["active_last_2_days"],
            "today_tasks": task_stats["total_tasks"],
            "completed_tasks": task_stats["completed_tasks"],
            "pending_tasks": task_stats["pending_tasks"],
            "failed_tasks": task_stats["failed_tasks"],
            "completion_percent": completion_percent
        }
    }


# =========================================================
# HEALTH / API
# =========================================================

@router.get("/status")
def api_status():

    return {
        "status": "ok",
        "service": "fastapi",
        "api": True
    }


# =========================================================
# USERS
# =========================================================

@router.get("/users/{telegram_chat_id}")
def get_user(
    telegram_chat_id: int,
    _: None = Depends(verify_api_key)
):

    user = get_user_by_chat_id(
        telegram_chat_id
    )

    if not user:

        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    return {
        "ok": True,
        "user": user
    }


@router.post("/users/start")
def start_user(
    data: StartUserRequest,
    _: None = Depends(verify_api_key)
):

    existing = get_user_by_chat_id(
        data.telegram_chat_id
    )

    if existing:

        with get_connection() as conn:

            with conn.cursor(
                cursor_factory=RealDictCursor
            ) as cur:

                cur.execute(
                    """
                    UPDATE public.users
                    SET
                        last_active_date = %s,
                        first_name = %s,
                        telegram_username = %s
                    WHERE telegram_chat_id = %s
                    RETURNING *
                    """,
                    (
                        get_today(),
                        data.first_name,
                        data.telegram_username,
                        data.telegram_chat_id
                    )
                )

                user = cur.fetchone()

            conn.commit()

        return {
            "ok": True,
            "created": False,
            "user": user
        }

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                INSERT INTO public.users
                (
                    telegram_chat_id,
                    telegram_username,
                    first_name,
                    timezone,
                    state,
                    subscription_status,
                    last_active_date
                )
                VALUES
                (
                    %s,
                    %s,
                    %s,
                    'Asia/Tashkent',
                    'waiting_morning_time',
                    'trial',
                    %s
                )
                RETURNING *
                """,
                (
                    data.telegram_chat_id,
                    data.telegram_username,
                    data.first_name,
                    get_today()
                )
            )

            user = cur.fetchone()

        conn.commit()

    return {
        "ok": True,
        "created": True,
        "user": user
    }


@router.patch(
    "/users/{telegram_chat_id}/morning-time"
)
def set_morning_time(
    telegram_chat_id: int,
    data: MorningTimeRequest,
    _: None = Depends(verify_api_key)
):

    user = get_user_by_chat_id(
        telegram_chat_id
    )

    if not user:

        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    if user["state"] != "waiting_morning_time":

        raise HTTPException(
            status_code=409,
            detail="Morning time already selected"
        )

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                UPDATE public.users
                SET
                    morning_time = %s,
                    state = 'active',
                    last_active_date = %s
                WHERE telegram_chat_id = %s
                RETURNING *
                """,
                (
                    data.morning_time,
                    get_today(),
                    telegram_chat_id
                )
            )

            updated = cur.fetchone()

        conn.commit()

    return {
        "ok": True,
        "user": updated
    }


# =========================================================
# TASKS — READ
# =========================================================

@router.get(
    "/users/{telegram_chat_id}/tasks"
)
def get_tasks(
    telegram_chat_id: int,
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    _: None = Depends(verify_api_key)
):

    user = get_user_by_chat_id(
        telegram_chat_id
    )

    if not user:

        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    if start_date is None:

        start_date = get_today()

    if end_date is None:

        end_date = start_date

    if end_date < start_date:

        raise HTTPException(
            status_code=400,
            detail="end_date cannot be before start_date"
        )

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT
                    id,
                    task_date,
                    task_text,
                    status,
                    telegram_message_id,
                    created_at
                FROM public.tasks
                WHERE user_id = %s
                  AND task_date >= %s
                  AND task_date <= %s
                ORDER BY
                    task_date ASC,
                    created_at ASC
                """,
                (
                    user["id"],
                    start_date,
                    end_date
                )
            )

            tasks = cur.fetchall()

    return {
        "ok": True,
        "start_date": start_date,
        "end_date": end_date,
        "count": len(tasks),
        "tasks": tasks
    }


# =========================================================
# TASKS — CREATE
# =========================================================

def normalize_telegram_task(
    text: str
):

    return (
        text
        .lower()
        .replace("\r", "")
        .strip()
    )


def clean_telegram_task(
    text: str
):

    return re.sub(
        r"^\s*\d+[\.\)\-]\s*",
        "",
        text
    ).strip()


@router.post("/tasks")
def create_tasks(
    data: CreateTasksRequest,
    _: None = Depends(verify_api_key)
):

    user = get_user_by_chat_id(
        data.telegram_chat_id
    )

    if not user:

        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    cleaned_tasks = [
        task.strip()
        for task in data.tasks
        if task and task.strip()
    ]

    if not cleaned_tasks:

        raise HTTPException(
            status_code=400,
            detail="Task list is empty"
        )

    today = get_today()

    added = []
    duplicates = []

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT task_text
                FROM public.tasks
                WHERE user_id = %s
                  AND task_date = %s
                """,
                (
                    user["id"],
                    today
                )
            )

            existing = {
                row["task_text"].strip().lower()
                for row in cur.fetchall()
            }

            current_input = set()

            for task_text in cleaned_tasks:

                normalized = task_text.lower()

                if (
                    normalized in existing
                    or normalized in current_input
                ):

                    duplicates.append(
                        task_text
                    )

                    continue

                cur.execute(
                    """
                    INSERT INTO public.tasks
                    (
                        user_id,
                        task_date,
                        task_text,
                        status
                    )
                    VALUES
                    (
                        %s,
                        %s,
                        %s,
                        'pending'
                    )
                    RETURNING *
                    """,
                    (
                        user["id"],
                        today,
                        task_text
                    )
                )

                added.append(
                    cur.fetchone()
                )

                current_input.add(
                    normalized
                )

            cur.execute(
                """
                UPDATE public.users
                SET last_active_date = %s
                WHERE id = %s
                """,
                (
                    today,
                    user["id"]
                )
            )

        conn.commit()

    return {
        "ok": True,
        "date": today,
        "added_count": len(added),
        "duplicate_count": len(duplicates),
        "added": added,
        "duplicates": duplicates
    }


# =========================================================
# TASK STATUS — API
# =========================================================

@router.patch(
    "/users/{telegram_chat_id}/tasks/{task_id}/status"
)
def update_task_status(
    telegram_chat_id: int,
    task_id: str,
    data: TaskStatusRequest,
    _: None = Depends(verify_api_key)
):

    if data.telegram_chat_id != telegram_chat_id:

        raise HTTPException(
            status_code=400,
            detail="Telegram chat ID mismatch"
        )

    if data.status not in (
        "completed",
        "failed"
    ):

        raise HTTPException(
            status_code=400,
            detail="Status must be completed or failed"
        )

    user = get_user_by_chat_id(
        telegram_chat_id
    )

    if not user:

        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                UPDATE public.tasks
                SET status = %s
                WHERE id = %s
                  AND user_id = %s
                  AND status = 'pending'
                RETURNING *
                """,
                (
                    data.status,
                    task_id,
                    user["id"]
                )
            )

            task = cur.fetchone()

            if task:

                cur.execute(
                    """
                    UPDATE public.users
                    SET last_active_date = %s
                    WHERE id = %s
                    """,
                    (
                        get_today(),
                        user["id"]
                    )
                )

        conn.commit()

    if not task:

        raise HTTPException(
            status_code=409,
            detail="Task not found or already processed"
        )

    return {
        "ok": True,
        "task": task
    }


# =========================================================
# FINISH DAY — API
# =========================================================

@router.post(
    "/users/{telegram_chat_id}/finish-day"
)
def finish_day(
    telegram_chat_id: int,
    _: None = Depends(verify_api_key)
):

    user = get_user_by_chat_id(
        telegram_chat_id
    )

    if not user:

        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    today = get_today()

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT *
                FROM public.tasks
                WHERE user_id = %s
                  AND task_date = %s
                  AND status = 'pending'
                ORDER BY created_at ASC
                """,
                (
                    user["id"],
                    today
                )
            )

            pending_tasks = cur.fetchall()

            if not pending_tasks:

                return {
                    "ok": True,
                    "finished": False,
                    "reason": "no_pending_tasks",
                    "tasks": []
                }

            cur.execute(
                """
                UPDATE public.users
                SET
                    state = 'completed',
                    last_active_date = %s
                WHERE id = %s
                RETURNING *
                """,
                (
                    today,
                    user["id"]
                )
            )

            updated_user = cur.fetchone()

        conn.commit()

    return {
        "ok": True,
        "finished": True,
        "date": today,
        "pending_tasks": pending_tasks,
        "user": updated_user
    }


# =========================================================
# DAILY REPORT
# =========================================================

@router.get(
    "/users/{telegram_chat_id}/reports/daily"
)
def daily_report(
    telegram_chat_id: int,
    _: None = Depends(verify_api_key)
):

    user = get_user_by_chat_id(
        telegram_chat_id
    )

    if not user:

        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    today = get_today()

    yesterday = today - timedelta(days=1)

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT
                    id,
                    task_date,
                    task_text,
                    status,
                    created_at
                FROM public.tasks
                WHERE user_id = %s
                  AND task_date IN (%s, %s)
                ORDER BY
                    task_date ASC,
                    created_at ASC
                """,
                (
                    user["id"],
                    yesterday,
                    today
                )
            )

            rows = cur.fetchall()

    today_tasks = [
        task
        for task in rows
        if task["task_date"] == today
    ]

    yesterday_tasks = [
        task
        for task in rows
        if task["task_date"] == yesterday
    ]

    today_stats = calculate_stats(
        today_tasks
    )

    yesterday_stats = calculate_stats(
        yesterday_tasks
    )

    return {
        "ok": True,
        "date": today,
        "today": {
            "stats": today_stats,
            "tasks": today_tasks
        },
        "yesterday": {
            "date": yesterday,
            "stats": yesterday_stats,
            "tasks": yesterday_tasks
        },
        "difference_percent":
            today_stats["percent"]
            - yesterday_stats["percent"]
    }


# =========================================================
# WEEKLY REPORT
# =========================================================

@router.get(
    "/users/{telegram_chat_id}/reports/weekly"
)
def weekly_report(
    telegram_chat_id: int,
    _: None = Depends(verify_api_key)
):

    user = get_user_by_chat_id(
        telegram_chat_id
    )

    if not user:

        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    today = get_today()

    current_monday = (
        today - timedelta(
            days=today.weekday()
        )
    )

    start_date = current_monday - timedelta(
        days=7
    )

    end_date = current_monday - timedelta(
        days=1
    )

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT
                    id,
                    task_date,
                    task_text,
                    status,
                    created_at
                FROM public.tasks
                WHERE user_id = %s
                  AND task_date >= %s
                  AND task_date <= %s
                ORDER BY
                    task_date ASC,
                    created_at ASC
                """,
                (
                    user["id"],
                    start_date,
                    end_date
                )
            )

            tasks = cur.fetchall()

    stats = calculate_stats(tasks)

    days = []

    for i in range(7):

        current_date = (
            start_date
            + timedelta(days=i)
        )

        day_tasks = [
            task
            for task in tasks
            if task["task_date"] == current_date
        ]

        day_stats = calculate_stats(
            day_tasks
        )

        days.append({
            "date": current_date,
            "stats": day_stats
        })

    return {
        "ok": True,
        "start_date": start_date,
        "end_date": end_date,
        "stats": stats,
        "days": days,
        "tasks": tasks
    }


# =========================================================
# MONTHLY REPORT
# =========================================================

@router.get(
    "/users/{telegram_chat_id}/reports/monthly"
)
def monthly_report(
    telegram_chat_id: int,
    _: None = Depends(verify_api_key)
):

    user = get_user_by_chat_id(
        telegram_chat_id
    )

    if not user:

        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    today = get_today()

    start_date = today.replace(day=1)

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT
                    id,
                    task_date,
                    task_text,
                    status,
                    created_at
                FROM public.tasks
                WHERE user_id = %s
                  AND task_date >= %s
                  AND task_date <= %s
                ORDER BY
                    task_date ASC,
                    created_at ASC
                """,
                (
                    user["id"],
                    start_date,
                    today
                )
            )

            tasks = cur.fetchall()

    stats = calculate_stats(tasks)

    daily = {}

    for task in tasks:

        key = str(task["task_date"])

        if key not in daily:

            daily[key] = {
                "date": task["task_date"],
                "total": 0,
                "completed": 0,
                "failed": 0,
                "pending": 0
            }

        daily[key]["total"] += 1

        if task["status"] == "completed":

            daily[key]["completed"] += 1

        elif task["status"] == "failed":

            daily[key]["failed"] += 1

        else:

            daily[key]["pending"] += 1

    daily_list = list(
        daily.values()
    )

    for item in daily_list:

        item["percent"] = (
            round(
                item["completed"]
                / item["total"]
                * 100
            )
            if item["total"]
            else 0
        )

    perfect_days = sum(
        1
        for item in daily_list
        if item["percent"] == 100
    )

    best_day = (
        max(
            daily_list,
            key=lambda item: item["percent"]
        )
        if daily_list
        else None
    )

    worst_day = (
        min(
            daily_list,
            key=lambda item: item["percent"]
        )
        if daily_list
        else None
    )

    return {
        "ok": True,
        "year": today.year,
        "month": today.month,
        "start_date": start_date,
        "end_date": today,
        "stats": stats,
        "perfect_days": perfect_days,
        "best_day": best_day,
        "worst_day": worst_day,
        "daily": daily_list
    }


# =========================================================
# YEARLY REPORT
# =========================================================

@router.get(
    "/users/{telegram_chat_id}/reports/yearly"
)
def yearly_report(
    telegram_chat_id: int,
    _: None = Depends(verify_api_key)
):

    user = get_user_by_chat_id(
        telegram_chat_id
    )

    if not user:

        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    today = get_today()

    start_date = date(
        today.year,
        1,
        1
    )

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT
                    id,
                    task_date,
                    task_text,
                    status,
                    created_at
                FROM public.tasks
                WHERE user_id = %s
                  AND task_date >= %s
                  AND task_date <= %s
                ORDER BY
                    task_date ASC,
                    created_at ASC
                """,
                (
                    user["id"],
                    start_date,
                    today
                )
            )

            tasks = cur.fetchall()

    stats = calculate_stats(tasks)

    monthly = {}

    for task in tasks:

        key = (
            task["task_date"].year,
            task["task_date"].month
        )

        if key not in monthly:

            monthly[key] = []

        monthly[key].append(task)

    monthly_stats = []

    for (
        year,
        month
    ), month_tasks in sorted(
        monthly.items()
    ):

        monthly_stats.append({
            "year": year,
            "month": month,
            "stats": calculate_stats(
                month_tasks
            )
        })

    return {
        "ok": True,
        "year": today.year,
        "start_date": start_date,
        "end_date": today,
        "stats": stats,
        "monthly": monthly_stats
    }


# =========================================================
# TELEGRAM WEBHOOK — MIGRATION BRIDGE
# =========================================================

@router.post("/telegram")
def telegram_webhook(
    update: dict,
    _: None = Depends(verify_api_key)
):

    message = update.get("message") or {}

    callback_query = (
        update.get("callback_query") or {}
    )

    chat_id = (
        message.get("chat", {}).get("id")
        or
        callback_query.get("message", {})
        .get("chat", {})
        .get("id")
    )

    if not chat_id:

        return {
            "ok": True,
            "ignored": True,
            "reason": "No chat ID"
        }

    message_text = (
        message.get("text") or ""
    ).strip()

    # =====================================================
    # USER ACTIVITY
    # =====================================================

    with get_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE public.users
                SET last_active_date = %s
                WHERE telegram_chat_id = %s
                """,
                (
                    get_today(),
                    chat_id
                )
            )

        conn.commit()

    # =====================================================
    # /START
    # =====================================================

    if message_text == "/start":

        first_name = (
            message.get("from", {}).get("first_name")
            or "Do'st"
        )

        username = (
            message.get("from", {}).get("username")
            or ""
        )

        return handle_telegram_start(
            chat_id=chat_id,
            first_name=first_name,
            username=username
        )

    # =====================================================
    # /ADMIN
    # =====================================================

    if message_text == "/admin":

        return handle_telegram_admin(
            chat_id=chat_id
        )

    # =====================================================
    # /YAKUNLADIM
    # =====================================================

    if message_text == "/yakunladim":

        return handle_telegram_finish_day(
            chat_id=chat_id
        )

    # =====================================================
    # CALLBACK DATA
    # =====================================================

    callback_data = (
        callback_query.get("data") or ""
    ).strip()

    # =====================================================
    # MORNING TIME CALLBACK
    # =====================================================

    if callback_data.startswith(
        "morning_time|"
    ):

        return handle_telegram_morning_time(
            chat_id=chat_id,
            callback_query_id=callback_query.get("id"),
            callback_data=callback_data
        )

    # =====================================================
    # TASK STATUS CALLBACK
    # =====================================================

    if callback_data.startswith(
        "task_status|"
    ):

        callback_message = (
            callback_query.get("message") or {}
        )

        callback_message_id = (
            callback_message.get("message_id")
        )

        return handle_telegram_task_status(
            chat_id=chat_id,
            callback_query_id=callback_query.get("id"),
            callback_data=callback_data,
            callback_message_id=callback_message_id
        )

    # =====================================================
    # ODDIY MATN = VAZIFA
    # =====================================================

    if (
        message_text
        and not message_text.startswith("/")
    ):

        user = get_user_by_chat_id(
            chat_id
        )

        if not user:

            telegram_send_message(
                chat_id,
                "⚠️ Avval /start buyrug'ini bering."
            )

            return {
                "ok": True,
                "route": "task_text",
                "handled_by": "fastapi"
            }

        if user["state"] != "active":

            if user["state"] == "completed":

                telegram_send_message(
                    chat_id,
                    """🏁 Siz bugungi vazifalarni yakunlab bo'lgansiz.

📊 Natijani ko'rish uchun /hisobot yuboring."""
                )

            else:

                telegram_send_message(
                    chat_id,
                    "⚠️ Avval /start buyrug'ini bering."
                )

            return {
                "ok": True,
                "route": "task_text",
                "handled_by": "fastapi"
            }

        task_lines = [
            clean_telegram_task(line)
            for line in message_text.split("\n")
        ]

        task_lines = [
            task
            for task in task_lines
            if task
        ]

        if not task_lines:

            telegram_send_message(
                chat_id,
                "⚠️ Vazifa matni bosh."
            )

            return {
                "ok": True,
                "route": "task_text",
                "handled_by": "fastapi"
            }

        result = create_tasks(
            CreateTasksRequest(
                telegram_chat_id=chat_id,
                tasks=task_lines
            )
        )

        added_count = result[
            "added_count"
        ]

        duplicate_count = result[
            "duplicate_count"
        ]

        response_text = ""

        if added_count > 0:

            response_text += (
                f"🎉 {added_count} ta yangi "
                f"vazifa qabul qilindi!\n\n"
            )

        if duplicate_count > 0:

            response_text += (
                f"🔄 {duplicate_count} ta vazifa "
                f"oldin qo'shilgan.\n\n"
            )

        response_text += (
            "🤲 Kuningiz barakali o'tsin!"
        )

        if added_count > 0:

            response_text += (
                "\n\n🏁 Kuningizni yakunlaganingizda "
                "/yakunladim yuboring."
            )

        telegram_send_message(
            chat_id,
            response_text
        )

        return {
            "ok": True,
            "route": "task_text",
            "handled_by": "fastapi",
            "added_count": added_count,
            "duplicate_count": duplicate_count
        }

    # =====================================================
    # QOLGAN ACTIONLAR — NODE
    #
    # /xabar
    # /hisobot
    # /haftalik
    # /oylik
    # /yillik
    # va boshqa eski actionlar
    # =====================================================

    if not LEGACY_BACKEND_URL:

        raise HTTPException(
            status_code=500,
            detail="LEGACY_BACKEND_URL is not configured"
        )

    response = requests.post(
        f"{LEGACY_BACKEND_URL}/api/telegram",
        json=update,
        timeout=30
    )

    return response.json()
