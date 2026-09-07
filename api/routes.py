import os
import re
from datetime import date, datetime, timedelta
from typing import Optional

import requests
from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel, Field
from psycopg2.extras import RealDictCursor

from database import get_connection


# =========================================================
# ROUTER
# =========================================================

router = APIRouter(prefix="/api")


# =========================================================
# CONFIG
# =========================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
LEGACY_BACKEND_URL = os.getenv("LEGACY_BACKEND_URL")

ADMIN_CHAT_ID = "8908985083"

TIMEZONE = "Asia/Tashkent"

API_KEY = os.getenv("API_KEY")


# =========================================================
# API KEY
# =========================================================

def verify_api_key(
    x_api_key: Optional[str] = Header(default=None)
):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid API key"
        )

    return None


# =========================================================
# PYDANTIC MODELS
# =========================================================

class StartUserRequest(BaseModel):
    chat_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None


class MorningTimeRequest(BaseModel):
    chat_id: int
    time: str = Field(
        pattern=r"^(0[2-9]|10):00$"
    )


class CreateTasksRequest(BaseModel):
    chat_id: int
    text: str


class TaskStatusRequest(BaseModel):
    chat_id: int
    task_id: str
    status: str


# =========================================================
# DATE / TIME HELPERS
# =========================================================

def get_today() -> date:
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


def get_tashkent_datetime() -> datetime:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    CURRENT_TIMESTAMP
                    AT TIME ZONE 'Asia/Tashkent'
                """
            )

            return cur.fetchone()[0]


def get_yesterday() -> date:
    return get_today() - timedelta(days=1)


def format_uz_date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


# =========================================================
# USER HELPERS
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
                LIMIT 1
                """,
                (chat_id,)
            )

            return cur.fetchone()


def is_admin(chat_id: int) -> bool:
    return str(chat_id) == str(ADMIN_CHAT_ID)


def update_user_activity(chat_id: int):

    today = get_today()

    with get_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE public.users
                SET last_active_date = %s
                WHERE telegram_chat_id = %s
                """,
                (
                    today,
                    chat_id
                )
            )

        conn.commit()


# =========================================================
# TASK HELPERS
# =========================================================

def clean_task_text(text: str) -> str:

    text = text.strip()

    # Remove numbering:
    # 1. task
    # 1) task
    # 1 - task
    # - task
    # • task

    text = re.sub(
        r"^\s*(?:\d+[\.\)]|\-|\•)\s*",
        "",
        text
    )

    return text.strip()


def normalize_task(text: str) -> str:

    text = clean_task_text(text)

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip().lower()


def progress_bar(
    completed: int,
    total: int,
    length: int = 10
) -> str:

    if total <= 0:
        return "░" * length

    percent = completed / total

    filled = round(percent * length)

    return (
        "█" * filled
        + "░" * (length - filled)
    )


def get_motivation(percent: int) -> str:

    if percent >= 100:
        return "🔥 Ajoyib! Bugungi rejangiz to‘liq bajarildi!"

    if percent >= 80:
        return "💪 Juda yaxshi! Oxirigacha yetkazib qo‘ying!"

    if percent >= 50:
        return "🚀 Zo‘r ketayapsiz! Yana ozgina qoldi."

    if percent > 0:
        return "🌱 Boshladingiz — davom eting!"

    return "💡 Bugun kichik qadamdan boshlang."


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
            detail=(
                "Telegram sendMessage failed: "
                f"{telegram_error}"
            )
        )

    return response.json()


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
            detail=(
                "Telegram sendMessage failed: "
                f"{telegram_error}"
            )
        )

    return response.json()


def telegram_answer_callback(
    callback_query_id: str,
    text: Optional[str] = None
):

    if not TELEGRAM_TOKEN:
        return

    payload = {
        "callback_query_id": callback_query_id
    }

    if text:
        payload["text"] = text

    requests.post(
        (
            f"https://api.telegram.org/"
            f"bot{TELEGRAM_TOKEN}/answerCallbackQuery"
        ),
        json=payload,
        timeout=10
    )


def telegram_delete_message(
    chat_id: int,
    message_id: int
):

    if not TELEGRAM_TOKEN:
        return

    requests.post(
        (
            f"https://api.telegram.org/"
            f"bot{TELEGRAM_TOKEN}/deleteMessage"
        ),
        json={
            "chat_id": chat_id,
            "message_id": message_id
        },
        timeout=10
    )


# =========================================================
# MORNING TIME KEYBOARD
# =========================================================

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
        {
            "inline_keyboard": keyboard
        }
    )


# =========================================================
# TASK STATUS KEYBOARD
# =========================================================

def telegram_send_task_status_keyboard(
    chat_id: int,
    task: dict,
    number: int
):

    keyboard = [
        [
            {
                "text": "✅ Bajarildi",
                "callback_data": (
                    f"task_status|{task['id']}|completed"
                )
            },
            {
                "text": "❌ Bajarilmadi",
                "callback_data": (
                    f"task_status|{task['id']}|failed"
                )
            }
        ]
    ]

    return telegram_send_message_with_keyboard(
        chat_id,
        f"{number}. {task['task_text']}",
        {
            "inline_keyboard": keyboard
        }
    )


# =========================================================
# START
# =========================================================

def handle_telegram_start(
    chat_id: int,
    username: Optional[str],
    first_name: Optional[str]
):

    first_name = (
        first_name
        or "Do'st"
    )

    username = username or ""

    user = get_user_by_chat_id(chat_id)

    # -----------------------------------------------------
    # NEW USER
    # -----------------------------------------------------

    if not user:

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
                        %s,
                        'waiting_morning_time',
                        'trial',
                        %s
                    )
                    """,
                    (
                        chat_id,
                        username,
                        first_name,
                        TIMEZONE,
                        get_today()
                    )
                )

            conn.commit()

        telegram_send_morning_keyboard(
            chat_id,
            first_name
        )

        return {
            "ok": True,
            "route": "start",
            "new_user": True
        }

    # -----------------------------------------------------
    # EXISTING USER
    # -----------------------------------------------------

    with get_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE public.users
                SET
                    first_name = %s,
                    telegram_username = %s,
                    last_active_date = %s,
                    state = CASE
                        WHEN state = 'blocked'
                        THEN 'active'
                        ELSE state
                    END
                WHERE telegram_chat_id = %s
                """,
                (
                    first_name,
                    username,
                    get_today(),
                    chat_id
                )
            )

        conn.commit()

    telegram_send_message(
        chat_id,
        f"""👋 Assalomu alaykum, {first_name}!

