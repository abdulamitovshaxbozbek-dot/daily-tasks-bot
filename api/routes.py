import os
import re
import json
import time
from datetime import date, timedelta
from typing import Optional

import requests

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
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

LEGACY_BACKEND_URL = os.getenv(
    "LEGACY_BACKEND_URL"
)

ADMIN_CHAT_ID = "8908985083"

TIMEZONE = "Asia/Tashkent"

API_KEY = os.getenv("API_KEY")

# Whisper transkripsiya sifatini oshirish uchun namuna matn.
# Bu audio kontent emas, balki Whisper'ga "qanday so'zlar va
# uslub kutilyapti" degan yo'nalish beradi.
WHISPER_PROMPT_UZ = (
    "Ertaga maktabga boraman, kitob o'qiyman, sport qilaman, "
    "ingliz tili darsiga boraman, uy vazifasini bajaraman, "
    "universitetga boraman, so'z yodlayman, kursga boraman, "
    "nemis tiliga darsga boraman, ertalab soat sakkizda kursga "
    "boraman, ikki soat o'qiyman, uch soat sport qilaman, "
    "bir soat kitob o'qiyman, soat to'qqizda uyg'onaman."
)

# Ovozdan aniqlangan, lekin ishonch darajasi past bo'lgan
# vazifalarni foydalanuvchi tasdiqlaguncha vaqtincha saqlash.
# DB sxemasini o'zgartirmaslik uchun xotirada saqlanadi.
# Format: { chat_id: {"tasks": [...], "created_at": float} }
PENDING_VOICE_TASKS: dict[int, dict] = {}

# Tasdiqlanmagan vazifalar necha soniyadan keyin eskirgan
# hisoblanishi (foydalanuvchi tugmani bosmasa).
PENDING_VOICE_TTL_SECONDS = 15 * 60


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


# =========================================================
# ACTIVITY UPDATE
# =========================================================

