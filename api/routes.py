import os
import re
import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

import httpx

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException
)

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

LEGACY_BACKEND_URL = os.getenv(
    "LEGACY_BACKEND_URL"
)

ADMIN_CHAT_ID = "8908985083"

TIMEZONE = "Asia/Tashkent"

TZ = ZoneInfo(TIMEZONE)

API_KEY = os.getenv("API_KEY")


# =========================================================
# SHARED ASYNC HTTP CLIENT
#
# Bitta doimiy client - TCP connection va TLS session
# Telegram bilan qayta ishlatiladi (keep-alive).
# Har chaqiriqda yangi client yaratish ham xarajat edi.
# =========================================================

_http_client: Optional[httpx.AsyncClient] = None


def get_http_client() -> httpx.AsyncClient:

    global _http_client

    if _http_client is None:

        _http_client = httpx.AsyncClient(
            timeout=15,
            limits=httpx.Limits(
                max_connections=50,
                max_keepalive_connections=20
            )
        )

    return _http_client


async def close_http_client():

    global _http_client

    if _http_client is not None:

        await _http_client.aclose()

        _http_client = None


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
# MODELS
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
# DATE HELPERS
#
# Ilgari get_today() har safar DB ga borib CURRENT_TIMESTAMP
# AT TIME ZONE so'rar edi - bu ekstra round-trip edi va bitta
# request ichida 3-5 marta chaqirilardi. Endi Python ichida,
# zoneinfo bilan, DB'siz hisoblanadi.
# =========================================================

def get_today() -> date:

    return datetime.now(TZ).date()


def get_yesterday_date() -> date:

    return get_today() - timedelta(days=1)


def format_uz_date(value) -> str:

    if isinstance(value, str):

        value = date.fromisoformat(value)

    months = [
        "yanvar",
        "fevral",
        "mart",
        "aprel",
        "may",
        "iyun",
        "iyul",
        "avgust",
        "sentabr",
        "oktabr",
        "noyabr",
        "dekabr"
    ]

    return (
        f"{value.day}-{months[value.month - 1]}, "
        f"{value.year}"
    )


def format_short_uz_date(value) -> str:

    if isinstance(value, str):

        value = date.fromisoformat(value)

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


def update_user_activity(chat_id: int, today: date):

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

    return re.sub(
        r"^\s*\d+[\.\)\-]\s*",
        "",
        text.strip()
    ).strip()


def normalize_task(text: str) -> str:

    return (
        clean_task_text(text)
        .lower()
        .replace("\n", " ")
    )


def calculate_stats(tasks):

    stats = {
        "total": len(tasks),
        "completed": 0,
        "failed": 0,
        "pending": 0,
        "percent": 0
    }

    for task in tasks:

        if task["status"] == "completed":

            stats["completed"] += 1

        elif task["status"] == "failed":

            stats["failed"] += 1

        else:

            stats["pending"] += 1

    if stats["total"] > 0:

        stats["percent"] = round(
            (
                stats["completed"]
                / stats["total"]
            ) * 100
        )

    return stats


def progress_bar(percent: int) -> str:

    filled = round(percent / 10)

    empty = 10 - filled

    return (
        "🟩" * filled
        + "⬜" * empty
    )


def get_motivation(percent: int):

    if percent == 100:

        return {
            "title": "🔥 Ajoyib natija!",
            "text": (
                "Barcha vazifalaringiz bajarildi. "
                "Shu tempni davom ettiring! 🚀"
            )
        }

    if percent >= 70:

        return {
            "title": "👍 Yaxshi natija!",
            "text": (
                "Natija yomon emas. "
                "Ertaga yana bir qadam oldinga! 💪"
            )
        }

    if percent >= 40:

        return {
            "title": "💪 Harakat davom etsin!",
            "text": (
                "Bugun ham foydali ishlar qilindi. "
                "Ertaga bundan ham yaxshiroq natija qilish mumkin!"
            )
        }

    if percent > 0:

        return {
            "title": "🌱 Boshlanish bor!",
            "text": (
                "Muhimi to‘xtamaslik. "
                "Ertaga yangi imkoniyat!"
            )
        }

    return {
        "title": "📭 Bugun natija yo‘q",
        "text": (
            "Ertaga yangidan boshlaymiz. "
            "Kichik qadamlar katta natijaga olib boradi! 💪"
        )
    }

# =========================================================
# TELEGRAM HELPERS (ASYNC)
#
# Ilgari sync `requests` ishlatilgan edi - bu FastAPI'ning
# event loop'ini har chaqiriqda bloklar edi (masalan finish_day
# ichida 10 ta task bo'lsa, 10 ta ketma-ket bloklovchi so'rov).
# Endi httpx.AsyncClient bilan, kerak bo'lganda parallel.
# =========================================================

async def telegram_send_message(
    chat_id: int,
    text: str
):

    if not TELEGRAM_TOKEN:

        raise HTTPException(
            status_code=500,
            detail="TELEGRAM_TOKEN is not configured"
        )

    client = get_http_client()

    response = await client.post(
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text
        }
    )

    if response.status_code >= 400:

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


async def telegram_send_message_with_keyboard(
    chat_id: int,
    text: str,
    reply_markup: dict
):

    if not TELEGRAM_TOKEN:

        raise HTTPException(
            status_code=500,
            detail="TELEGRAM_TOKEN is not configured"
        )

    client = get_http_client()

    response = await client.post(
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "reply_markup": reply_markup
        }
    )

    if response.status_code >= 400:

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