Siz allaqachon ro'yxatdan o'tgansiz. ✅

📋 Vazifalaringizni yuborishingiz mumkin."""
    )

    return {
        "ok": True,
        "route": "start",
        "existing_user": True
    }


# =========================================================
# MORNING TIME
# =========================================================

def handle_morning_time(
    chat_id: int,
    time_value: str,
    callback_query_id: Optional[str] = None
):

    if not re.match(
        r"^(0[2-9]|10):00$",
        time_value
    ):

        if callback_query_id:
            telegram_answer_callback(
                callback_query_id,
                "Noto'g'ri vaqt."
            )

        return {
            "ok": False,
            "error": "Invalid morning time"
        }

    user = get_user_by_chat_id(chat_id)

    if not user:

        return {
            "ok": False,
            "error": "User not found"
        }

    with get_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE public.users
                SET
                    morning_time = %s,
                    state = 'active',
                    last_active_date = %s
                WHERE telegram_chat_id = %s
                """,
                (
                    time_value,
                    get_today(),
                    chat_id
                )
            )

        conn.commit()

    if callback_query_id:

        telegram_answer_callback(
            callback_query_id,
            "Vaqt saqlandi ✅"
        )

    telegram_send_message(
        chat_id,
        f"""✅ Ajoyib!

🕐 Ertalabki rejalashtirish vaqtingiz:
{time_value}

Endi har kuni shu vaqtda sizga eslatma keladi.

✍️ Bugungi vazifalaringizni yuborishingiz mumkin."""
    )

    return {
        "ok": True,
        "route": "morning_time",
        "time": time_value
    }


# =========================================================
# CREATE TASKS
# =========================================================