def update_user_activity(chat_id: int):

    with get_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE public.users
                SET last_active_date =
                    (
                        CURRENT_TIMESTAMP
                        AT TIME ZONE 'Asia/Tashkent'
                    )::date
                WHERE telegram_chat_id = %s
                """,
                (chat_id,)
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
    """
    Takroriy vazifalarni yaxshiroq aniqlash.
    """

    text = clean_task_text(
        text
    ).lower()

    text = re.sub(
        r"[^\w\s]",
        " ",
        text
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


# =========================================================
# UZBEK CYRILLIC -> LATIN
# =========================================================

def uzbek_to_latin(text: str) -> str:
    """
    Groq tasodifan kirill alifbosida qaytarsa,
    avtomatik o‘zbek lotiniga o'tkazadi.
    """

    if not text:
        return text

    replacements = {
        # O'zbek harflari
        "Ў": "O‘",
        "ў": "o‘",

        "Ғ": "G‘",
        "ғ": "g‘",

        "Қ": "Q",
        "қ": "q",

        "Ҳ": "H",
        "ҳ": "h",

        # Kirill -> lotin
        "А": "A",
        "а": "a",

        "Б": "B",
        "б": "b",

        "В": "V",
        "в": "v",

        "Г": "G",
        "г": "g",

        "Д": "D",
        "д": "d",

        "Е": "E",
        "е": "e",

        "Ё": "Yo",
        "ё": "yo",

        "Ж": "J",
        "ж": "j",

        "З": "Z",
        "з": "z",

        "И": "I",
        "и": "i",

        "Й": "Y",
        "й": "y",

        "К": "K",
        "к": "k",

        "Л": "L",
        "л": "l",

        "М": "M",
        "м": "m",

        "Н": "N",
        "н": "n",

        "О": "O",
        "о": "o",

        "П": "P",
        "п": "p",

        "Р": "R",
        "р": "r",

        "С": "S",
        "с": "s",

        "Т": "T",
        "т": "t",

        "У": "U",
        "у": "u",

        "Ф": "F",
        "ф": "f",

        "Х": "X",
        "х": "x",

        "Ц": "Ts",
        "ц": "ts",

        "Ч": "Ch",
        "ч": "ch",

        "Ш": "Sh",
        "ш": "sh",

        "Щ": "Sh",
        "щ": "sh",

        "Ъ": "'",
        "ъ": "'",

        "Ы": "I",
        "ы": "i",

        "Ь": "",
        "ь": "",

        "Э": "E",
        "э": "e",

        "Ю": "Yu",
        "ю": "yu",

        "Я": "Ya",
        "я": "ya"
    }

    for old, new in replacements.items():

        text = text.replace(
            old,
            new
        )

    return text


def clean_parsed_task(text: str) -> str:
    """
    Groq parserdan kelgan taskni yakuniy tozalash.
    """

    text = uzbek_to_latin(
        text
    )

    text = clean_task_text(
        text
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


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

    filled = round(
        percent / 10
    )

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
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage",
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
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage",
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
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/answerCallbackQuery",
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
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/deleteMessage",
        json={
            "chat_id": chat_id,
            "message_id": message_id
        },
        timeout=10
    )


# =========================================================
# MORNING KEYBOARD
# =========================================================

def telegram_send_morning_keyboard(
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
# START
# =========================================================

def handle_telegram_start(
    chat_id: int,
    username: Optional[str],
    first_name: Optional[str]
):

    first_name = first_name or "Do‘st"

    username = username or ""

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
                        last_active_date =
                            (
                                CURRENT_TIMESTAMP
                                AT TIME ZONE 'Asia/Tashkent'
                            )::date,

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
                        chat_id
                    )
                )

            conn.commit()

        telegram_send_message(
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
                    (
                        CURRENT_TIMESTAMP
                        AT TIME ZONE 'Asia/Tashkent'
                    )::date
                )
                """,
                (
                    chat_id,
                    username,
                    first_name,
                    TIMEZONE
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

            telegram_answer_callback(
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

            telegram_answer_callback(
                callback_query_id,
                "Vaqt allaqachon tanlangan."
            )

        return {
            "ok": True,
            "already_selected": True
        }

    with get_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE public.users
                SET
                    morning_time = %s,
                    state = 'active',
                    last_active_date =
                        (
                            CURRENT_TIMESTAMP
                            AT TIME ZONE 'Asia/Tashkent'
                        )::date
                WHERE telegram_chat_id = %s
                  AND (
                      morning_time IS NULL
                      OR state = 'waiting_morning_time'
                  )
                """,
                (
                    time_value,
                    chat_id
                )
            )

        conn.commit()

    if callback_query_id:

        telegram_answer_callback(
            callback_query_id,
            "Vaqt belgilandi ✅"
        )

    telegram_send_message(
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

def handle_create_tasks(
    chat_id: int,
    text: str,
    from_voice: bool = False
):

    user = get_user_by_chat_id(
        chat_id
    )

    if not user:

        telegram_send_message(
            chat_id,
            "⚠️ Avval /start buyrug‘ini bosing."
        )

        return {
            "ok": False
        }

    if user["state"] == "blocked":

        telegram_send_message(
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

        telegram_send_message(
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

        cleaned = uzbek_to_latin(cleaned)

        if cleaned:

            tasks.append(cleaned)

    if not tasks:

        telegram_send_message(
            chat_id,
            "⚠️ Vazifa matni bo‘sh."
        )

        return {
            "ok": False
        }

    today = get_today()

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

            existing_rows = cur.fetchall()

    existing = {
        normalize_task(
            row["task_text"]
        )
        for row in existing_rows
    }

    added_count = 0
    duplicate_count = 0
    added_tasks = []

    # -----------------------------------------------------
    # INSERT TASKS
    # -----------------------------------------------------

    with get_connection() as conn:

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

                added_tasks.append(
                    task_text
                )

                added_count += 1

        # MUHIM:
        # commit endi to'g'ri indentationda.
        conn.commit()

    # -----------------------------------------------------
    # RESPONSE
    # -----------------------------------------------------

    response_parts = []

    if added_count > 0:

        if from_voice:

            # Ovozli xabarda vazifalar ro'yxatini ko'rsatamiz
            response_parts.append(
                f"🎉 {added_count} ta yangi vazifa "
                f"qabul qilindi va saqlandi:"
            )

            for i, task in enumerate(
                added_tasks,
                1
            ):

                response_parts.append(
                    f"{i}. {task}"
                )

        else:

            # Textda vazifalar ro'yxatini ko'rsatmaymiz
            response_parts.append(
                f"🎉 {added_count} ta yangi vazifa "
                f"qabul qilindi va saqlandi!"
            )

    if duplicate_count > 0:

        response_parts.append(
            f"🔄 {duplicate_count} ta vazifa "
            f"bugun allaqachon qo‘shilgan."
        )

        response_parts.append(
            "♻️ Qayta saqlanmadi."
        )

    if added_count > 0 or duplicate_count > 0:

        response_parts.append(
            "🤲 Kuningiz barakali o‘tsin!"
        )

    if added_count > 0:

        response_parts.append(
            "🏁 Kuningizni yakunlaganingizda "
            "/yakunladim buyrug‘ini yuboring."
        )

    if response_parts:

        telegram_send_message(
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

def handle_finish_day(
    chat_id: int
):

    user = get_user_by_chat_id(
        chat_id
    )

    if not user:

        telegram_send_message(
            chat_id,
            "⚠️ Avval /start buyrug‘ini bosing."
        )

        return {
            "ok": False
        }

    if user["state"] == "completed":

        telegram_send_message(
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

    if not pending_tasks:

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

        telegram_send_message(
            chat_id,

            """🎉 Barcha vazifalar belgilandi!

📊 Endi /hisobot buyrug‘ini bersangiz, bugungi hisobotingizni yuboraman."""
        )

        return {
            "ok": True,
            "all_completed": True
        }

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

    for index, task in enumerate(
        pending_tasks,
        start=1
    ):

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

        telegram_send_message_with_keyboard(
            chat_id,

            f"{index}. {task['task_text']}",

            keyboard
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
                "Noto‘g‘ri status."
            )

        return {
            "ok": False
        }

    user = get_user_by_chat_id(
        chat_id
    )

    if not user:

        if callback_query_id:

            telegram_answer_callback(
                callback_query_id,
                "Foydalanuvchi topilmadi."
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

        conn.commit()

    if not task:

        if callback_query_id:

            telegram_answer_callback(
                callback_query_id,
                "Bu vazifa allaqachon belgilangandi."
            )

        return {
            "ok": True,
            "already_processed": True
        }

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

    if message_id:

        try:

            telegram_delete_message(
                chat_id,
                message_id
            )

        except Exception as error:

            print(
                "Delete message error:",
                error
            )

    today = get_today()

    with get_connection() as conn:

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

    if pending_count > 0:

        return {
            "ok": True,
            "pending": pending_count
        }

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

    if claimant:

        telegram_send_message(
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

def handle_daily_report(
    chat_id: int
):

    user = get_user_by_chat_id(
        chat_id
    )

    if not user:

        telegram_send_message(
            chat_id,
            "⚠️ User topilmadi."
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

    telegram_send_message(
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

def handle_weekly_report(
    chat_id: int
):

    user = get_user_by_chat_id(
        chat_id
    )

    if not user:

        telegram_send_message(
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

    telegram_send_message(
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

def handle_monthly_report(
    chat_id: int
):

    user = get_user_by_chat_id(
        chat_id
    )

    if not user:

        telegram_send_message(
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

    telegram_send_message(
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

def handle_yearly_report(
    chat_id: int
):

    user = get_user_by_chat_id(
        chat_id
    )

    if not user:

        telegram_send_message(
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

    telegram_send_message(
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

def handle_admin(
    chat_id: int
):

    if not is_admin(chat_id):

        telegram_send_message(
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
    message_text: str
):

    if not is_admin(chat_id):

        telegram_send_message(
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

        telegram_send_message(
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

    for user in users:

        try:

            telegram_send_message(
                user["telegram_chat_id"],
                text
            )

            sent += 1

        except Exception as error:

            failed += 1

            print(
                "Broadcast error:",
                error
            )

    telegram_send_message(
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
                    MAX(t.task_date) AS last_task_date

                FROM public.users u

                LEFT JOIN public.tasks t
                    ON t.user_id = u.id

                WHERE
                    u.telegram_chat_id IS NOT NULL
                    AND u.state != 'blocked'

                GROUP BY
                    u.id

                HAVING

                    (
                        u.morning_time IS NULL
                        OR u.state = 'waiting_morning_time'
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
                (today,)
            )

            candidates = cur.fetchall()

    for user in candidates:

        chat_id = user["telegram_chat_id"]

        if (
            user["last_reminder_sent_date"]
            == today
        ):

            continue

        first_name = (
            user["first_name"]
            or "Do‘st"
        )

        if (
            user["morning_time"] is None
            or user["state"] == "waiting_morning_time"
        ):

            text = f"""👋 Assalomu alaykum, {first_name}!

⏰ Siz ertalabki vaqtingizni hali tanlamagansiz.

🕐 Iltimos, vaqt tanlashni yakunlang. Shundan so‘ng botdan bemalol foydalanishingiz mumkin. 😊

Quyidagi vaqtlardan birini tanlang:"""

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

            reply_markup = {
                "inline_keyboard": keyboard
            }

        else:

            text = f"""👋 Salom, {first_name}!

📋 Oxirgi paytlarda yangi vazifalaringiz qo‘shilmagan.

✍️ Bugungi 1–3 ta vazifangizni yozib ko‘ring.

Masalan:
• Ingliz tilidan 20 ta so‘z yodlash
• 10 bet kitob o‘qish
• 30 daqiqa sport qilish

✨ Har bir reja — tartibli hayot sari bir qadam!"""

            reply_markup = None

        try:

            if reply_markup:

                telegram_send_message_with_keyboard(
                    chat_id,
                    text,
                    reply_markup
                )

            else:

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

            results.append(
                {
                    "chat_id": chat_id,
                    "sent": True
                }
            )

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

            results.append(
                {
                    "chat_id": chat_id,
                    "sent": False,
                    "blocked": is_blocked,
                    "error": error_text
                }
            )

    return {
        "ok": True,
        "date": today,
        "checked": len(candidates),
        "results": results
    }


# =========================================================
# PENDING VOICE TASKS (in-memory)
# =========================================================

def _cleanup_expired_pending_voice_tasks():
    """
    Eskirgan (TTL o'tgan) tasdiqlanmagan vazifalarni tozalaydi.
    """

    now = time.time()

    expired_chat_ids = [
        cid
        for cid, entry in PENDING_VOICE_TASKS.items()
        if now - entry["created_at"] > PENDING_VOICE_TTL_SECONDS
    ]

    for cid in expired_chat_ids:

        PENDING_VOICE_TASKS.pop(cid, None)


def set_pending_voice_tasks(
    chat_id: int,
    tasks: list[str]
):

    _cleanup_expired_pending_voice_tasks()

    PENDING_VOICE_TASKS[chat_id] = {
        "tasks": tasks,
        "created_at": time.time()
    }


def pop_pending_voice_tasks(
    chat_id: int
) -> Optional[list[str]]:

    _cleanup_expired_pending_voice_tasks()

    entry = PENDING_VOICE_TASKS.pop(
        chat_id,
        None
    )

    if not entry:

        return None

    return entry["tasks"]


# =========================================================
# GROQ VOICE TRANSCRIPTION
# =========================================================

def groq_transcribe_telegram_voice(
    file_id: str
) -> str:

    print("========================================")
    print("VOICE TRANSCRIBE START")
    print("VOICE FILE ID:", file_id)
    print("========================================")

    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "TELEGRAM_TOKEN sozlanmagan"
        )

    if not GROQ_API_KEY:
        raise RuntimeError(
            "GROQ_API_KEY sozlanmagan"
        )

    # =====================================================
    # 1. TELEGRAM GET FILE
    # =====================================================

    print(
        "VOICE: Telegram getFile chaqirilmoqda..."
    )

    telegram_file_response = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
        params={
            "file_id": file_id
        },
        timeout=15
    )

    print(
        "VOICE: Telegram getFile status:",
        telegram_file_response.status_code
    )

    if telegram_file_response.status_code != 200:

        print(
            "VOICE TELEGRAM GETFILE ERROR:",
            telegram_file_response.text
        )

        raise RuntimeError(
            "Telegram fayl ma'lumotini olishda xatolik"
        )

    telegram_file_data = (
        telegram_file_response.json()
    )

    file_path = (
        telegram_file_data
        .get("result", {})
        .get("file_path")
    )

    if not file_path:

        print(
            "VOICE ERROR: Telegram file_path yo‘q"
        )

        print(
            "VOICE TELEGRAM RESPONSE:",
            telegram_file_data
        )

        raise RuntimeError(
            "Telegram file_path qaytarmadi"
        )

    print(
        "VOICE FILE PATH:",
        file_path
    )

    # =====================================================
    # 2. TELEGRAM'DAN OGG YUKLASH
    # =====================================================

    print(
        "VOICE: Ovoz fayli Telegram'dan yuklanmoqda..."
    )

    audio_response = requests.get(
        f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}",
        timeout=30
    )

    print(
        "VOICE: Audio download status:",
        audio_response.status_code
    )

    if audio_response.status_code != 200:

        print(
            "VOICE AUDIO DOWNLOAD ERROR:",
            audio_response.text
        )

        raise RuntimeError(
            "Ovoz faylini yuklab olishda xatolik"
        )

    audio_size = len(
        audio_response.content
    )

    print(
        "VOICE AUDIO SIZE:",
        audio_size,
        "bytes"
    )

    if not audio_response.content:

        raise RuntimeError(
            "Ovoz fayli bo‘sh"
        )

    # =====================================================
    # 3. GROQ WHISPER
    # =====================================================

    print(
        "VOICE: Groq Whisper'ga yuborilmoqda..."
    )

    groq_response = requests.post(
        "https://api.groq.com/openai/v1/audio/transcriptions",

        headers={
            "Authorization":
                f"Bearer {GROQ_API_KEY}"
        },

        files={
            "file": (
                "voice.ogg",
                audio_response.content,
                "audio/ogg"
            )
        },

        data={
            "model": "whisper-large-v3-turbo",

            # MUHIM:
            # Foydalanuvchi asosan o‘zbekcha gapiradi.
            "language": "uz",

            # MUHIM:
            # Whisper'ga o'zbek tilidagi kundalik vazifalar
            # uslubi va lug'ati haqida yo'nalish beramiz.
            # Bu aralash til (qozoq/turkcha) bilan
            # chalkashishni kamaytiradi.
            "prompt": WHISPER_PROMPT_UZ,

            "response_format": "json",

            # MUHIM:
            # temperature=0 har doim "eng ishonchli" so'zni tanlaydi,
            # lekin tez/notinch nutqda bu ko'pincha noto'g'ri bo'ladi.
            # Kichik oraliq bersak, Whisper qiyin joylarda fallback
            # qilib, qaytadan baholaydi.
            "temperature": "0.2"
        },

        timeout=60
    )

    print(
        "VOICE: Groq Whisper status:",
        groq_response.status_code
    )

    if groq_response.status_code != 200:

        print(
            "VOICE GROQ WHISPER ERROR:",
            groq_response.text
        )

        raise RuntimeError(
            "Groq Whisper xatolik qaytardi"
        )

    result = groq_response.json()

    transcript = (
        result.get("text")
        or ""
    ).strip()

    print(
        "VOICE TRANSCRIPT:",
        transcript
    )

    print(
        "VOICE TRANSCRIBE DONE"
    )

    return transcript

# =========================================================
# GROQ TASK PARSER
# =========================================================

def groq_parse_tasks(
    transcript: str
) -> list[dict]:
    """
    Har bir element: {"text": str, "confidence": "high" | "low"}
    """

    print("========================================")
    print("VOICE TASK PARSER START")
    print("PARSER TRANSCRIPT:", transcript)
    print("========================================")

    if not transcript.strip():
        print("VOICE TASK PARSER: transcript bo‘sh")
        return []

    if not GROQ_API_KEY:
        print("VOICE TASK PARSER ERROR: GROQ_API_KEY yo‘q")
        raise RuntimeError("GROQ_API_KEY sozlanmagan")

    print("VOICE TASK PARSER: Groq API'ga yuborilmoqda...")

    groq_response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",

        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        },

        json={
            "model": "openai/gpt-oss-20b",

            "messages": [
                {
                    "role": "system",
                    "content": """
Siz o‘zbek tilidagi ovozli xabardan kundalik bajarilishi kerak bo‘lgan vazifalarni aniqlaydigan aqlli task parser siz.

Sizga Telegram Whisper orqali olingan TRANSCRIPT beriladi.

MUHIM:
Whisper o‘zbekcha gaplarni ba'zan turkcha, ozarbayjoncha, qozoqcha yoki aralash ko‘rinishda noto‘g‘ri yozishi mumkin.

Siz transcriptni so‘zma-so‘z qabul qilmang.
Avval foydalanuvchi nima demoqchi bo‘lganini MA'NO bo‘yicha tushuning.
Keyin uni tabiiy o‘zbek tilidagi vazifa shakliga keltiring.

ASOSIY QOIDALAR:

1. Har bir aniq bajariladigan ishni alohida task qiling.

2. "boraman", "boraman", "boram an", "bora mann", "bora man",
   "keboram", "kelaman", "qilaman", "o‘qiyman", "yodlayman"
   kabi buzilgan fe'l shakllarini ma'nosiga qarab to‘g‘rilang.

3. Ovoz tanib olishdagi fonetik xatolarni tuzating.

Misollar:

"universitetke boraman"
→ "Universitetga borish"

"universitetka boraman"
→ "Universitetga borish"

"kursu ge bora man"
→ "Kursga borish"

"kursga boraman"
→ "Kursga borish"

"mektep ke bora mann"
→ "Maktabga borish"

"məktepke boraman"
→ "Maktabga borish"

"mektepga boraman"
→ "Maktabga borish"

"maktab keboram"
→ "Maktabga borish"

"maktabga boraman"
→ "Maktabga borish"

"kitap okuş"
→ "Kitob o‘qish"

"kitob oqish"
→ "Kitob o‘qish"

"söz yotlaş"
→ "So‘z yodlash"

"söz yodlash"
→ "So‘z yodlash"

"inglesislislir dærske boraman"
→ "Ingliz tili darsiga borish"

"ingliz tiliga darsga boraman"
→ "Ingliz tili darsiga borish"

"ingliz tili darsiga boraman"
→ "Ingliz tili darsiga borish"

4. O‘zbek tilida tabiiy va grammatik jihatdan to‘g‘ri shakldan foydalaning.

Masalan:
"Mektepga borish" YOMON.
"Maktabga borish" YAXSHI.

"Universitetga borish" YAXSHI.

"Ingliz tilini darsga borish" YOMON.
"Ingliz tili darsiga borish" YAXSHI.

5. Foydalanuvchi gapida vaqt bo‘lsa, task ma'nosini saqlang.

Masalan:
"Bugun soat sakkizda kursga boraman"
→ "Kursga borish"

"Bugun maktabga boraman"
→ "Maktabga borish"

Hozircha vaqtni task nomiga qo‘shmang, faqat asosiy vazifani ajrating.

6. Bir gapda bir nechta ish bo‘lsa, ularni alohida tasklarga ajrating.

Masalan:
"Bugun universitetga boraman, keyin maktabga boraman"
→
"Universitetga borish"
"Maktabga borish"

7. "xo‘sh", "keyin", "yana", "shuningdek", "demak",
"mayli", "ana", "endi" kabi filler so‘zlarni task deb hisoblamang.

8. Salomlashish, savol, fikr, izoh, minnatdorchilik yoki oddiy suhbatni task qilmang.

9. Transcript juda buzilgan bo‘lsa ham, undagi tanish so‘zlar va gap tuzilmasidan foydalanib, foydalanuvchining ehtimoliy ma'nosini tiklashga harakat qiling — LEKIN bu holatda confidence="low" deb belgilang (15-qoidaga qarang).

Masalan:

"Maktab keboram an abit tanki in."

Bu transcript grammatik jihatdan buzilgan.
Lekin "Maktab" va "keboram" qismlaridan foydalanuvchi
maktabga borishni nazarda tutgan bo‘lishi mumkin.

Shuning uchun:
→ {"text": "Maktabga borish", "confidence": "low"}

10. Ammo transcriptda umuman bajariladigan ishni anglatadigan yetarli signal bo‘lmasa, taxmin qilib task yaratmang.

Masalan:
"Salom, yaxshimisiz?"
→ []

11. Natijadagi barcha tasklar O‘ZBEK LOTIN ALIFBOSIDA bo‘lsin.

KIRILL ISHLATMANG.

To‘g‘ri:
- Maktabga borish
- Universitetga borish
- Kitob o‘qish
- So‘z yodlash
- Ingliz tili darsiga borish

12. O‘zbekcha maxsus harflarni to‘g‘ri ishlating:
o‘, g‘

13. Tasklar qisqa bo‘lsin va odatda infinitiv shaklida tugasin:
- borish
- qilish
- o‘qish
- yodlash
- o‘rganish

14. Transcriptdagi xatoni saqlab qolmang.
Masalan:
"mektep" → "maktab"
"kitap" → "kitob"
"bora man" → "borish"
"bora mann" → "borish"

15. HAR BIR vazifa uchun "confidence" maydonini belgilang:

"high" — agar:
- Transcript aniq va tushunarli bo‘lsa
- So‘zlar deyarli to‘g‘ri tanilgan bo‘lsa (ozgina fonetik xato bo‘lishi mumkin)
- Vazifa ma'nosi shubhasiz bo‘lsa

"low" — agar:
- Transcript juda buzilgan yoki tushunarsiz bo‘lsa
- Siz so‘zlarni katta darajada taxmin qilib tiklagan bo‘lsangiz
- Bir nechta boshqacha talqin ham mumkin bo‘lsa
- Transcript tarkibida aralash til (qozoqcha/turkcha) so‘zlar ko‘p bo‘lib, ma'noni aniq tiklash qiyin bo‘lsa

Ikkilanganda — har doim "low" tanlang. "low" xato emas, u shunchaki foydalanuvchidan tasdiq so‘rashga yordam beradi.

16. Agar bitta transcriptda bir xil vazifa takrorlansa, uni faqat bir marta qaytaring.

17. Raqamlar, soatlar va vaqt ifodalari (masalan "soat nol sakkiz",
"ikki soat", "uch marta") ko'pincha tez nutqda noto'g'ri
tanilgan bo'ladi. Agar raqam/vaqt qismi tushunarsiz yoki
chalkash bo'lsa:

- Uni task matniga QO'SHMANG (faqat asosiy harakatni ajrating).
- Bu raqam tufayli butun taskni "low" deb belgilamang, agar
  qolgan qism (harakatning o'zi) aniq bo'lsa.

Masalan:
"Ertalab soat nol sækiz nol nol-dil kursga boraman"
→ {"text": "Kursga borish", "confidence": "high"}
(chunki "kursga borish" aniq, faqat vaqt qismi chalkash)

Lekin agar harakatning o'zi ham vaqt/raqam bilan chambarchas
bog'liq bo'lib, raqamsiz ma'nosiz qolsa (masalan noaniq son
nechta marta takrorlanishi kerakligini bildirsa), confidence
"low" qiling.

MUHIM:
Sizning vazifangiz transcriptni tarjima qilish emas.
Sizning vazifangiz transcriptdan FOYDALANUVCHI NIMA QILISHI KERAKLIGINI aniqlash.

Natija faqat quyidagi JSON schema formatida bo‘lsin:

{
  "tasks": [
    {"text": "Maktabga borish", "confidence": "high"},
    {"text": "Kursga borish", "confidence": "low"}
  ]
}

Agar aniq vazifa topilmasa:

{
  "tasks": []
}

Hech qanday qo‘shimcha matn yozmang.
"""
                },
                {
                    "role": "user",
                    "content": transcript
                }
            ],

            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "task_list",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "tasks": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "text": {
                                            "type": "string"
                                        },
                                        "confidence": {
                                            "type": "string",
                                            "enum": [
                                                "high",
                                                "low"
                                            ]
                                        }
                                    },
                                    "required": [
                                        "text",
                                        "confidence"
                                    ],
                                    "additionalProperties": False
                                }
                            }
                        },
                        "required": ["tasks"],
                        "additionalProperties": False
                    }
                }
            }
        },

        timeout=60
    )

    print(
        "VOICE TASK PARSER STATUS:",
        groq_response.status_code
    )

    if groq_response.status_code != 200:
        print(
            "VOICE GROQ TASK PARSER ERROR:",
            groq_response.text
        )
        raise RuntimeError(
            "Groq task parser xatolik qaytardi"
        )

    parser_response = groq_response.json()

    print(
        "VOICE TASK PARSER RESPONSE:",
        parser_response
    )

    content = (
        parser_response
        .get("choices", [{}])[0]
        .get("message", {})
        .get("content")
        or ""
    )

    print(
        "VOICE TASK PARSER CONTENT:",
        content
    )

    try:
        data = json.loads(content)

    except json.JSONDecodeError as error:
        print(
            "VOICE TASK PARSER JSON ERROR:",
            repr(error)
        )
        return []

    raw_tasks = data.get("tasks", [])

    cleaned_tasks = []
    seen_tasks = set()

    for item in raw_tasks:

        if not isinstance(item, dict):
            continue

        task_text = item.get("text")
        confidence = item.get("confidence")

        if not isinstance(task_text, str):
            continue

        if confidence not in ("high", "low"):
            # Noma'lum holatda ehtiyotkorlik bilan "low" deb olamiz
            confidence = "low"

        cleaned = clean_parsed_task(task_text)

        if not cleaned:
            continue

        normalized = normalize_task(cleaned)

        if normalized in seen_tasks:
            continue

        seen_tasks.add(normalized)

        cleaned_tasks.append(
            {
                "text": cleaned,
                "confidence": confidence
            }
        )

    print(
        "VOICE PARSED TASKS:",
        cleaned_tasks
    )

    print("VOICE TASK PARSER DONE")

    return cleaned_tasks


# =========================================================
# HANDLE VOICE MESSAGE
# =========================================================

def handle_voice_message(
    chat_id: int,
    voice: dict
):

    print("========================================")
    print("HANDLE VOICE START")
    print("VOICE CHAT ID:", chat_id)
    print("VOICE DATA:", voice)
    print("========================================")

    file_id = voice.get(
        "file_id"
    )

    if not file_id:

        print(
            "VOICE ERROR: file_id topilmadi"
        )

        telegram_send_message(
            chat_id,
            "⚠️ Ovoz faylini topa olmadim. Qayta yuboring."
        )

        return {
            "ok": False
        }

    print(
        "VOICE FILE ID FOUND:",
        file_id
    )

    try:

        # =================================================
        # STEP 1 — TRANSCRIPTION
        # =================================================

        print(
            "VOICE STEP 1: transcription boshlanmoqda"
        )

        transcript = groq_transcribe_telegram_voice(
            file_id
        )

        print(
            "VOICE STEP 1 DONE"
        )

        print(
            "VOICE TRANSCRIPT:",
            transcript
        )

        if not transcript:

            print(
                "VOICE: transcript bo‘sh"
            )

            telegram_send_message(
                chat_id,
                "⚠️ Ovozdan matnni tushunmadim. Qayta ayting."
            )

            return {
                "ok": False
            }

        # =================================================
        # STEP 2 — TASK PARSER
        # =================================================

        print(
            "VOICE STEP 2: task parser boshlanmoqda"
        )

        parsed_tasks = groq_parse_tasks(
            transcript
        )

        print(
            "VOICE STEP 2 DONE"
        )

        print(
            "VOICE TASKS:",
            parsed_tasks
        )

        if not parsed_tasks:

            print(
                "VOICE: task topilmadi"
            )

            telegram_send_message(
                chat_id,
                "Tushunmadim, qayta ayting."
            )

            return {
                "ok": True,
                "route": "voice",
                "tasks": []
            }

        high_conf_tasks = [
            item["text"]
            for item in parsed_tasks
            if item["confidence"] == "high"
        ]

        low_conf_tasks = [
            item["text"]
            for item in parsed_tasks
            if item["confidence"] == "low"
        ]

        print(
            "VOICE HIGH CONFIDENCE:",
            high_conf_tasks
        )

        print(
            "VOICE LOW CONFIDENCE:",
            low_conf_tasks
        )

        result = {
            "ok": True,
            "route": "voice"
        }

        # =================================================
        # STEP 3a — ISHONCHLI VAZIFALARNI DARHOL SAQLASH
        # =================================================

        if high_conf_tasks:

            print(
                "VOICE STEP 3a: high-confidence tasklarni saqlash"
            )

            task_text = "\n".join(
                high_conf_tasks
            )

            create_result = handle_create_tasks(
                chat_id,
                task_text,
                from_voice=True
            )

            result["auto_saved"] = create_result

            print(
                "VOICE STEP 3a DONE:",
                create_result
            )

        # =================================================
        # STEP 3b — NOANIQ VAZIFALARNI TASDIQLASH
        # =================================================

        if low_conf_tasks:

            print(
                "VOICE STEP 3b: low-confidence tasklarni tasdiqlashga yuborish"
            )

            set_pending_voice_tasks(
                chat_id,
                low_conf_tasks
            )

            confirm_lines = "\n".join(
                f"• {t}" for t in low_conf_tasks
            )

            keyboard = {
                "inline_keyboard": [
                    [
                        {
                            "text": "✅ Ha, to‘g‘ri",
                            "callback_data": "voice_confirm|yes"
                        },
                        {
                            "text": "❌ Yo‘q, bekor qilish",
                            "callback_data": "voice_confirm|no"
                        }
                    ]
                ]
            }

            telegram_send_message_with_keyboard(
                chat_id,

                "🤔 Ovozingizni to‘liq aniq tushunolmadim.\n\n"
                "Quyidagi vazifa(lar)ni to‘g‘ri tushundimmi?\n\n"
                f"{confirm_lines}",

                keyboard
            )

            result["pending_confirmation"] = low_conf_tasks

            print(
                "VOICE STEP 3b DONE"
            )

        print("========================================")
        print("HANDLE VOICE DONE")
        print("========================================")

        return result

    except Exception as error:

        print("========================================")
        print(
            "VOICE ERROR:",
            repr(error)
        )

        print(
            "VOICE ERROR TYPE:",
            type(error).__name__
        )

        print("========================================")

        try:

            telegram_send_message(
                chat_id,
                "⚠️ Ovozli xabarni qayta ishlashda xatolik yuz berdi. Qayta urinib ko‘ring."
            )

        except Exception as telegram_error:

            print(
                "VOICE ERROR MESSAGE SEND ERROR:",
                repr(telegram_error)
            )

        return {
            "ok": False,
            "error": str(error)
        }


# =========================================================
# HANDLE VOICE CONFIRM CALLBACK
# =========================================================

def handle_voice_confirm(
    chat_id: int,
    decision: str,
    callback_query_id: Optional[str] = None,
    message_id: Optional[int] = None
):

    pending_tasks = pop_pending_voice_tasks(
        chat_id
    )

    if not pending_tasks:

        if callback_query_id:

            telegram_answer_callback(
                callback_query_id,
                "Bu so‘rov eskirgan."
            )

        return {
            "ok": True,
            "expired": True
        }

    if message_id:

        try:

            telegram_delete_message(
                chat_id,
                message_id
            )

        except Exception as error:

            print(
                "Voice confirm delete message error:",
                error
            )

    if decision == "yes":

        if callback_query_id:

            telegram_answer_callback(
                callback_query_id,
                "Saqlanmoqda ✅"
            )

        task_text = "\n".join(
            pending_tasks
        )

        return handle_create_tasks(
            chat_id,
            task_text,
            from_voice=True
        )

    # decision == "no"

    if callback_query_id:

        telegram_answer_callback(
            callback_query_id,
            "Bekor qilindi"
        )

    telegram_send_message(
        chat_id,
        "❌ Bekor qilindi. Vazifani matn yoki ovoz orqali qayta yuborishingiz mumkin."
    )

    return {
        "ok": True,
        "cancelled": True
    }


# =========================================================
# TELEGRAM WEBHOOK
# =========================================================

@router.post("/telegram")
def telegram_webhook(
    update: dict
):

    print("========================================")
    print("TELEGRAM WEBHOOK RECEIVED")
    print("========================================")

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

            print(
                "WEBHOOK: chat_id topilmadi"
            )

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

        # =================================================
        # ACTIVITY
        # =================================================

        print(
            "WEBHOOK: ACTIVITY UPDATE START"
        )

        update_user_activity(
            chat_id
        )

        print(
            "WEBHOOK: ACTIVITY UPDATE DONE"
        )

        # =================================================
        # START
        # =================================================

        if message_text == "/start":

            return handle_telegram_start(
                chat_id,
                username,
                first_name
            )

        # =================================================
        # CALLBACK
        # =================================================

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

                return handle_morning_time(
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

                    return handle_task_status(
                        chat_id,
                        task_id,
                        status,
                        callback_query_id,
                        callback_message_id
                    )

            # ---------------------------------------------
            # VOICE CONFIRM
            # ---------------------------------------------

            if callback_data.startswith(
                "voice_confirm|"
            ):

                decision = (
                    callback_data.split(
                        "|",
                        1
                    )[1]
                )

                return handle_voice_confirm(
                    chat_id,
                    decision,
                    callback_query_id,
                    callback_message_id
                )

            if callback_query_id:

                telegram_answer_callback(
                    callback_query_id
                )

            return {
                "ok": True,
                "route": "callback_ignored"
            }

        # =================================================
        # YAKUNLADIM
        # =================================================

        if message_text == "/yakunladim":

            return handle_finish_day(
                chat_id
            )

        # =================================================
        # REPORTS
        # =================================================

        if message_text == "/hisobot":

            return handle_daily_report(
                chat_id
            )

        if message_text == "/haftalik":

            return handle_weekly_report(
                chat_id
            )

        if message_text == "/oylik":

            return handle_monthly_report(
                chat_id
            )

        if message_text == "/yillik":

            return handle_yearly_report(
                chat_id
            )

        # =================================================
        # ADMIN
        # =================================================

        if message_text == "/admin":

            return handle_admin(
                chat_id
            )

        # =================================================
        # BROADCAST
        # =================================================

        if message_text.startswith(
            "/xabar"
        ):

            return handle_broadcast(
                chat_id,
                message_text
            )

        # =================================================
        # UNKNOWN COMMAND
        # =================================================

        if message_text.startswith("/"):

            telegram_send_message(
                chat_id,
                "⚠️ Bu buyruq mavjud emas."
            )

            return {
                "ok": True,
                "route": "unknown_command"
            }

        # =================================================
        # VOICE MESSAGE
        # =================================================

        if message.get("voice"):

            print("========================================")
            print("VOICE DETECTED")

            print(
                "VOICE CHAT ID:",
                chat_id
            )

            print(
                "VOICE OBJECT:",
                message["voice"]
            )

            print("========================================")

            return handle_voice_message(
                chat_id,
                message["voice"]
            )

        # =================================================
        # TASK TEXT
        # =================================================

        if message_text:

            print(
                "TEXT TASK DETECTED"
            )

            return handle_create_tasks(
                chat_id,
                message_text
            )

        # =================================================
        # LEGACY
        # =================================================

        print(
            "WEBHOOK: LEGACY ROUTE"
        )

        return proxy_to_legacy(
            update
        )

    except HTTPException:

        raise

    except Exception as error:

        print("========================================")

        print(
            "TELEGRAM WEBHOOK ERROR:",
            repr(error)
        )

        print(
            "TELEGRAM WEBHOOK ERROR TYPE:",
            type(error).__name__
        )

        print("========================================")

        raise HTTPException(
            status_code=500,
            detail=str(error)
        )


# =========================================================
# REMINDER API
# =========================================================

@router.post("/reminders/run")
def run_reminders(
    _: None = Depends(verify_api_key)
):

    return handle_reminders()


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
# FINISH API
# =========================================================

@router.post("/finish")
def finish_api(
    chat_id: int
):

    return handle_finish_day(
        chat_id
    )


# =========================================================
# REPORT APIs
# =========================================================

@router.get(
    "/reports/daily/{chat_id}"
)
def daily_report_api(
    chat_id: int
):

    return handle_daily_report(
        chat_id
    )


@router.get(
    "/reports/weekly/{chat_id}"
)
def weekly_report_api(
    chat_id: int
):

    return handle_weekly_report(
        chat_id
    )


@router.get(
    "/reports/monthly/{chat_id}"
)
def monthly_report_api(
    chat_id: int
):

    return handle_monthly_report(
        chat_id
    )


@router.get(
    "/reports/yearly/{chat_id}"
)
def yearly_report_api(
    chat_id: int
):

    return handle_yearly_report(
        chat_id
    )


# =========================================================
# LEGACY BRIDGE
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