async def telegram_answer_callback(
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

    client = get_http_client()

    await client.post(
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/answerCallbackQuery",
        json=payload
    )


async def telegram_delete_message(
    chat_id: int,
    message_id: int
):

    if not TELEGRAM_TOKEN:

        return

    client = get_http_client()

    await client.post(
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/deleteMessage",
        json={
            "chat_id": chat_id,
            "message_id": message_id
        }
    )


# =========================================================
# MORNING KEYBOARD
# =========================================================

async def telegram_send_morning_keyboard(
    chat_id: int,
    first_name: str
):

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

    keyboard = []

    for i in range(
        0,
        len(times),
        3
    ):

        keyboard.append(
            [
                {
                    "text": time,
                    "callback_data":
                        f"morning_time|{time}"
                }

                for time in times[i:i + 3]
            ]
        )

    return await telegram_send_message_with_keyboard(
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
# START
# =========================================================

async def handle_telegram_start(
    chat_id: int,
    username: Optional[str],
    first_name: Optional[str]
):

    first_name = first_name or "Do‘st"

    username = username or ""

    today = get_today()

    user = get_user_by_chat_id(
        chat_id
    )

    # -----------------------------------------------------
    # EXISTING USER
    # -----------------------------------------------------

    if user:

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
                        today,
                        chat_id
                    )
                )

            conn.commit()

        await telegram_send_message(
            chat_id,

            f"""👋 Assalomu alaykum, {first_name}!

Siz allaqachon ro‘yxatdan o‘tgansiz. ✅

📋 Vazifalaringizni yuborishingiz mumkin."""
        )

        return {
            "ok": True,
            "route": "start",
            "existing_user": True
        }

    # -----------------------------------------------------
    # NEW USER
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
                    today
                )
            )

        conn.commit()

    await telegram_send_morning_keyboard(
        chat_id,
        first_name
    )

    return {
        "ok": True,
        "route": "start",
        "new_user": True
    }


# =========================================================
# MORNING TIME
# =========================================================

async def handle_morning_time(
    chat_id: int,
    time_value: str,
    callback_query_id: Optional[str] = None
):

    if not re.match(
        r"^(0[2-9]|10):00$",
        time_value
    ):

        if callback_query_id:

            await telegram_answer_callback(
                callback_query_id,
                "Noto‘g‘ri vaqt."
            )

        return {
            "ok": False
        }

    user = get_user_by_chat_id(
        chat_id
    )

    if not user:

        if callback_query_id:

            await telegram_answer_callback(
                callback_query_id,
                "Avval /start bering."
            )

        return {
            "ok": False
        }

    # -----------------------------------------------------
    # SECOND CLICK
    # -----------------------------------------------------

    if (
        user["morning_time"] is not None
        and user["state"] != "waiting_morning_time"
    ):

        if callback_query_id:

            await telegram_answer_callback(
                callback_query_id,
                "Vaqt allaqachon tanlangan."
            )

        return {
            "ok": True,
            "already_selected": True
        }

    today = get_today()

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
                  AND (
                      morning_time IS NULL
                      OR state = 'waiting_morning_time'
                  )
                """,
                (
                    time_value,
                    today,
                    chat_id
                )
            )

        conn.commit()

    if callback_query_id:

        await telegram_answer_callback(
            callback_query_id,
            "Vaqt belgilandi ✅"
        )

    await telegram_send_message(
        chat_id,

        f"""✅ Ertalabki vaqt belgilandi: {time_value}

🚀 Hammasi tayyor. Endi kunlik vazifalaringizni yuborishingiz mumkin.

⏰ Belgilangan vaqtda sizga eslatma yuboramiz.

📢 Yangiliklar va yangilanishlar: @kunlikvazifalar_news

🤲 Kuningiz barakali o‘tsin!"""
    )

    return {
        "ok": True,
        "route": "morning_time",
        "time": time_value
    }

# =========================================================
# CREATE TASKS
# =========================================================

async def handle_create_tasks(
    chat_id: int,
    text: str
):

    user = get_user_by_chat_id(
        chat_id
    )

    if not user:

        await telegram_send_message(
            chat_id,
            "⚠️ Avval /start buyrug‘ini bosing."
        )

        return {
            "ok": False
        }

    if user["state"] == "blocked":

        await telegram_send_message(
            chat_id,
            "⚠️ Botdan foydalanish uchun /start buyrug‘ini bosing."
        )

        return {
            "ok": False
        }

    # -----------------------------------------------------
    # COMPLETED DAY
    # -----------------------------------------------------

    if user["state"] == "completed":

        await telegram_send_message(
            chat_id,

            """🏁 Bugungi kuningiz allaqachon yakunlangan.

📊 Natijani ko‘rish uchun /hisobot buyrug‘ini yuboring."""
        )

        return {
            "ok": False,
            "day_completed": True
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

        cleaned = clean_task_text(
            raw_task
        )

        if cleaned:

            tasks.append(cleaned)

    if not tasks:

        await telegram_send_message(
            chat_id,
            "⚠️ Vazifa matni bo‘sh."
        )

        return {
            "ok": False
        }

    today = get_today()

    # -----------------------------------------------------
    # EXISTING + INSERT - BITTA CONNECTION/TRANSACTION
    #
    # Ilgari: 1-connection SELECT uchun, 2-connection INSERT
    # loop uchun. Endi ikkalasi bitta connection ichida,
    # shu bilan bitta round-trip narxi kamayadi.
    # -----------------------------------------------------

    added_count = 0
    duplicate_count = 0

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

            existing_rows = cur.fetchall()

        existing = {
            normalize_task(
                row["task_text"]
            )
            for row in existing_rows
        }

        with conn.cursor() as cur:

            for task_text in tasks:

                normalized = normalize_task(
                    task_text
                )

                if normalized in existing:

                    duplicate_count += 1

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
                    """,
                    (
                        user["id"],
                        task_text,
                        today
                    )
                )

                existing.add(
                    normalized
                )

                added_count += 1

        conn.commit()

    # -----------------------------------------------------
    # RESPONSE
    # -----------------------------------------------------

    response_parts = []

    # NEW TASKS
    if added_count > 0:

        response_parts.append(
            f"🎉 {added_count} ta yangi vazifa qabul qilindi va saqlandi!"
        )

    # DUPLICATES
    if duplicate_count > 0:

        response_parts.append(
            f"🔄 {duplicate_count} ta vazifa bugun allaqachon qo‘shilgan."
        )

        response_parts.append(
            "♻️ Qayta saqlanmadi."
        )

    # FINAL
    response_parts.append(
        "🤲 Kuningiz barakatli o‘tsin!"
    )

    # /yakunladim only when NEW TASK exists
    if added_count > 0:

        response_parts.append(
            "🏁 Kuningizni yakunlaganingizda /yakunladim buyrug‘ini yuboring."
        )

    await telegram_send_message(
        chat_id,
        "\n\n".join(response_parts)
    )

    return {
        "ok": True,
        "route": "create_tasks",
        "added": added_count,
        "duplicates": duplicate_count
    }