def handle_create_tasks(
    chat_id: int,
    text: str
):

    user = get_user_by_chat_id(chat_id)

    if not user:

        telegram_send_message(
            chat_id,
            "⚠️ Avval /start buyrug'ini bosing."
        )

        return {
            "ok": False,
            "error": "User not found"
        }

    if user["state"] == "blocked":

        telegram_send_message(
            chat_id,
            "⚠️ Botdan foydalanish uchun /start buyrug'ini bosing."
        )

        return {
            "ok": False,
            "error": "User blocked"
        }

    today = get_today()

    # -----------------------------------------------------
    # IF DAY ALREADY FINISHED
    # -----------------------------------------------------

    if user["state"] == "completed":

        telegram_send_message(
            chat_id,
            """✅ Bugungi kun allaqachon yakunlangan.

Yangi kunni boshlash uchun ertaga vazifalaringizni yuboring."""
        )

        return {
            "ok": False,
            "error": "Day already completed"
        }

    # -----------------------------------------------------
    # SPLIT TASKS
    # -----------------------------------------------------

    raw_tasks = re.split(
        r"\r?\n+",
        text.strip()
    )

    tasks = []

    for raw_task in raw_tasks:

        cleaned = clean_task_text(raw_task)

        if not cleaned:
            continue

        tasks.append(cleaned)

    if not tasks:

        telegram_send_message(
            chat_id,
            "⚠️ Vazifa matni bo'sh."
        )

        return {
            "ok": False,
            "error": "Empty task"
        }

    # -----------------------------------------------------
    # EXISTING TASKS
    # -----------------------------------------------------

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

            existing = cur.fetchall()

    existing_normalized = {
        normalize_task(row["task_text"])
        for row in existing
    }

    # -----------------------------------------------------
    # INSERT
    # -----------------------------------------------------

    added = []
    duplicates = []

    with get_connection() as conn:

        with conn.cursor() as cur:

            for task_text in tasks:

                normalized = normalize_task(task_text)

                if normalized in existing_normalized:

                    duplicates.append(task_text)
                    continue

                cur.execute(
                    """
                    INSERT INTO public.tasks (
                        user_id,
                        task_text,
                        task_date,
                        status
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        'pending'
                    )
                    RETURNING id
                    """,
                    (
                        user["id"],
                        task_text,
                        today
                    )
                )

                task_id = cur.fetchone()[0]

                added.append({
                    "id": task_id,
                    "task_text": task_text
                })

                existing_normalized.add(normalized)

        conn.commit()

    # -----------------------------------------------------
    # RESPONSE
    # -----------------------------------------------------

    if added:

        lines = [
            "✅ Vazifalar qo'shildi!",
            ""
        ]

        for index, task in enumerate(
            added,
            start=1
        ):

            lines.append(
                f"{index}. {task['task_text']}"
            )

        lines.extend([
            "",
            f"📋 Jami: {len(added)} ta vazifa"
        ])

        if duplicates:

            lines.extend([
                f"⚠️ {len(duplicates)} ta vazifa takroriy bo'lgani uchun qo'shilmadi."
            ])

        telegram_send_message(
            chat_id,
            "\n".join(lines)
        )

    else:

        telegram_send_message(
            chat_id,
            "⚠️ Yangi vazifalar qo'shilmadi."
        )

    return {
        "ok": True,
        "route": "create_tasks",
        "added": len(added),
        "duplicates": len(duplicates)
    }


# =========================================================
# FINISH DAY
# =========================================================

def handle_finish_day(
    chat_id: int
):

    user = get_user_by_chat_id(chat_id)

    if not user:

        telegram_send_message(
            chat_id,
            "⚠️ Avval /start buyrug'ini bosing."
        )

        return {
            "ok": False,
            "error": "User not found"
        }

    today = get_today()

    # -----------------------------------------------------
    # ALREADY COMPLETED
    # -----------------------------------------------------

    if user["state"] == "completed":

        telegram_send_message(
            chat_id,
            """✅ Bugungi kun allaqachon yakunlangan.

Ertaga yangi kunni boshlaymiz! 🌅"""
        )

        return {
            "ok": True,
            "already_completed": True
        }

    # -----------------------------------------------------
    # GET PENDING TASKS
    # -----------------------------------------------------

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT
                    id,
                    task_text,
                    status
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
            """🌙 Bugungi kun uchun bajarilmagan vazifalar yo'q.

🎉 Demak, barcha vazifalar bajarilgan!

Ajoyib ish!"""
        )

        with get_connection() as conn:

            with conn.cursor() as cur:

                cur.execute(
                    """
                    UPDATE public.users
                    SET state = 'completed'
                    WHERE id = %s
                    """,
                    (user["id"],)
                )

            conn.commit()

        return {
            "ok": True,
            "all_completed": True
        }

    # -----------------------------------------------------
    # MARK DAY AS COMPLETED
    # -----------------------------------------------------

    with get_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE public.users
                SET state = 'completed'
                WHERE id = %s
                """,
                (user["id"],)
            )

        conn.commit()

    # -----------------------------------------------------
    # SEND HEADER
    # -----------------------------------------------------

    telegram_send_message(
        chat_id,
        f"""🌙 Kun yakunlandi!

📅 {format_uz_date(today)}

Quyidagi vazifalar hali belgilanmagan:

👇 Har bir vazifani alohida belgilang."""
    )

    # -----------------------------------------------------
    # SEND TASKS
    # -----------------------------------------------------

    for index, task in enumerate(
        pending_tasks,
        start=1
    ):

        telegram_send_task_status_keyboard(
            chat_id,
            task,
            index
        )

    return {
        "ok": True,
        "route": "finish_day",
        "pending": len(pending_tasks)
    }


# =========================================================
# TASK STATUS
# =========================================================

def handle_task_status(
    chat_id: int,
    task_id: str,
    status: str,
    callback_query_id: Optional[str] = None,
    message_id: Optional[int] = None
):

    if status not in (
        "completed",
        "failed"
    ):

        if callback_query_id:

            telegram_answer_callback(
                callback_query_id,
                "Noto'g'ri status."
            )

        return {
            "ok": False,
            "error": "Invalid status"
        }

    # -----------------------------------------------------
    # ATOMIC UPDATE
    # -----------------------------------------------------

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                UPDATE public.tasks
                SET status = %s
                WHERE id = %s
                  AND status = 'pending'
                RETURNING *
                """,
                (
                    status,
                    task_id
                )
            )

            task = cur.fetchone()

        conn.commit()

    if not task:

        if callback_query_id:

            telegram_answer_callback(
                callback_query_id,
                "Bu vazifa allaqachon belgilangan."
            )

        return {
            "ok": True,
            "already_processed": True
        }

    # -----------------------------------------------------
    # CALLBACK ANSWER
    # -----------------------------------------------------

    if callback_query_id:

        if status == "completed":

            telegram_answer_callback(
                callback_query_id,
                "Bajarildi ✅"
            )

        else:

            telegram_answer_callback(
                callback_query_id,
                "Bajarilmadi ❌"
            )

    # -----------------------------------------------------
    # DELETE OLD MESSAGE
    # -----------------------------------------------------

    if message_id:

        telegram_delete_message(
            chat_id,
            message_id
        )

    # -----------------------------------------------------
    # GET USER
    # -----------------------------------------------------

    user = get_user_by_chat_id(chat_id)

    if not user:

        return {
            "ok": True,
            "status": status
        }

    today = get_today()

    # -----------------------------------------------------
    # COUNT PENDING
    # -----------------------------------------------------

    with get_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT COUNT(*)
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

            pending_count = cur.fetchone()[0]

    # -----------------------------------------------------
    # ALL COMPLETED CLAIM
    # -----------------------------------------------------

    if pending_count == 0:

        with get_connection() as conn:

            with conn.cursor() as cur:

                cur.execute(
                    """
                    UPDATE public.users
                    SET last_completion_notified_date = %s
                    WHERE id = %s
                      AND last_completion_notified_date
                          IS DISTINCT FROM %s
                    RETURNING id
                    """,
                    (
                        today,
                        user["id"],
                        today
                    )
                )

                claimant = cur.fetchone()

            conn.commit()

        # Only one request sends final message
        if claimant:

            telegram_send_message(
                chat_id,
                """🎉 Barcha vazifalar belgilandi!

Bugungi kuningizni ajoyib yakunladingiz. 💪

🌙 Yaxshi dam oling! Ertaga yana davom etamiz."""
            )

    return {
        "ok": True,
        "status": status,
        "pending": pending_count
    }


# =========================================================
# DAILY REPORT
# =========================================================

def get_daily_report_data(
    user_id,
    report_date: date
):

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (
                        WHERE status = 'completed'
                    ) AS completed,
                    COUNT(*) FILTER (
                        WHERE status = 'pending'
                    ) AS pending,
                    COUNT(*) FILTER (
                        WHERE status = 'failed'
                    ) AS failed
                FROM public.tasks
                WHERE user_id = %s
                  AND task_date = %s
                """,
                (
                    user_id,
                    report_date
                )
            )

            return cur.fetchone()


def handle_daily_report(
    chat_id: int
):

    user = get_user_by_chat_id(chat_id)

    if not user:

        telegram_send_message(
            chat_id,
            "⚠️ Avval /start buyrug'ini bosing."
        )

        return {
            "ok": False
        }

    today = get_today()

    stats = get_daily_report_data(
        user["id"],
        today
    )

    total = int(stats["total"] or 0)
    completed = int(stats["completed"] or 0)
    pending = int(stats["pending"] or 0)
    failed = int(stats["failed"] or 0)

    percent = (
        round(completed / total * 100)
        if total
        else 0
    )

    bar = progress_bar(
        completed,
        total
    )

    text = f"""📊 BUGUNGI HISOBOT