# =========================================================
# FINISH DAY
# =========================================================

async def handle_finish_day(
    chat_id: int
):

    user = get_user_by_chat_id(
        chat_id
    )

    if not user:

        await telegram_send_message(
            chat_id,
            "⚠️ Avval /start buyrug‘ini bosing."
        )

        return {
            "ok": False
        }

    if user["state"] == "completed":

        await telegram_send_message(
            chat_id,

            """🏁 Bugungi kuningiz allaqachon yakunlangan.

📊 Hisobot uchun /hisobot buyrug‘ini yuboring."""
        )

        return {
            "ok": True,
            "already_completed": True
        }

    today = get_today()

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

        # -------------------------------------------------
        # MARK DAY COMPLETED (bir xil connection ichida,
        # NO PENDING holati bilan birga)
        # -------------------------------------------------

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
    # NO PENDING
    # -----------------------------------------------------

    if not pending_tasks:

        await telegram_send_message(
            chat_id,

            """🎉 Barcha vazifalar belgilandi!

📊 Endi /hisobot buyrug‘ini bersangiz, bugungi hisobotingizni yuboraman."""
        )

        return {
            "ok": True,
            "all_completed": True
        }

    # -----------------------------------------------------
    # SEND EACH TASK - PARALLEL
    #
    # Ilgari: har bir task uchun ketma-ket `await` qilinardi,
    # ya'ni N ta task = N ta Telegram round-trip vaqti yig'indisi
    # (10 task * ~300ms = 3s+). Endi barchasi bir vaqtda
    # asyncio.gather bilan yuboriladi - umumiy vaqt eng sekin
    # bitta so'rov vaqtiga teng bo'ladi.
    #
    # Telegram xabar tartibini kafolatlash uchun index saqlanadi,
    # lekin jismonan yuborish parallel ketadi.
    # -----------------------------------------------------

    async def send_task_message(index: int, task):

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "✅ Bajarildi",
                        "callback_data":
                            f"task_status|{task['id']}|completed"
                    },
                    {
                        "text": "❌ Bajarilmadi",
                        "callback_data":
                            f"task_status|{task['id']}|failed"
                    }
                ]
            ]
        }

        return await telegram_send_message_with_keyboard(
            chat_id,

            f"{index}. {task['task_text']}",

            keyboard
        )

    await asyncio.gather(
        *(
            send_task_message(index, task)
            for index, task in enumerate(
                pending_tasks,
                start=1
            )
        ),
        return_exceptions=True
    )

    return {
        "ok": True,
        "route": "finish_day",
        "pending": len(pending_tasks)
    }

# =========================================================
# TASK STATUS
# =========================================================

async def handle_task_status(
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

            await telegram_answer_callback(
                callback_query_id,
                "Noto‘g‘ri status."
            )

        return {
            "ok": False
        }

    today = get_today()

    # -----------------------------------------------------
    # ATOMIC UPDATE + USER FETCH + PENDING COUNT
    #
    # Ilgari bular 3 ta alohida get_connection() edi.
    # Endi bitta connection, ketma-ket 3 ta query - lekin
    # bitta socket ustida, pool orqali connection ochish
    # narxi faqat bir marta to'lanadi.
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

        # ---------------------------------------------
        # ALREADY PROCESSED
        # ---------------------------------------------

        if not task:

            if callback_query_id:

                await telegram_answer_callback(
                    callback_query_id,
                    "Bu vazifa allaqachon belgilangandi."
                )

            return {
                "ok": True,
                "already_processed": True
            }

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

            user = cur.fetchone()

        if not user:

            # callback javobini va delete'ni pastda birga qilamiz
            pending_count = None

        else:

            with conn.cursor() as cur:

                cur.execute(
                    """
                    SELECT COUNT(*)::int
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
    # ANSWER CALLBACK + DELETE MESSAGE - PARALLEL
    #
    # Bu ikkisi bir-biriga bog'liq emas, shuning uchun
    # ketma-ket await qilish o'rniga birga yuboriladi.
    # -----------------------------------------------------

    followups = []

    if callback_query_id:

        callback_text = (
            "Bajarildi ✅"
            if status == "completed"
            else "Bajarilmadi ❌"
        )

        followups.append(
            telegram_answer_callback(
                callback_query_id,
                callback_text
            )
        )

    if message_id:

        followups.append(
            telegram_delete_message(
                chat_id,
                message_id
            )
        )

    if followups:

        await asyncio.gather(
            *followups,
            return_exceptions=True
        )

    if not user:

        return {
            "ok": True
        }

    # -----------------------------------------------------
    # STILL PENDING
    # -----------------------------------------------------

    if pending_count > 0:

        return {
            "ok": True,
            "pending": pending_count
        }

    # -----------------------------------------------------
    # CLAIM FINAL NOTIFICATION
    # -----------------------------------------------------

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

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

    # -----------------------------------------------------
    # SEND ONLY ONCE
    # -----------------------------------------------------

    if claimant:

        await telegram_send_message(
            chat_id,

            """🎉 Barcha vazifalar belgilandi!

📊 Endi /hisobot buyrug‘ini bersangiz, bugungi hisobotingizni yuboraman."""
        )

    return {
        "ok": True,
        "status": status,
        "pending": 0
    }

# =========================================================
# DAILY REPORT
# =========================================================

async def handle_daily_report(
    chat_id: int
):

    user = get_user_by_chat_id(
        chat_id
    )

    if not user:

        await telegram_send_message(
            chat_id,
            "⚠️ User topilmadi."
        )

        return {
            "ok": False
        }

    today = get_today()

    yesterday = today - timedelta(days=1)

    # -----------------------------------------------------
    # GET TODAY + YESTERDAY (bitta connection)
    # -----------------------------------------------------

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
                ORDER BY created_at ASC
                """,
                (
                    user["id"],
                    today
                )
            )

            today_tasks = cur.fetchall()

            cur.execute(
                """
                SELECT *
                FROM public.tasks
                WHERE user_id = %s
                  AND task_date = %s
                ORDER BY created_at ASC
                """,
                (
                    user["id"],
                    yesterday
                )
            )

            yesterday_tasks = cur.fetchall()

    today_stats = calculate_stats(
        today_tasks
    )

    yesterday_stats = calculate_stats(
        yesterday_tasks
    )

    # -----------------------------------------------------
    # BUILD REPORT
    # -----------------------------------------------------

    lines = []

    lines.append(
        "📊 KUNLIK XULOSA"
    )

    lines.append(
        f"📅 {format_uz_date(today)}"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        f"👍 {today_stats['percent']}%"
    )

    lines.append(
        progress_bar(
            today_stats["percent"]
        )
    )

    lines.append(
        f"{today_stats['completed']} / "
        f"{today_stats['total']} vazifa bajarildi"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "📋 VAZIFALAR"
    )

    if not today_tasks:

        lines.append(
            "ℹ️ Bugun uchun vazifalar topilmadi"
        )

    else:

        for task in today_tasks:

            if task["status"] == "completed":

                icon = "☑️"

            elif task["status"] == "failed":

                icon = "❌"

            else:

                icon = "⏳"

            lines.append(
                f"{icon} {task['task_text']}"
            )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "📈 NATIJA"
    )

    lines.append(
        f"🟩 Bajarildi    {today_stats['completed']}"
    )

    # pending ham bajarilmagan hisoblanadi
    not_completed = (
        today_stats["failed"]
        + today_stats["pending"]
    )

    lines.append(
        f"🟥 Bajarilmadi  {not_completed}"
    )

    lines.append(
        f"🎯 {today_stats['percent']}% natija"
    )

    lines.append(
        progress_bar(
            today_stats["percent"]
        )
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "📅 KECHA"
    )

    if yesterday_stats["total"] == 0:

        lines.append(
            "ℹ️ Ma'lumot yo‘q"
        )

    else:

        lines.append(
            f"👍 {yesterday_stats['percent']}%"
        )

        lines.append(
            progress_bar(
                yesterday_stats["percent"]
            )
        )

        lines.append(
            f"{yesterday_stats['completed']} / "
            f"{yesterday_stats['total']} vazifa bajarildi"
        )

    # -----------------------------------------------------
    # MOTIVATION
    # -----------------------------------------------------

    motivation = get_motivation(
        today_stats["percent"]
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "💡 XULOSA"
    )

    lines.append(
        motivation["title"]
    )

    lines.append(
        motivation["text"]
    )

    lines.append(
        "🎯 Kechagi o‘zingizdan kuchliroq bo‘ling!"
    )

    await telegram_send_message(
        chat_id,
        "\n\n".join(lines)
    )

    return {
        "ok": True,
        "route": "daily_report"
    }

# =========================================================
# WEEKLY REPORT
# =========================================================