📅 {format_uz_date(today)}

━━━━━━━━━━━━━━

📋 Jami: {total} ta

✅ Bajarilgan: {completed} ta
⏳ Bajarilmagan: {pending} ta
❌ Muvaffaqiyatsiz: {failed} ta

📈 Natija: {percent}%

{bar}

{get_motivation(percent)}"""

    telegram_send_message(
        chat_id,
        text
    )

    return {
        "ok": True,
        "report": "daily"
    }


# =========================================================
# WEEKLY REPORT
# =========================================================

def handle_weekly_report(
    chat_id: int
):

    user = get_user_by_chat_id(chat_id)

    if not user:

        telegram_send_message(
            chat_id,
            "⚠️ Avval /start buyrug'ini bosing."
        )

        return {
            "ok": False
        }

    today = get_today()
    start_date = today - timedelta(days=6)

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (
                        WHERE status = 'completed'
                    ) AS completed,
                    COUNT(*) FILTER (
                        WHERE status = 'pending'
                    ) AS pending,
                    COUNT(*) FILTER (
                        WHERE status = 'failed'
                    ) AS failed
                FROM public.tasks
                WHERE user_id = %s
                  AND task_date BETWEEN %s AND %s
                """,
                (
                    user["id"],
                    start_date,
                    today
                )
            )

            stats = cur.fetchone()

    total = int(stats["total"] or 0)
    completed = int(stats["completed"] or 0)
    pending = int(stats["pending"] or 0)
    failed = int(stats["failed"] or 0)

    percent = (
        round(completed / total * 100)
        if total
        else 0
    )

    telegram_send_message(
        chat_id,
        f"""📊 HAFTALIK HISOBOT

📅 {format_uz_date(start_date)} — {format_uz_date(today)}

━━━━━━━━━━━━━━

📋 Jami: {total} ta
✅ Bajarilgan: {completed} ta
⏳ Bajarilmagan: {pending} ta
❌ Muvaffaqiyatsiz: {failed} ta

📈 Bajarilish darajasi: {percent}%

{progress_bar(completed, total)}

{get_motivation(percent)}"""
    )

    return {
        "ok": True,
        "report": "weekly"
    }


# =========================================================
# MONTHLY REPORT
# =========================================================

def handle_monthly_report(
    chat_id: int
):

    user = get_user_by_chat_id(chat_id)

    if not user:

        telegram_send_message(
            chat_id,
            "⚠️ Avval /start buyrug'ini bosing."
        )

        return {
            "ok": False
        }

    today = get_today()

    start_date = today.replace(
        day=1
    )

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (
                        WHERE status = 'completed'
                    ) AS completed,
                    COUNT(*) FILTER (
                        WHERE status = 'pending'
                    ) AS pending,
                    COUNT(*) FILTER (
                        WHERE status = 'failed'
                    ) AS failed
                FROM public.tasks
                WHERE user_id = %s
                  AND task_date BETWEEN %s AND %s
                """,
                (
                    user["id"],
                    start_date,
                    today
                )
            )

            stats = cur.fetchone()

    total = int(stats["total"] or 0)
    completed = int(stats["completed"] or 0)
    pending = int(stats["pending"] or 0)
    failed = int(stats["failed"] or 0)

    percent = (
        round(completed / total * 100)
        if total
        else 0
    )

    telegram_send_message(
        chat_id,
        f"""📊 OYLIK HISOBOT

📅 {format_uz_date(start_date)} — {format_uz_date(today)}

━━━━━━━━━━━━━━

📋 Jami: {total} ta
✅ Bajarilgan: {completed} ta
⏳ Bajarilmagan: {pending} ta
❌ Muvaffaqiyatsiz: {failed} ta

📈 Bajarilish darajasi: {percent}%

{progress_bar(completed, total)}