async def handle_weekly_report(
    chat_id: int
):

    user = get_user_by_chat_id(
        chat_id
    )

    if not user:

        await telegram_send_message(
            chat_id,
            "⚠️ User topilmadi."
        )

        return {
            "ok": False
        }

    today = get_today()

    monday = (
        today
        - timedelta(
            days=today.weekday()
        )
    )

    start_date = monday - timedelta(
        days=7
    )

    end_date = monday - timedelta(
        days=1
    )

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT *
                FROM public.tasks
                WHERE user_id = %s
                  AND task_date BETWEEN %s AND %s
                ORDER BY task_date ASC, created_at ASC
                """,
                (
                    user["id"],
                    start_date,
                    end_date
                )
            )

            tasks = cur.fetchall()

    stats = calculate_stats(
        tasks
    )

    day_names = [
        "Dushanba",
        "Seshanba",
        "Chorshanba",
        "Payshanba",
        "Juma",
        "Shanba",
        "Yakshanba"
    ]

    daily = {}

    for i in range(7):

        current_date = (
            start_date
            + timedelta(days=i)
        )

        daily[current_date] = {
            "name": day_names[i],
            "total": 0,
            "completed": 0,
            "failed": 0
        }

    for task in tasks:

        task_date = task["task_date"]

        if isinstance(task_date, str):

            task_date = date.fromisoformat(
                task_date
            )

        if task_date not in daily:

            continue

        daily[task_date]["total"] += 1

        if task["status"] == "completed":

            daily[task_date]["completed"] += 1

        elif task["status"] == "failed":

            daily[task_date]["failed"] += 1

    best_day = None
    worst_day = None

    active_days = 0
    perfect_days = 0

    lines = []

    lines.append(
        "📊 HAFTALIK XULOSA"
    )

    lines.append(
        f"📅 {format_uz_date(start_date)} - "
        f"{format_uz_date(end_date)}"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        f"👍 {stats['percent']}%"
    )

    lines.append(
        progress_bar(
            stats["percent"]
        )
    )

    lines.append(
        f"{stats['completed']} / "
        f"{stats['total']} vazifa bajarildi"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "📆 HAFTA KUNLARI"
    )

    for current_date, item in daily.items():

        if item["total"] == 0:

            lines.append(
                f"⬜ {item['name']} - Ma'lumot yo‘q"
            )

            continue

        active_days += 1

        percent = round(
            (
                item["completed"]
                / item["total"]
            ) * 100
        )

        item["percent"] = percent

        if percent == 100:

            perfect_days += 1

        if (
            best_day is None
            or percent > best_day["percent"]
        ):

            best_day = {
                **item
            }

        if (
            worst_day is None
            or percent < worst_day["percent"]
        ):

            worst_day = {
                **item
            }

        lines.append(
            f"🟩 {item['name']} - "
            f"{percent}% "
            f"({item['completed']}/{item['total']})"
        )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "📈 NATIJA"
    )

    lines.append(
        f"🟩 Bajarildi: {stats['completed']}"
    )

    not_completed = (
        stats["failed"]
        + stats["pending"]
    )

    lines.append(
        f"🟥 Bajarilmadi: {not_completed}"
    )

    lines.append(
        f"🎯 {stats['percent']}%"
    )

    lines.append(
        progress_bar(
            stats["percent"]
        )
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "🏆 ENG YAXSHI KUN"
    )

    if best_day:

        lines.append(
            f"🔥 {best_day['name']} - "
            f"{best_day['percent']}%"
        )

    else:

        lines.append(
            "ℹ️ Ma'lumot yo‘q"
        )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "📉 ENG SUST KUN"
    )

    if worst_day:

        lines.append(
            f"📉 {worst_day['name']} - "
            f"{worst_day['percent']}%"
        )

    else:

        lines.append(
            "ℹ️ Ma'lumot yo‘q"
        )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        f"🔥 100% kunlar: {perfect_days} ta"
    )

    lines.append(
        f"📆 Faol kunlar: {active_days} ta"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "💡 HAFTA XULOSASI"
    )

    if stats["total"] == 0:

        lines.append(
            "📭 Bu hafta uchun vazifalar topilmadi."
        )

    else:

        motivation = get_motivation(
            stats["percent"]
        )

        lines.append(
            motivation["title"]
        )

        lines.append(
            motivation["text"]
        )

    await telegram_send_message(
        chat_id,
        "\n\n".join(lines)
    )

    return {
        "ok": True,
        "route": "weekly_report"
    }


# =========================================================
# MONTHLY REPORT
# =========================================================

async def handle_monthly_report(
    chat_id: int
):

    user = get_user_by_chat_id(
        chat_id
    )

    if not user:

        await telegram_send_message(
            chat_id,
            "⚠️ User topilmadi."
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
                SELECT *
                FROM public.tasks
                WHERE user_id = %s
                  AND task_date BETWEEN %s AND %s
                ORDER BY task_date ASC, created_at ASC
                """,
                (
                    user["id"],
                    start_date,
                    today
                )
            )

            tasks = cur.fetchall()

    stats = calculate_stats(
        tasks
    )

    daily = {}

    for task in tasks:

        task_date = task["task_date"]

        if isinstance(task_date, str):

            task_date = date.fromisoformat(
                task_date
            )

        if task_date not in daily:

            daily[task_date] = {
                "total": 0,
                "completed": 0,
                "failed": 0
            }

        daily[task_date]["total"] += 1

        if task["status"] == "completed":

            daily[task_date]["completed"] += 1

        elif task["status"] == "failed":

            daily[task_date]["failed"] += 1

    best_day = None
    worst_day = None
    perfect_days = 0

    for task_date, item in daily.items():

        percent = round(
            (
                item["completed"]
                / item["total"]
            ) * 100
        )

        item["percent"] = percent

        if percent == 100:

            perfect_days += 1

        if (
            best_day is None
            or percent > best_day["percent"]
        ):

            best_day = {
                "date": task_date,
                **item
            }

        if (
            worst_day is None
            or percent < worst_day["percent"]
        ):

            worst_day = {
                "date": task_date,
                **item
            }

    lines = []

    lines.append(
        "📊 OYLIK XULOSA"
    )

    lines.append(
        f"📅 {today.strftime('%m')}-oy, "
        f"{today.year}"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        f"👍 {stats['percent']}%"
    )

    lines.append(
        progress_bar(
            stats["percent"]
        )
    )

    lines.append(
        f"{stats['completed']} / "
        f"{stats['total']} vazifa bajarildi"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "📈 NATIJA"
    )

    lines.append(
        f"🟩 Bajarildi: {stats['completed']}"
    )

    not_completed = (
        stats["failed"]
        + stats["pending"]
    )

    lines.append(
        f"🟥 Bajarilmadi: {not_completed}"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "🏆 ENG YAXSHI KUN"
    )

    if best_day:

        lines.append(
            f"🔥 {format_uz_date(best_day['date'])} - "
            f"{best_day['percent']}%"
        )

    else:

        lines.append(
            "ℹ️ Ma'lumot yo‘q"
        )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "📉 ENG SUST KUN"
    )

    if worst_day:

        lines.append(
            f"📉 {format_uz_date(worst_day['date'])} - "
            f"{worst_day['percent']}%"
        )

    else:

        lines.append(
            "ℹ️ Ma'lumot yo‘q"
        )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        f"🔥 100% bajarilgan kunlar: "
        f"{perfect_days} ta"
    )

    lines.append(
        f"📆 Vazifa qo‘shilgan kunlar: "
        f"{len(daily)} ta"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "💡 OY XULOSASI"
    )

    motivation = get_motivation(
        stats["percent"]
    )

    lines.append(
        motivation["title"]
    )

    lines.append(
        motivation["text"]
    )

    lines.append(
        "🎯 Har bir kun yangi imkoniyat!"
    )

    await telegram_send_message(
        chat_id,
        "\n\n".join(lines)
    )

    return {
        "ok": True,
        "route": "monthly_report"
    }