{get_motivation(percent)}"""
    )

    return {
        "ok": True,
        "report": "monthly"
    }


# =========================================================
# YEARLY REPORT
# =========================================================

def handle_yearly_report(
    chat_id: int
):

    user = get_user_by_chat_id(chat_id)

    if not user:

        telegram_send_message(
            chat_id,
            "⚠️ Avval /start buyrug'ini bosing."
        )

        return {
            "ok": False
        }

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
                    COUNT(*) AS total,
                    COUNT(*) FILTER (
                        WHERE status = 'completed'
                    ) AS completed,
                    COUNT(*) FILTER (
                        WHERE status = 'pending'
                    ) AS pending,
                    COUNT(*) FILTER (
                        WHERE status = 'failed'
                    ) AS failed
                FROM public.tasks
                WHERE user_id = %s
                  AND task_date BETWEEN %s AND %s
                """,
                (
                    user["id"],
                    start_date,
                    today
                )
            )

            stats = cur.fetchone()

    total = int(stats["total"] or 0)
    completed = int(stats["completed"] or 0)
    pending = int(stats["pending"] or 0)
    failed = int(stats["failed"] or 0)

    percent = (
        round(completed / total * 100)
        if total
        else 0
    )

    telegram_send_message(
        chat_id,
        f"""📊 YILLIK HISOBOT

📅 {today.year}-yil

━━━━━━━━━━━━━━

📋 Jami: {total} ta
✅ Bajarilgan: {completed} ta
⏳ Bajarilmagan: {pending} ta
❌ Muvaffaqiyatsiz: {failed} ta

📈 Bajarilish darajasi: {percent}%

{progress_bar(completed, total)}

{get_motivation(percent)}"""
    )

    return {
        "ok": True,
        "report": "yearly"
    }


# =========================================================
# ADMIN
# =========================================================

def handle_admin(
    chat_id: int
):

    if not is_admin(chat_id):

        telegram_send_message(
            chat_id,
            "⛔ Sizda admin huquqi yo'q."
        )

        return {
            "ok": False,
            "error": "Not admin"
        }

    today = get_today()
    yesterday = today - timedelta(days=1)

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            # USERS
            cur.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (
                        WHERE (
                            created_at
                            AT TIME ZONE 'Asia/Tashkent'
                        )::date = %s
                    ) AS today_new,
                    COUNT(*) FILTER (
                        WHERE (
                            created_at
                            AT TIME ZONE 'Asia/Tashkent'
                        )::date = %s
                    ) AS yesterday_new,
                    COUNT(*) FILTER (
                        WHERE last_active_date = %s
                    ) AS today_active,
                    COUNT(*) FILTER (
                        WHERE last_active_date >= %s
                    ) AS last_two_days
                FROM public.users
                """,
                (
                    today,
                    yesterday,
                    today,
                    today - timedelta(days=1)
                )
            )

            users = cur.fetchone()

            # TASKS
            cur.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (
                        WHERE status = 'completed'
                    ) AS completed,
                    COUNT(*) FILTER (
                        WHERE status = 'pending'
                    ) AS pending,
                    COUNT(*) FILTER (
                        WHERE status = 'failed'
                    ) AS failed
                FROM public.tasks
                WHERE task_date = %s
                """,
                (today,)
            )

            tasks = cur.fetchone()

    total_users = int(users["total"] or 0)
    today_new = int(users["today_new"] or 0)
    yesterday_new = int(users["yesterday_new"] or 0)
    today_active = int(users["today_active"] or 0)
    last_two_days = int(users["last_two_days"] or 0)

    total_tasks = int(tasks["total"] or 0)
    completed = int(tasks["completed"] or 0)
    pending = int(tasks["pending"] or 0)
    failed = int(tasks["failed"] or 0)

    percent = (
        round(completed / total_tasks * 100)
        if total_tasks
        else 0
    )

    text = f"""👑 ADMIN PANEL

📅 {format_uz_date(today)}

━━━━━━━━━━━━━━━━

👥 FOYDALANUVCHILAR

├ Jami: {total_users} ta
├ Bugun qo'shilgan: {today_new} ta
└ Kecha qo'shilgan: {yesterday_new} ta

📈 FAOLLIK

├ Bugun foydalangan: {today_active} ta
└ Oxirgi 2 kunda foydalangan: {last_two_days} ta

📋 BUGUNGI VAZIFALAR

├ Jami: {total_tasks} ta
├ Bajarilgan: {completed} ta
├ Bajarilmagan: {pending} ta
├ Muvaffaqiyatsiz: {failed} ta
└ Bajarilish darajasi: {percent}%

📢 Broadcast uchun:

/xabar <matn>"""

    telegram_send_message(
        chat_id,
        text
    )

    return {
        "ok": True,
        "route": "admin"
    }


# =========================================================
# BROADCAST
# =========================================================

def handle_broadcast(
    chat_id: int,
    text: str
):

    if not is_admin(chat_id):

        telegram_send_message(
            chat_id,
            "⛔ Sizda admin huquqi yo'q."
        )

        return {
            "ok": False
        }

    message = re.sub(
        r"^/xabar\s*",
        "",
        text,
        flags=re.IGNORECASE
    ).strip()

    if not message:

        telegram_send_message(
            chat_id,
            "⚠️ Foydalanish:\n\n/xabar <matn>"
        )

        return {
            "ok": False
        }

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT telegram_chat_id
                FROM public.users
                WHERE telegram_chat_id IS NOT NULL
                  AND state != 'blocked'
                """
            )

            users = cur.fetchall()

    sent = 0
    failed = 0

    for user in users:

        target_chat_id = user["telegram_chat_id"]

        try:

            telegram_send_message(
                target_chat_id,
                message
            )

            sent += 1

        except Exception:

            failed += 1

    telegram_send_message(
        chat_id,
        f"""📢 Broadcast yakunlandi.

✅ Yuborildi: {sent} ta
❌ Xatolik: {failed} ta"""
    )

    return {
        "ok": True,
        "sent": sent,
        "failed": failed
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
                            %s -
                            (
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

        first_name = (
            user["first_name"]
            or "Do'st"
        )

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
                and
                "bot was blocked by the user"
                in error_text
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


# =========================================================
# TELEGRAM WEBHOOK
# =========================================================

@router.post("/telegram")
def telegram_webhook(
    update: dict
):

    started_at = datetime.utcnow()

    try:

        message = update.get("message") or {}
        callback_query = update.get(
            "callback_query"
        ) or {}

        callback_message = (
            callback_query.get("message")
            or {}
        )

        # -------------------------------------------------
        # MESSAGE
        # -------------------------------------------------

        chat = (
            message.get("chat")
            or callback_message.get("chat")
            or {}
        )

        chat_id = chat.get("id")

        if not chat_id:

            return {
                "ok": True,
                "route": "ignored"
            }

        text = (
            message.get("text")
            or ""
        ).strip()

        # -------------------------------------------------
        # USER INFO
        # -------------------------------------------------

        from_user = (
            message.get("from")
            or callback_query.get("from")
            or {}
        )

        username = from_user.get(
            "username"
        )

        first_name = from_user.get(
            "first_name"
        )

        # -------------------------------------------------
        # UPDATE ACTIVITY
        # -------------------------------------------------

        update_user_activity(
            chat_id
        )

        # =================================================
        # /START
        # =================================================

        if text == "/start":

            return handle_telegram_start(
                chat_id,
                username,
                first_name
            )

        # =================================================
        # /ADMIN
        # =================================================

        if text == "/admin":

            return handle_admin(
                chat_id
            )

        # =================================================
        # /YAKUNLADIM
        # =================================================

        if text == "/yakunladim":

            return handle_finish_day(
                chat_id
            )

        # =================================================
        # REPORTS
        # =================================================

        if text == "/hisobot":

            return handle_daily_report(
                chat_id
            )

        if text == "/haftalik":

            return handle_weekly_report(
                chat_id
            )

        if text == "/oylik":

            return handle_monthly_report(
                chat_id
            )

        if text == "/yillik":

            return handle_yearly_report(
                chat_id
            )

        # =================================================
        # BROADCAST
        # =================================================

        if text.startswith("/xabar"):

            return handle_broadcast(
                chat_id,
                text
            )

        # =================================================
        # CALLBACK
        # =================================================

        if callback_query:

            callback_query_id = (
                callback_query.get("id")
            )

            callback_data = (
                callback_query.get("data")
                or ""
            )

            callback_message_id = (
                callback_message.get("message_id")
            )

            # ---------------------------------------------
            # MORNING TIME
            # ---------------------------------------------

            if callback_data.startswith(
                "morning_time|"
            ):

                parts = callback_data.split(
                    "|",
                    1
                )

                if len(parts) == 2:

                    return handle_morning_time(
                        chat_id,
                        parts[1],
                        callback_query_id
                    )

            # ---------------------------------------------
            # TASK STATUS
            # ---------------------------------------------

            if callback_data.startswith(
                "task_status|"
            ):

                parts = callback_data.split(
                    "|"
                )

                if len(parts) == 3:

                    return handle_task_status(
                        chat_id,
                        parts[1],
                        parts[2],
                        callback_query_id,
                        callback_message_id
                    )

            # Unknown callback

            if callback_query_id:

                telegram_answer_callback(
                    callback_query_id
                )

            return {
                "ok": True,
                "route": "callback_ignored"
            }

        # =================================================
        # COMMANDS
        # =================================================

        if text.startswith("/"):

            telegram_send_message(
                chat_id,
                """⚠️ Bu buyruq mavjud emas.