# =========================================================
# YEARLY REPORT
# =========================================================

async def handle_yearly_report(
    chat_id: int
):

    user = get_user_by_chat_id(
        chat_id
    )

    if not user:

        await telegram_send_message(
            chat_id,
            "⚠️ User topilmadi."
        )

        return {
            "ok": False
        }

    today = get_today()

    year = today.year

    start_date = date(
        year,
        1,
        1
    )

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT *
                FROM public.tasks
                WHERE user_id = %s
                  AND task_date BETWEEN %s AND %s
                ORDER BY task_date ASC, created_at ASC
                """,
                (
                    user["id"],
                    start_date,
                    today
                )
            )

            tasks = cur.fetchall()

    stats = calculate_stats(
        tasks
    )

    month_names = {
        1: "Yanvar",
        2: "Fevral",
        3: "Mart",
        4: "Aprel",
        5: "May",
        6: "Iyun",
        7: "Iyul",
        8: "Avgust",
        9: "Sentabr",
        10: "Oktabr",
        11: "Noyabr",
        12: "Dekabr"
    }

    monthly = {}

    for task in tasks:

        task_date = task["task_date"]

        if isinstance(task_date, str):

            task_date = date.fromisoformat(
                task_date
            )

        month = task_date.month

        if month not in monthly:

            monthly[month] = {
                "total": 0,
                "completed": 0,
                "failed": 0
            }

        monthly[month]["total"] += 1

        if task["status"] == "completed":

            monthly[month]["completed"] += 1

        elif task["status"] == "failed":

            monthly[month]["failed"] += 1

    best_month = None
    worst_month = None

    for month, item in monthly.items():

        percent = round(
            (
                item["completed"]
                / item["total"]
            ) * 100
        )

        item["percent"] = percent

        if (
            best_month is None
            or percent > best_month["percent"]
        ):

            best_month = {
                "month": month,
                **item
            }

        if (
            worst_month is None
            or percent < worst_month["percent"]
        ):

            worst_month = {
                "month": month,
                **item
            }

    lines = []

    lines.append(
        "📊 YILLIK XULOSA"
    )

    lines.append(
        f"📅 {year}-yil"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        f"👍 {stats['percent']}%"
    )

    lines.append(
        progress_bar(
            stats["percent"]
        )
    )

    lines.append(
        f"{stats['completed']} / "
        f"{stats['total']} vazifa bajarildi"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "📆 OYLAR BO‘YICHA"
    )

    if not monthly:

        lines.append(
            "ℹ️ Hozircha ma'lumot yo‘q"
        )

    else:

        for month in sorted(monthly):

            item = monthly[month]

            lines.append(
                f"🟩 {month_names[month]} - "
                f"{item['percent']}% "
                f"({item['completed']}/{item['total']})"
            )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "📈 NATIJA"
    )

    lines.append(
        f"🟩 Bajarildi: {stats['completed']}"
    )

    not_completed = (
        stats["failed"]
        + stats["pending"]
    )

    lines.append(
        f"🟥 Bajarilmadi: {not_completed}"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "🏆 ENG YAXSHI OY"
    )

    if best_month:

        lines.append(
            f"🔥 {month_names[best_month['month']]} - "
            f"{best_month['percent']}%"
        )

    else:

        lines.append(
            "ℹ️ Ma'lumot yo‘q"
        )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "📉 ENG SUST OY"
    )

    if worst_month:

        lines.append(
            f"📉 {month_names[worst_month['month']]} - "
            f"{worst_month['percent']}%"
        )

    else:

        lines.append(
            "ℹ️ Ma'lumot yo‘q"
        )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        f"📆 Faol oylar: {len(monthly)} ta"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "💡 YIL XULOSASI"
    )

    motivation = get_motivation(
        stats["percent"]
    )

    lines.append(
        motivation["title"]
    )

    lines.append(
        motivation["text"]
    )

    lines.append(
        "🏆 Yangi yil yangi natijalar uchun imkoniyat!"
    )

    await telegram_send_message(
        chat_id,
        "\n\n".join(lines)
    )

    return {
        "ok": True,
        "route": "yearly_report"
    }

# =========================================================
# ADMIN
# =========================================================

async def handle_admin(
    chat_id: int
):

    if not is_admin(chat_id):

        await telegram_send_message(
            chat_id,
            "⛔ Sizda admin huquqi yo‘q."
        )

        return {
            "ok": False
        }

    today = get_today()

    yesterday = today - timedelta(
        days=1
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
                    yesterday
                )
            )

            users = cur.fetchone()

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

    total_tasks = int(
        tasks["total"] or 0
    )

    completed = int(
        tasks["completed"] or 0
    )

    percent = (
        round(
            completed
            / total_tasks
            * 100
        )
        if total_tasks
        else 0
    )

    text = f"""👑 ADMIN PANEL

📅 {format_short_uz_date(today)}

━━━━━━━━━━━━━━━━━━━━

👥 FOYDALANUVCHILAR

├ Jami: {users["total"]} ta
├ Bugun qo‘shilgan: {users["today_new"]} ta
└ Kecha qo‘shilgan: {users["yesterday_new"]} ta

📈 FAOLLIK