Mavjud buyruqlar:

/start
/yakunladim
/hisobot
/haftalik
/oylik
/yillik"""
            )

            return {
                "ok": True,
                "route": "unknown_command"
            }

        # =================================================
        # ORDINARY TEXT → CREATE TASKS
        # =================================================

        if text:

            return handle_create_tasks(
                chat_id,
                text
            )

        # =================================================
        # LEGACY
        # =================================================

        return proxy_to_legacy(
            update
        )

    except HTTPException:

        raise

    except Exception as e:

        print(
            "TELEGRAM WEBHOOK ERROR:",
            repr(e)
        )

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

    finally:

        duration = (
            datetime.utcnow()
            - started_at
        ).total_seconds()

        print(
            f"Telegram webhook: "
            f"{duration:.3f}s"
        )


# =========================================================
# REMINDER ENDPOINT
# =========================================================

@router.post("/reminders/run")
def run_reminders(
    _: None = Depends(verify_api_key)
):

    return handle_reminders()


# =========================================================
# SIMPLE STATUS
# =========================================================

@router.get("/status")
def status():

    return {
        "ok": True,
        "service": "fastapi",
        "timezone": TIMEZONE
    }


# =========================================================
# USERS
# =========================================================

@router.get("/users")
def get_users(
    _: None = Depends(verify_api_key)
):

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT *
                FROM public.users
                ORDER BY created_at DESC
                """
            )

            users = cur.fetchall()

    return {
        "ok": True,
        "count": len(users),
        "users": users
    }


# =========================================================
# START API
# =========================================================

@router.post("/start")
def start_api(
    data: StartUserRequest
):

    return handle_telegram_start(
        data.chat_id,
        data.username,
        data.first_name
    )


# =========================================================
# MORNING TIME API
# =========================================================

@router.post("/morning-time")
def morning_time_api(
    data: MorningTimeRequest
):

    return handle_morning_time(
        data.chat_id,
        data.time
    )


# =========================================================
# TASKS API
# =========================================================

@router.post("/tasks")
def tasks_api(
    data: CreateTasksRequest
):

    return handle_create_tasks(
        data.chat_id,
        data.text
    )


# =========================================================
# TASK STATUS API
# =========================================================

@router.post("/tasks/status")
def task_status_api(
    data: TaskStatusRequest
):

    return handle_task_status(
        data.chat_id,
        data.task_id,
        data.status
    )


# =========================================================
# FINISH DAY API
# =========================================================

@router.post("/finish")
def finish_api(
    chat_id: int
):

    return handle_finish_day(
        chat_id
    )


# =========================================================
# REPORT API
# =========================================================

@router.get("/reports/daily/{chat_id}")
def daily_report_api(
    chat_id: int
):

    return handle_daily_report(
        chat_id
    )


@router.get("/reports/weekly/{chat_id}")
def weekly_report_api(
    chat_id: int
):

    return handle_weekly_report(
        chat_id
    )


@router.get("/reports/monthly/{chat_id}")
def monthly_report_api(
    chat_id: int
):

    return handle_monthly_report(
        chat_id
    )


@router.get("/reports/yearly/{chat_id}")
def yearly_report_api(
    chat_id: int
):

    return handle_yearly_report(
        chat_id
    )


# =========================================================
# LEGACY BACKEND BRIDGE
# =========================================================

def proxy_to_legacy(
    update: dict
):

    if not LEGACY_BACKEND_URL:

        raise HTTPException(
            status_code=500,
            detail=(
                "LEGACY_BACKEND_URL "
                "is not configured"
            )
        )

    response = requests.post(
        f"{LEGACY_BACKEND_URL}/api/telegram",
        json=update,
        timeout=30
    )

    if not response.ok:

        raise HTTPException(
            status_code=response.status_code,
            detail=response.text
        )

    return response.json()