├ Bugun foydalangan: {users["today_active"]} ta
└ Oxirgi 2 kunda foydalangan: {users["last_two_days"]} ta

📋 BUGUNGI VAZIFALAR

├ Jami: {tasks["total"]} ta
├ Bajarilgan: {tasks["completed"]} ta
├ Bajarilmagan: {tasks["pending"]} ta
├ Muvaffaqiyatsiz: {tasks["failed"]} ta
└ Bajarilish darajasi: {percent}%

📢 Broadcast uchun:

/xabar <matn>"""

    await telegram_send_message(
        chat_id,
        text
    )

    return {
        "ok": True,
        "route": "admin"
    }


# =========================================================
# BROADCAST
#
# Ilgari: har bir user uchun ketma-ket `requests.post` -
# 1000 ta user bo'lsa, minutlab davom etardi va webhook
# javobini ushlab turardi (Telegram kutgan javob vaqtidan
# oshib ketishi mumkin edi).
#
# Endi: asyncio.gather bilan chunklarga bo'lib parallel
# yuboriladi. CHUNK_SIZE Telegram rate limitlariga (~30
# msg/sek) mos tanlanadi - hammasini bir vaqtda otib
# yuborish flood-control'ga tushirib qo'yishi mumkin.
# =========================================================

BROADCAST_CHUNK_SIZE = 25


async def _safe_send(chat_id: int, text: str):

    try:

        await telegram_send_message(
            chat_id,
            text
        )

        return True

    except Exception as error:

        print(
            "Broadcast error:",
            error
        )

        return False


async def handle_broadcast(
    chat_id: int,
    message_text: str
):

    if not is_admin(chat_id):

        await telegram_send_message(
            chat_id,
            "⛔ Sizda admin huquqi yo‘q."
        )

        return {
            "ok": False
        }

    text = re.sub(
        r"^/xabar\s*",
        "",
        message_text,
        flags=re.IGNORECASE
    ).strip()

    if not text:

        await telegram_send_message(
            chat_id,

            """📢 Xabar yuborish formati:

/xabar Sizning xabaringiz"""
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

    for i in range(
        0,
        len(users),
        BROADCAST_CHUNK_SIZE
    ):

        chunk = users[i:i + BROADCAST_CHUNK_SIZE]

        results = await asyncio.gather(
            *(
                _safe_send(
                    user["telegram_chat_id"],
                    text
                )
                for user in chunk
            )
        )

        sent += sum(1 for r in results if r)

        failed += sum(1 for r in results if not r)

    await telegram_send_message(
        chat_id,

        f"""📢 Broadcast yakunlandi.

✅ Yuborildi: {sent} ta

❌ Xatolik: {failed} ta

👥 Jami: {len(users)} ta"""
    )

    return {
        "ok": True,
        "sent": sent,
        "failed": failed
    }

# =========================================================
# REMINDERS
# =========================================================

async def _process_reminder(user, today):

    chat_id = user["telegram_chat_id"]

    first_name = (
        user["first_name"]
        or "Do‘st"
    )

    if (
        user["morning_time"] is None
        or user["state"] == "waiting_morning_time"
    ):

        text = f"""👋 Assalomu alaykum, {first_name}!

⏰ Kuningizni rejalashtirish uchun ertalabki vaqtingizni tanlang.

📋 Vazifalaringizni tartibli boshlash uchun /start buyrug‘ini bosing."""

    else:

        text = f"""👋 Salom, {first_name}!

📋 Bir necha kundan beri yangi vazifa qo‘shilmagan.

Bugungi rejalaringizni yozib, kuningizni tartibli boshlang. 💪

✍️ Vazifalaringizni shu yerga yuboring."""

    try:

        await telegram_send_message(
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

        return {
            "chat_id": chat_id,
            "sent": True
        }

    except Exception as error:

        error_text = str(error)

        is_blocked = (
            "error_code': 403"
            in error_text
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

        return {
            "chat_id": chat_id,
            "sent": False,
            "blocked": is_blocked,
            "error": error_text
        }


async def handle_reminders():

    today = get_today()

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

    # -----------------------------------------------------
    # Filter allaqachon bugun eslatma olganlarni
    # -----------------------------------------------------

    to_process = [
        user
        for user in candidates
        if user["last_reminder_sent_date"] != today
    ]

    # -----------------------------------------------------
    # PARALLEL, CHUNKLANGAN (broadcast'dagi kabi)
    # -----------------------------------------------------

    results = []

    for i in range(
        0,
        len(to_process),
        BROADCAST_CHUNK_SIZE
    ):

        chunk = to_process[i:i + BROADCAST_CHUNK_SIZE]

        chunk_results = await asyncio.gather(
            *(
                _process_reminder(user, today)
                for user in chunk
            )
        )

        results.extend(chunk_results)

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
async def telegram_webhook(
    update: dict
):

    try:

        message = (
            update.get("message")
            or {}
        )

        callback_query = (
            update.get("callback_query")
            or {}
        )

        callback_message = (
            callback_query.get("message")
            or {}
        )

        chat = (
            message.get("chat")
            or callback_message.get("chat")
            or {}
        )

        chat_id = chat.get("id")

        if not chat_id:

            return {
                "ok": True,
                "ignored": True
            }

        message_text = (
            message.get("text")
            or ""
        ).strip()

        callback_data = (
            callback_query.get("data")
            or ""
        )

        username = (
            message.get("from", {}).get("username")
            or callback_query.get("from", {}).get("username")
            or ""
        )

        first_name = (
            message.get("from", {}).get("first_name")
            or callback_query.get("from", {}).get("first_name")
            or "Do‘st"
        )

        # -------------------------------------------------
        # ACTIVITY
        #
        # today bir marta hisoblanadi va pastga uzatiladi -
        # boshqa hech bir joyda qayta DB'ga borilmaydi.
        # -------------------------------------------------

        today = get_today()

        update_user_activity(
            chat_id,
            today
        )

        # -------------------------------------------------
        # START
        # -------------------------------------------------

        if message_text == "/start":

            return await handle_telegram_start(
                chat_id,
                username,
                first_name
            )

        # -------------------------------------------------
        # CALLBACK
        # -------------------------------------------------

        if callback_query:

            callback_query_id = (
                callback_query.get("id")
            )

            callback_message_id = (
                callback_message.get(
                    "message_id"
                )
            )

            # ---------------------------------------------
            # MORNING TIME
            # ---------------------------------------------

            if callback_data.startswith(
                "morning_time|"
            ):

                time_value = (
                    callback_data.split(
                        "|",
                        1
                    )[1]
                )

                return await handle_morning_time(
                    chat_id,
                    time_value,
                    callback_query_id
                )

            # ---------------------------------------------
            # TASK STATUS
            # ---------------------------------------------

            if callback_data.startswith(
                "task_status|"
            ):

                parts = (
                    callback_data.split("|")
                )

                if len(parts) == 3:

                    task_id = parts[1]

                    status = parts[2]

                    return await handle_task_status(
                        chat_id,
                        task_id,
                        status,
                        callback_query_id,
                        callback_message_id
                    )

            # UNKNOWN CALLBACK

            if callback_query_id:

                await telegram_answer_callback(
                    callback_query_id
                )

            return {
                "ok": True,
                "route": "callback_ignored"
            }

        # -------------------------------------------------
        # YAKUNLADIM
        # -------------------------------------------------

        if message_text == "/yakunladim":

            return await handle_finish_day(
                chat_id
            )

        # -------------------------------------------------
        # REPORTS
        # -------------------------------------------------

        if message_text == "/hisobot":

            return await handle_daily_report(
                chat_id
            )

        if message_text == "/haftalik":

            return await handle_weekly_report(
                chat_id
            )

        if message_text == "/oylik":

            return await handle_monthly_report(
                chat_id
            )

        if message_text == "/yillik":

            return await handle_yearly_report(
                chat_id
            )

        # -------------------------------------------------
        # ADMIN
        # -------------------------------------------------

        if message_text == "/admin":

            return await handle_admin(
                chat_id
            )

        # -------------------------------------------------
        # BROADCAST
        # -------------------------------------------------

        if message_text.startswith(
            "/xabar"
        ):

            return await handle_broadcast(
                chat_id,
                message_text
            )

        # -------------------------------------------------
        # UNKNOWN COMMAND
        # -------------------------------------------------

        if message_text.startswith("/"):

            await telegram_send_message(
                chat_id,
                "⚠️ Bu buyruq mavjud emas."
            )

            return {
                "ok": True,
                "route": "unknown_command"
            }

        # -------------------------------------------------
        # TASK TEXT
        # -------------------------------------------------

        if message_text:

            return await handle_create_tasks(
                chat_id,
                message_text
            )

        # -------------------------------------------------
        # LEGACY
        # -------------------------------------------------

        return await proxy_to_legacy(
            update
        )

    except HTTPException:

        raise

    except Exception as error:

        print(
            "TELEGRAM WEBHOOK ERROR:",
            repr(error)
        )

        raise HTTPException(
            status_code=500,
            detail=str(error)
        )


# =========================================================
# REMINDER API
# =========================================================

@router.post("/reminders/run")
async def run_reminders(
    _: None = Depends(verify_api_key)
):

    return await handle_reminders()


# =========================================================
# STATUS
# =========================================================

@router.get("/status")
def get_status():

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
async def start_api(
    data: StartUserRequest
):

    return await handle_telegram_start(
        data.chat_id,
        data.username,
        data.first_name
    )


# =========================================================
# MORNING TIME API
# =========================================================

@router.post("/morning-time")
async def morning_time_api(
    data: MorningTimeRequest
):

    return await handle_morning_time(
        data.chat_id,
        data.time
    )


# =========================================================
# TASKS API
# =========================================================

@router.post("/tasks")
async def tasks_api(
    data: CreateTasksRequest
):

    return await handle_create_tasks(
        data.chat_id,
        data.text
    )


# =========================================================
# TASK STATUS API
# =========================================================

@router.post("/tasks/status")
async def task_status_api(
    data: TaskStatusRequest
):

    return await handle_task_status(
        data.chat_id,
        data.task_id,
        data.status
    )


# =========================================================
# FINISH API
# =========================================================

@router.post("/finish")
async def finish_api(
    chat_id: int
):

    return await handle_finish_day(
        chat_id
    )


# =========================================================
# REPORT APIs
# =========================================================

@router.get(
    "/reports/daily/{chat_id}"
)
async def daily_report_api(
    chat_id: int
):

    return await handle_daily_report(
        chat_id
    )


@router.get(
    "/reports/weekly/{chat_id}"
)
async def weekly_report_api(
    chat_id: int
):

    return await handle_weekly_report(
        chat_id
    )


@router.get(
    "/reports/monthly/{chat_id}"
)
async def monthly_report_api(
    chat_id: int
):

    return await handle_monthly_report(
        chat_id
    )


@router.get(
    "/reports/yearly/{chat_id}"
)
async def yearly_report_api(
    chat_id: int
):

    return await handle_yearly_report(
        chat_id
    )


# =========================================================
# LEGACY BRIDGE
# =========================================================

async def proxy_to_legacy(
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

    client = get_http_client()

    response = await client.post(
        f"{LEGACY_BACKEND_URL}/api/telegram",
        json=update,
        timeout=30
    )

    if response.status_code >= 400:

        raise HTTPException(
            status_code=response.status_code,
            detail=response.text
        )

    return response.json()
