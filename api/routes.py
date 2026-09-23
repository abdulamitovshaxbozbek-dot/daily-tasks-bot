import os
import re
import json
import time
import hmac
import hashlib
from urllib.parse import parse_qsl
from datetime import date, timedelta
from typing import Optional

import requests

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query
)

from pydantic import BaseModel, Field
from psycopg2.extras import RealDictCursor

from database import get_connection
from bot_identity import register_chat, require_joined, bot_id, verify_webhook


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

# Mini App (Telegram WebApp) sahifasining to'liq HTTPS manzili.
# Railway'da bu odatda https://<service>.up.railway.app/miniapp
# ko'rinishida bo'ladi. Muhit o'zgaruvchisi orqali sozlanadi,
# shuning uchun domen o'zgarsa kodni tahrirlash shart emas.
MINIAPP_URL = os.getenv(
    "MINIAPP_URL",
    "https://daily-tasks-bot-production.up.railway.app/miniapp"
)

ADMIN_MINIAPP_URL = os.getenv(
    "ADMIN_MINIAPP_URL",
    "https://daily-tasks-bot-production.up.railway.app/admin-miniapp"
)

ADMIN_CHAT_ID = "8908985083"

TIMEZONE = "Asia/Tashkent"

API_KEY = os.getenv("API_KEY")

WHISPER_PROMPT_UZ = (
    "Ertaga maktabga boraman, kitob o'qiyman, sport qilaman, "
    "ingliz tili darsiga boraman, uy vazifasini bajaraman, "
    "universitetga boraman, so'z yodlayman, kursga boraman, "
    "nemis tiliga darsga boraman, ertalab soat sakkizda kursga "
    "boraman, ikki soat o'qiyman, uch soat sport qilaman, "
    "bir soat kitob o'qiyman, soat to'qqizda uyg'onaman."
)

# Ovozli tasdiq so'rovlari uchun amal qilish muddati.
# pop_pending_voice_tasks funksiyasidagi SQL so'rovda
# "interval '15 minutes'" sifatida qo'llaniladi — shu yerda
# o'zgartirilsa, pastdagi SQL ham mos ravishda yangilanishi
# kerak.
PENDING_VOICE_TTL_MINUTES = 15


# =========================================================
# FAIL REASONS (task bajarilmaganiga sabab)
# =========================================================
#
# Kodlar bazada (tasks.fail_reason) shu ko'rinishda saqlanadi.
# Bu markazlashtirilgan lug'at bo'lib, kelajakda:
#   - Mini App dashboard (foiz taqsimoti)
#   - AI insight / tavsiyalar
#   - Haftalik/oylik/yillik "eng ko'p sabab" statistikasi
# uchun bitta manba bo'lib xizmat qiladi. Til yoki emoji
# o'zgarsa ham, bazadagi kod o'zgarmaydi.

FAIL_REASONS: dict[str, dict] = {
    "no_time": {
        "emoji": "🕐",
        "label": "Vaqt yetmadi"
    },
    "forgot": {
        "emoji": "😴",
        "label": "Unutib qo'ydim"
    },
    "bad_mood": {
        "emoji": "😩",
        "label": "Kayfiyat bo'lmadi"
    },
    "too_hard": {
        "emoji": "💪",
        "label": "Juda qiyin bo'ldi"
    },
    "priority": {
        "emoji": "🔀",
        "label": "Muhimroq ish chiqdi"
    },
    "other": {
        "emoji": "🤷",
        "label": "Boshqa sabab"
    }
}

FAIL_REASON_NOT_SET_LABEL = "sababi yozilmadi"


def fail_reason_display(code: Optional[str]) -> str:
    """
    fail_reason kodini foydalanuvchiga ko'rsatiladigan
    "emoji + matn" ko'rinishiga aylantiradi.
    Kod noma'lum yoki None bo'lsa, standart matnni qaytaradi.
    """

    if not code:

        return FAIL_REASON_NOT_SET_LABEL

    reason = FAIL_REASONS.get(code)

    if not reason:

        return FAIL_REASON_NOT_SET_LABEL

    return f"{reason['emoji']} {reason['label']}"


def build_fail_reason_keyboard(task_id: str) -> dict:
    """
    Sabab tanlash uchun inline keyboard yasaydi.
    Har bir tugma callback_data: fail_reason|<task_id>|<code>
    """

    buttons = []

    codes = list(FAIL_REASONS.keys())

    for i in range(0, len(codes), 2):

        row = []

        for code in codes[i:i + 2]:

            reason = FAIL_REASONS[code]

            row.append(
                {
                    "text": f"{reason['emoji']} {reason['label']}",
                    "callback_data": f"fail_reason|{task_id}|{code}"
                }
            )

        buttons.append(row)

    return {
        "inline_keyboard": buttons
    }


# =========================================================
# PENDING FAIL REASON (bazada, worker'lar orasida umumiy)
# =========================================================
#
# Task "bajarilmadi" deb belgilangandan keyin, sabab hali
# tanlanmagan bo'lsa, shu holat public.tasks.reason_message_id
# ustunida saqlanadi (Telegram xabar ID'si). Bu, xotirada
# saqlashdan farqli o'laroq, barcha worker process'lar uchun
# umumiy bo'lgani sababli tanlandi.
#
# Foydalanuvchi istalgan vaqtda eski tugmani bosishi mumkin
# (status allaqachon 'failed', faqat fail_reason to'ldiriladi,
# shuning uchun muddat cheklovi shart emas).
#
# /hisobot chaqirilganda, shu paytgacha javob berilmagan
# barcha so'rov xabarlari o'chiriladi va fail_reason NULL
# ("sababi yozilmadi") holida qoladi — chunki hisobot "hozirgi
# holat"ning suratini oladi.

def add_pending_fail_reason(
    chat_id: int,
    task_id: str,
    message_id: Optional[int]
):

    if message_id is None:

        return

    with get_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE public.tasks
                SET reason_message_id = %s
                WHERE id = %s
                """,
                (
                    message_id,
                    task_id
                )
            )

        conn.commit()


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
# MINI APP: TELEGRAM initData TEKSHIRUVI
# =========================================================
#
# Telegram Mini App ochilganda, brauzerga window.Telegram
# WebApp.initData degan satr beriladi: bu foydalanuvchi
# ma'lumotlari (user, auth_date va h.k.) va Telegram bot
# tokenidan olingan maxfiy kalit bilan hisoblangan
# HMAC-SHA256 imzo (hash) dan iborat.
#
# Backend shu imzoni QAYTA HISOBLAB, Telegram yuborgan hash
# bilan solishtiradi. Agar mos kelsa — bu haqiqatan Telegram
# tomonidan yuborilgan, soxta emas, degani. Bu mexanizmsiz,
# istalgan kishi o'zining chat_id'sini yozib, boshqa
# foydalanuvchining ma'lumotini so'rashi mumkin bo'lar edi.
#
# Rasmiy Telegram hujjatidagi algoritm:
# https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

def verify_telegram_init_data(
    init_data: str,
    max_age_seconds: int = 24 * 60 * 60
) -> dict:
    """
    initData satrini tekshiradi va ichidagi 'user' obyektini
    (dict) qaytaradi. Tekshiruv muvaffaqiyatsiz bo'lsa yoki
    ma'lumot eskirgan bo'lsa, HTTPException(401) ko'taradi.
    """

    if not TELEGRAM_TOKEN:

        raise HTTPException(
            status_code=500,
            detail="TELEGRAM_TOKEN is not configured"
        )

    if not init_data:

        raise HTTPException(
            status_code=401,
            detail="initData yo'q"
        )

    try:

        pairs = parse_qsl(
            init_data,
            strict_parsing=True
        )

    except ValueError:

        raise HTTPException(
            status_code=401,
            detail="initData formati noto'g'ri"
        )

    data = dict(pairs)

    received_hash = data.pop(
        "hash",
        None
    )

    if not received_hash:

        raise HTTPException(
            status_code=401,
            detail="initData ichida hash yo'q"
        )

    # Telegram algoritmi: qolgan kalitlarni alifbo tartibida
    # "kalit=qiymat" ko'rinishida, \n bilan qo'shib, tekshiruv
    # satrini hosil qilamiz.
    check_string = "\n".join(
        f"{key}={data[key]}"
        for key in sorted(data.keys())
    )

    # Maxfiy kalit: HMAC-SHA256("WebAppData", bot_token)
    secret_key = hmac.new(
        b"WebAppData",
        TELEGRAM_TOKEN.encode(),
        hashlib.sha256
    ).digest()

    computed_hash = hmac.new(
        secret_key,
        check_string.encode(),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(
        computed_hash,
        received_hash
    ):

        raise HTTPException(
            status_code=401,
            detail="initData imzosi noto'g'ri"
        )

    auth_date = data.get("auth_date")

    if auth_date:

        try:

            age = time.time() - int(auth_date)

        except ValueError:

            age = None

        if age is not None and age > max_age_seconds:

            raise HTTPException(
                status_code=401,
                detail="initData eskirgan"
            )

    user_raw = data.get("user")

    if not user_raw:

        raise HTTPException(
            status_code=401,
            detail="initData ichida user yo'q"
        )

    try:

        user = json.loads(user_raw)

    except json.JSONDecodeError:

        raise HTTPException(
            status_code=401,
            detail="initData user JSON emas"
        )

    return user


def get_miniapp_chat_id(
    init_data: str = Query(
        ...,
        alias="initData",
        description="Telegram WebApp.initData qiymati"
    )
) -> int:
    """
    FastAPI dependency: initData'ni tekshiradi va undan
    telegram chat_id (foydalanuvchi id) ni chiqarib beradi.
    Mini App endpointlari shuni Depends() orqali ishlatadi.
    """

    user = verify_telegram_init_data(init_data)

    chat_id = user.get("id")

    if not chat_id:

        raise HTTPException(
            status_code=401,
            detail="initData user.id topilmadi"
        )

    return int(chat_id)


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
# KUN TO'LIQ YAKUNLANGANMI? (status + fail_reason)
# =========================================================
#
# "Kun to'liq yakunlandi" endi ikki shartni talab qiladi:
#   1) 'pending' statusidagi task qolmagan bo'lishi
#   2) 'failed' statusidagi hech bir task sababi yozilmagan
#      (fail_reason IS NULL) bo'lmasligi kerak
# Ikkalasi ham bajarilgandagina "Barcha vazifalar belgilandi"
# xabari yuboriladi.

def check_unfinished_tasks_count(
    user_id: str,
    task_date: date
) -> int:

    with get_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT COUNT(*)::int
                FROM public.tasks
                WHERE user_id = %s
                  AND task_date = %s
                  AND (
                      status = 'pending'
                      OR (
                          status = 'failed'
                          AND fail_reason IS NULL
                      )
                  )
                """,
                (
                    user_id,
                    task_date
                )
            )

            return cur.fetchone()[0]


def maybe_notify_day_fully_completed(
    chat_id: int,
    user_id: str,
    task_date: date
):
    """
    Agar kun to'liq yakunlangan bo'lsa (barcha status va
    fail_reason to'ldirilgan bo'lsa), va bu haqda hali
    xabar berilmagan bo'lsa, foydalanuvchiga "Barcha
    vazifalar belgilandi" xabarini yuboradi.

    last_completion_notified_date orqali bir marta
    yuborilishini kafolatlaydi (IS DISTINCT FROM %s sharti
    tufayli, race condition holatida ham faqat bitta worker
    xabar yuboradi).
    """

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
                    task_date,
                    user_id,
                    task_date
                )
            )

            claimant = cur.fetchone()

        conn.commit()

    if claimant:

        telegram_send_message_with_keyboard(
            chat_id,

            "🎉 Barcha vazifalar belgilandi!\n\n"
            "📊 Bugungi hisobotingizni pastdagi tugma orqali "
            "ko‘rishingiz mumkin.",

            {
                "inline_keyboard": [
                    [
                        {
                            "text": "📊 Hisobotni ko‘rish",
                            "web_app": {
                                "url": MINIAPP_URL
                            }
                        }
                    ]
                ]
            }
        )


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
                RETURNING id, last_active_date
                """,
                (chat_id,)
            )

            row = cur.fetchone()

            # Retention hisob-kitobi uchun kunlik faollikni yozib
            # boramiz. Faqat shu jadval yaratilgan kundan keyingi
            # kunlar to'g'ri hisoblanadi. ON CONFLICT DO NOTHING:
            # bir kunda bir necha marta chaqirilsa ham bitta qator.
            if row:

                user_id, today = row

                cur.execute(
                    """
                    INSERT INTO public.user_activity_daily (
                        user_id,
                        activity_date
                    )
                    VALUES (
                        %s,
                        %s
                    )
                    ON CONFLICT (user_id, activity_date) DO NOTHING
                    """,
                    (
                        user_id,
                        today
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

    if not text:
        return text

    replacements = {
        "Ў": "O‘",
        "ў": "o‘",
        "Ғ": "G‘",
        "ғ": "g‘",
        "Қ": "Q",
        "қ": "q",
        "Ҳ": "H",
        "ҳ": "h",
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


def calculate_fail_reason_breakdown(tasks):
    """
    Berilgan tasklar ro'yxati ichidan status='failed'
    bo'lganlarning fail_reason kodlari bo'yicha sonini
    hisoblaydi. Eng ko'p uchragan sababni qaytaradi.

    Qaytadi: (top_code_or_None, count, breakdown_dict)
    breakdown_dict: {code_or_None: count}
    """

    breakdown: dict[Optional[str], int] = {}

    for task in tasks:

        if task["status"] != "failed":

            continue

        code = task.get("fail_reason")

        breakdown[code] = breakdown.get(code, 0) + 1

    if not breakdown:

        return None, 0, breakdown

    top_code = max(
        breakdown,
        key=lambda k: breakdown[k]
    )

    return top_code, breakdown[top_code], breakdown


def format_top_fail_reason_line(tasks) -> Optional[str]:
    """
    Hisobotlarning pastida ko'rsatiladigan
    "Vazifalar bajarilmaganiga eng ko'p sabab: ..." qatorini
    tayyorlaydi. Bajarilmagan task bo'lmasa, None qaytaradi.
    """

    top_code, top_count, _ = calculate_fail_reason_breakdown(
        tasks
    )

    if top_count == 0:

        return None

    label = fail_reason_display(top_code)

    marta_word = "marta"

    return (
        f"🔍 Vazifalar bajarilmaganiga eng ko‘p sabab: "
        f"{label} ({top_count} {marta_word})"
    )


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


def telegram_edit_message_with_keyboard(
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: dict
):
    """Mavjud Telegram xabari va inline tugmalarini yangilaydi."""

    if not TELEGRAM_TOKEN:

        raise HTTPException(
            status_code=500,
            detail="TELEGRAM_TOKEN is not configured"
        )

    response = requests.post(
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/editMessageText",
        json={
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "reply_markup": reply_markup
        },
        timeout=15
    )

    if response.ok:

        return response.json()

    try:

        telegram_error = response.json()

    except Exception:

        telegram_error = response.text

    # Matn va tugmalar o'zgarmagan bo'lsa Telegram 400 qaytaradi.
    # Bu haqiqiy xato emas: checklist allaqachon aktual.
    description = (
        telegram_error.get("description", "")
        if isinstance(telegram_error, dict)
        else str(telegram_error)
    )

    if "message is not modified" in description.lower():

        return {
            "ok": True,
            "not_modified": True
        }

    raise HTTPException(
        status_code=500,
        detail=(
            "Telegram editMessageText failed: "
            f"{telegram_error}"
        )
    )


# =========================================================
# LIVE CHECKLIST
# =========================================================

def get_live_checklist_message_id(
    user_id: str,
    task_date: date
) -> Optional[int]:

    with get_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT live_checklist_message_id
                FROM public.users
                WHERE id = %s
                  AND live_checklist_date = %s
                """,
                (
                    user_id,
                    task_date
                )
            )

            row = cur.fetchone()

    return row[0] if row else None


def save_live_checklist_message_id(
    user_id: str,
    task_date: date,
    message_id: int
):

    with get_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE public.users
                SET
                    live_checklist_message_id = %s,
                    live_checklist_date = %s
                WHERE id = %s
                """,
                (
                    message_id,
                    task_date,
                    user_id
                )
            )

        conn.commit()


def clear_live_checklist_message_id(
    user_id: str,
    task_date: date
):

    with get_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE public.users
                SET
                    live_checklist_message_id = NULL,
                    live_checklist_date = NULL
                WHERE id = %s
                  AND live_checklist_date = %s
                """,
                (
                    user_id,
                    task_date
                )
            )

        conn.commit()


def get_today_task_summary(user_id: str, task_date: date) -> dict:
    """Bugungi jami va pending vazifalarni bitta snapshotda oladi."""

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT
                    id,
                    task_text,
                    status,
                    created_at
                FROM public.tasks
                WHERE user_id = %s
                  AND task_date = %s
                ORDER BY created_at ASC, id ASC
                """,
                (
                    user_id,
                    task_date
                )
            )

            tasks = cur.fetchall()

    return {
        "total": len(tasks),
        "completed": sum(
            1 for task in tasks
            if task["status"] == "completed"
        ),
        "failed": sum(
            1 for task in tasks
            if task["status"] == "failed"
        ),
        "pending": [
            task for task in tasks
            if task["status"] == "pending"
        ]
    }


def build_live_checklist(summary: dict, heading: str) -> tuple[str, dict]:

    pending_tasks = summary["pending"]

    lines = [
        heading,
        "",
        (
            f"📊 Jami: {summary['total']}  |  "
            f"✅ {summary['completed']}  |  "
            f"❌ {summary['failed']}  |  "
            f"⏳ {len(pending_tasks)}"
        ),
        "",
        "Vazifani bajarganingizdan keyingina belgilang.",
        "Hozir hech narsani bosishingiz shart emas. 🔔",
        ""
    ]

    keyboard = []

    for index, task in enumerate(pending_tasks, start=1):

        lines.append(
            f"{index}. ⏳ {task['task_text']}"
        )

        keyboard.append(
            [
                {
                    "text": f"✅ {index}",
                    "callback_data": (
                        f"task_status|{task['id']}|completed"
                    )
                },
                {
                    "text": f"❌ {index}",
                    "callback_data": (
                        f"task_status|{task['id']}|failed"
                    )
                }
            ]
        )

    return (
        "\n".join(lines).rstrip(),
        {
            "inline_keyboard": keyboard
        }
    )


def refresh_live_checklist(
    chat_id: int,
    user: dict,
    heading: str = "📋 Bugungi vazifalar",
    force_new: bool = False
) -> dict:
    """
    DB holatidan checklistni qayta quradi.

    force_new=True bo'lsa eski checklist o'chirilib, chatning eng
    pastiga yangi xabar yuboriladi. Oddiy status o'zgarishida esa
    mavjud xabar joyida tahrirlanadi.
    """

    today = get_today()
    summary = get_today_task_summary(
        user["id"],
        today
    )
    old_message_id = get_live_checklist_message_id(
        user["id"],
        today
    )

    if not summary["pending"]:

        if old_message_id:

            telegram_delete_message(
                chat_id,
                old_message_id
            )

        clear_live_checklist_message_id(
            user["id"],
            today
        )

        return {
            "ok": True,
            "pending": 0,
            "summary": summary
        }

    text, keyboard = build_live_checklist(
        summary,
        heading
    )

    if old_message_id and not force_new:

        try:

            telegram_edit_message_with_keyboard(
                chat_id,
                old_message_id,
                text,
                keyboard
            )

            return {
                "ok": True,
                "pending": len(summary["pending"]),
                "message_id": old_message_id,
                "summary": summary
            }

        except HTTPException:

            # Xabar foydalanuvchi tomonidan o'chirilgan yoki juda
            # eski bo'lsa, quyida yangisini yuboramiz.
            pass

    if old_message_id:

        telegram_delete_message(
            chat_id,
            old_message_id
        )

    sent = telegram_send_message_with_keyboard(
        chat_id,
        text,
        keyboard
    )

    new_message_id = (
        sent.get("result", {}).get("message_id")
    )

    if new_message_id:

        save_live_checklist_message_id(
            user["id"],
            today,
            new_message_id
        )

    return {
        "ok": True,
        "pending": len(summary["pending"]),
        "message_id": new_message_id,
        "summary": summary
    }


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

📋 Qadam botiga xush kelibsiz.

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

    register_chat(chat_id)
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

🚀 Hammasi tayyor!

📝 Endi birinchi qadam — bugungi 1–3 ta vazifangizni yozing.

Masalan:
• Kitob o‘qish
• Sport qilish
• Ingliz tilidan 20 ta so‘z yodlash

✍️ Yozib yuboring yoki 🎙️ ovozli xabar orqali ayting (aniq va sekin gapiring).

Har bir kichik reja — tartibli kun sari bir qadam 💪

📢 Yangiliklar: @birqadam_news"""
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

    if user["state"] == "completed":

        next_morning_time = (
            str(user["morning_time"])[:5]
            if user["morning_time"]
            else "ertalab"
        )

        telegram_send_message_with_keyboard(
            chat_id,

            "🌙 Bugungi kuningiz yakunlangan.\n\n"
            f"Yangi kuningiz soat {next_morning_time} da "
            "boshlanadi. Shundan keyin yangi vazifalarni "
            "yuborishingiz mumkin.\n\n"
            "📊 Natijangizni pastdagi tugma orqali ko‘rishingiz "
            "mumkin.",

            {
                "inline_keyboard": [
                    [
                        {
                            "text": "📊 Hisobotni ko‘rish",
                            "web_app": {
                                "url": MINIAPP_URL
                            }
                        }
                    ]
                ]
            }
        )

        return {
            "ok": False,
            "day_completed": True
        }

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

    # Birinchi vazifami? (1-koddan, to‘g‘ri cursor bilan)
    with get_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM public.tasks
                    WHERE user_id = %s
                )
                """,
                (user["id"],)
            )

            is_first_task_ever = not cur.fetchone()[0]

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

            # Bugun barcha vazifalar belgilangandan keyin yana task
            # qo'shilsa, yangi task ham tugagach completion xabari qayta
            # yuborilishi uchun bir martalik flagni ochamiz.
            if added_count > 0:

                cur.execute(
                    """
                    UPDATE public.users
                    SET last_completion_notified_date = NULL
                    WHERE id = %s
                    """,
                    (user["id"],)
                )

        conn.commit()

    response_parts = []

    if added_count > 0 and is_first_task_ever:

        if added_count == 1:

            response_parts.append(
                "🎉 Ajoyib! Birinchi vazifangiz saqlandi."
            )

        else:

            response_parts.append(
                f"🎉 Ajoyib boshlanish! {added_count} ta vazifangiz saqlandi."
            )

        response_parts.append(
            "Shu tarzda davom eting — har bir kichik qadam katta natijaga olib boradi! 💪"
        )

    elif added_count > 0:

        if from_voice:

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

    if response_parts:

        telegram_send_message(
            chat_id,
            "\n\n".join(response_parts)
        )

    # Yangi vazifa qo'shilganda eski checklistni pastga ko'chiramiz:
    # eski xabar o'chadi, DB'dagi barcha pending vazifalar bilan yangi
    # checklist tasdiq xabaridan keyin yuboriladi.
    if added_count > 0:

        refresh_live_checklist(
            chat_id,
            user,
            heading=(
                f"📋 Bugungi reja — "
                f"{added_count} ta yangi vazifa qo‘shildi"
            ),
            force_new=True
        )

    return {
        "ok": True,
        "route": "create_tasks",
        "added": added_count,
        "duplicates": duplicate_count
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

    today = get_today()

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
                  AND task_date = %s
                RETURNING *
                """,
                (
                    status,
                    task_id,
                    user["id"],
                    today
                )
            )

            task = cur.fetchone()

        conn.commit()

    if not task:

        if callback_query_id:

            telegram_answer_callback(
                callback_query_id,
                "Bu vazifa allaqachon belgilangan yoki eski kun uchun."
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

    live_message_id = get_live_checklist_message_id(
        user["id"],
        today
    )

    # Task statusi DB'ga yozilgach, bitta yashovchi checklistni
    # darhol yangilaymiz. Bajarilgan/bajarilmagan task ro'yxatdan
    # chiqadi va qolgan son avtomatik kamayadi.
    refresh_live_checklist(
        chat_id,
        user
    )

    # ---------------------------------------------------
    # "Bajarilmadi" bosilganda: eski tugmali xabarni o'chirib,
    # sabab tanlash uchun yangi xabar yuboramiz. Status
    # allaqachon 'failed' deb yozilgan (yuqorida), lekin
    # fail_reason hali NULL bo'lgani uchun bu task hamon
    # "tugallanmagan" hisoblanadi (pastdagi
    # check_unfinished_tasks_count shuni hisobga oladi) —
    # ya'ni "Barcha vazifalar belgilandi" xabari sabab
    # tanlanmaguncha kelmaydi.
    # ---------------------------------------------------

    if status == "failed":

        if message_id and message_id != live_message_id:

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

        reason_message = telegram_send_message_with_keyboard(
            chat_id,

            f"❌ {task['task_text']}\n\n"
            "🤔 Nima sabab bajarilmadi?",

            build_fail_reason_keyboard(
                str(task["id"])
            )
        )

        reason_message_id = (
            reason_message
            .get("result", {})
            .get("message_id")
        )

        add_pending_fail_reason(
            chat_id,
            str(task["id"]),
            reason_message_id
        )

    elif message_id and message_id != live_message_id:

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

    # "Barcha vazifalar belgilandi" xabari endi faqat barcha
    # tasklar TO'LIQ yakunlanganda yuboriladi: ya'ni na
    # 'pending' status qolgan, na sababi hali yozilmagan
    # 'failed' task qolgan bo'lishi kerak.
    unfinished_count = check_unfinished_tasks_count(
        user["id"],
        today
    )

    if unfinished_count > 0:

        return {
            "ok": True,
            "pending": unfinished_count
        }

    maybe_notify_day_fully_completed(
        chat_id,
        user["id"],
        today
    )

    return {
        "ok": True,
        "status": status,
        "pending": 0
    }


# =========================================================
# FAIL REASON (bajarilmadi sababi)
# =========================================================

def handle_fail_reason(
    chat_id: int,
    task_id: str,
    reason_code: str,
    callback_query_id: Optional[str] = None,
    message_id: Optional[int] = None
):

    if reason_code not in FAIL_REASONS:

        if callback_query_id:

            telegram_answer_callback(
                callback_query_id,
                "Noto‘g‘ri sabab."
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
                SET
                    fail_reason = %s,
                    reason_message_id = NULL
                WHERE id = %s
                  AND user_id = %s
                  AND status = 'failed'
                RETURNING *
                """,
                (
                    reason_code,
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
                "Bu so‘rov eskirgan."
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

        return {
            "ok": True,
            "expired": True
        }

    if callback_query_id:

        telegram_answer_callback(
            callback_query_id,
            "Qayd etildi ✅"
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

    # Sabab tanlanishi bilan kun to'liq yakunlangan bo'lishi
    # mumkin (masalan, oxirgi ochiq bo'lgan task shu edi).
    # Shuni tekshirib, kerak bo'lsa "Barcha vazifalar
    # belgilandi" xabarini shu yerdan yuboramiz.

    task_date = task["task_date"]

    if isinstance(task_date, str):

        task_date = date.fromisoformat(task_date)

    unfinished_count = check_unfinished_tasks_count(
        user["id"],
        task_date
    )

    if unfinished_count == 0:

        maybe_notify_day_fully_completed(
            chat_id,
            user["id"],
            task_date
        )

    return {
        "ok": True,
        "route": "fail_reason",
        "task_id": task_id,
        "reason": reason_code
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

    # Eslatma: /hisobot chaqirilganda sabab-so'rov xabarlariga
    # ataylab tegilmaydi — ular Telegram'da qolib turadi va
    # foydalanuvchi istalgan vaqtda tugmani bosib sababni
    # belgilashi mumkin. Hisobotda bunday tasklar "sababi
    # yozilmadi" deb ko'rsatiladi, sabab keyin belgilansa
    # keyingi hisobotda yangilanadi.

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

                lines.append(
                    f"☑️ {task['task_text']}"
                )

            elif task["status"] == "failed":

                reason_text = fail_reason_display(
                    task.get("fail_reason")
                )

                lines.append(
                    f"❌ {task['task_text']} — {reason_text}"
                )

            else:

                lines.append(
                    f"⏳ {task['task_text']}"
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

    top_reason_line = format_top_fail_reason_line(
        today_tasks
    )

    if top_reason_line:

        lines.append(
            "━━━━━━━━━━━━━━━━━━━━"
        )

        lines.append(
            top_reason_line
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

    top_reason_line = format_top_fail_reason_line(
        tasks
    )

    if top_reason_line:

        lines.append(
            "━━━━━━━━━━━━━━━━━━━━"
        )

        lines.append(
            top_reason_line
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

    top_reason_line = format_top_fail_reason_line(
        tasks
    )

    if top_reason_line:

        lines.append(
            "━━━━━━━━━━━━━━━━━━━━"
        )

        lines.append(
            top_reason_line
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

    top_reason_line = format_top_fail_reason_line(
        tasks
    )

    if top_reason_line:

        lines.append(
            "━━━━━━━━━━━━━━━━━━━━"
        )

        lines.append(
            top_reason_line
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

    telegram_send_message_with_keyboard(
        chat_id,
        text,
        {
            "inline_keyboard": [
                [
                    {
                        "text": "📊 Admin Dashboard",
                        "web_app": {
                            "url": ADMIN_MINIAPP_URL
                        }
                    }
                ]
            ]
        }
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

    # Bu funksiya fon vazifasi (background task) sifatida
    # ishga tushiriladi — agar shu yerda kutilmagan xato yuz
    # bersa, FastAPI uni jim yutib yuboradi va admin hech
    # qanday xabar olmay qoladi. Shuning uchun butun asosiy
    # jarayonni try/except bilan o'raymiz, xato bo'lsa ham
    # adminga xabar beramiz.
    try:

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
                      AND (%s = false OR active_bot_id = %s)
                    """, (require_joined(), bot_id())
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

                error_text = str(error)

                print(
                    "Broadcast error:",
                    error_text
                )

                is_blocked = (
                    "error_code': 403"
                    in error_text
                    and
                    "bot was blocked by the user"
                    in error_text
                )

                if is_blocked:

                    try:

                        with get_connection() as block_conn:

                            with block_conn.cursor() as block_cur:

                                block_cur.execute(
                                    """
                                    UPDATE public.users
                                    SET state = 'blocked'
                                    WHERE telegram_chat_id = %s
                                    """,
                                    (user["telegram_chat_id"],)
                                )

                            block_conn.commit()

                    except Exception as db_error:

                        print(
                            "Broadcast blocked-state update error:",
                            db_error
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

    except Exception as error:

        print(
            "Broadcast fatal error:",
            repr(error)
        )

        try:

            telegram_send_message(
                chat_id,

                "⚠️ Broadcast yuborishda kutilmagan xatolik "
                "yuz berdi. Loglarni tekshiring."
            )

        except Exception as notify_error:

            print(
                "Broadcast fatal error notify failed:",
                repr(notify_error)
            )

        return {
            "ok": False,
            "error": str(error)
        }

# =========================================================
# OLD USERS MIGRATION BROADCAST
# =========================================================

def handle_old_users_migration(chat_id: int):

    if not is_admin(chat_id):
        return {"ok": False}

    BROADCAST_ID = "old_users_migration_2026_09"

    text = """🚀 QADAM yangilandi!

Botning yangi versiyasi ishga tushdi.

Endi QADAM orqali kunlik vazifalaringizni rejalashtiring, eslatmalar oling va natijalaringizni kuzatib boring.

👇 Yangi versiyaga o‘tish uchun tugmani bosing.

Har kuni maqsad sari bir qadam."""

    try:

        with get_connection() as conn:

            with conn.cursor(
                cursor_factory=RealDictCursor
            ) as cur:

                cur.execute(
                    """
                    SELECT
                        u.id,
                        u.telegram_chat_id
                    FROM public.users u
                    WHERE u.active_bot_id IS NULL
                      AND u.state != 'blocked'
                      AND u.telegram_chat_id IS NOT NULL

                      AND NOT EXISTS (
                          SELECT 1
                          FROM public.broadcast_logs bl
                          WHERE bl.user_id = u.id
                            AND bl.broadcast_id = %s
                            AND bl.status = 'sent'
                      )
                    """,
                    (BROADCAST_ID,)
                )

                users = cur.fetchall()

        sent = 0
        blocked = 0
        failed = 0

        for user in users:

            try:

                telegram_send_message_with_keyboard(
                    user["telegram_chat_id"],
                    text,
                    {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "🚀 Yangi QADAMga o‘tish",
                                    "url": "https://t.me/bir_qadambot?start=old_user"
                                }
                            ]
                        ]
                    }
                )

                with get_connection() as log_conn:

                    with log_conn.cursor() as log_cur:

                        log_cur.execute(
                            """
                            INSERT INTO public.broadcast_logs
                            (
                                broadcast_id,
                                user_id,
                                status
                            )
                            VALUES (%s, %s, 'sent')
                            """,
                            (
                                BROADCAST_ID,
                                user["id"]
                            )
                        )

                    log_conn.commit()

                sent += 1

            except Exception as error:

                error_text = str(error)

                print(
                    "Old users migration error:",
                    error_text
                )

                is_blocked = (
                    "403" in error_text
                     and
                    "bot was blocked by the user" in error_text.lower()
)

                if is_blocked:

                    blocked += 1

                    with get_connection() as block_conn:

                        with block_conn.cursor() as block_cur:

                            block_cur.execute(
                                """
                                UPDATE public.users
                                SET state = 'blocked'
                                WHERE id = %s
                                """,
                                (user["id"],)
                            )

                        block_conn.commit()

                else:
                    failed += 1

        telegram_send_message(
            chat_id,
            f"""🚀 Eski userlar migratsiyasi yakunlandi.

✅ Yuborildi: {sent} ta
🚫 Bloklagan: {blocked} ta
❌ Boshqa xatolik: {failed} ta
👥 Tekshirildi: {len(users)} ta"""
        )

        return {
            "ok": True,
            "sent": sent,
            "blocked": blocked,
            "failed": failed
        }

    except Exception as error:

        print(
            "Old users migration fatal error:",
            repr(error)
        )

        telegram_send_message(
            chat_id,
            "⚠️ Eski userlarga xabar yuborishda xatolik yuz berdi."
        )

        return {
            "ok": False,
            "error": str(error)
        }


# =========================================================
# REMINDERS
# =========================================================

def handle_day_cycle():
    """
    User holatini Asia/Tashkent vaqti bo'yicha boshqaradi.

    - 00:00 da active -> completed va kechagi checklist yopiladi.
    - Har userning morning_time vaqti kelganda completed -> active
      bo'ladi va yangi kunni rejalashtirish xabari yuboriladi.

    Endpoint har daqiqada chaqirilishi mumkin: state shartlari sababli
    bir xil o'tish va xabar takroran bajarilmaydi.
    """

    with get_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    TO_CHAR(
                        CURRENT_TIMESTAMP
                        AT TIME ZONE 'Asia/Tashkent',
                        'HH24:MI'
                    )
                """
            )

            current_time = cur.fetchone()[0]

    # -----------------------------------------------------
    # Yangi kun: userlarni tungi yopiq holatga o'tkazamiz.
    # Eski checklist message_id sini RETURNING orqali olib,
    # Telegramdagi xabarni ham o'chirishga harakat qilamiz.
    # -----------------------------------------------------
    if current_time == "00:00":

        with get_connection() as conn:

            with conn.cursor(
                cursor_factory=RealDictCursor
            ) as cur:

                cur.execute(
                    """
                    UPDATE public.users AS u
                    SET
                        state = 'completed',
                        live_checklist_message_id = NULL,
                        live_checklist_date = NULL
                    FROM (
                        SELECT
                            id,
                            telegram_chat_id,
                            live_checklist_message_id
                        FROM public.users
                        WHERE state = 'active'
                          AND (%s = false OR active_bot_id = %s)
                        FOR UPDATE
                    ) AS old
                    WHERE u.id = old.id
                    RETURNING
                        old.telegram_chat_id,
                        old.live_checklist_message_id
                    """,
                    (require_joined(), bot_id())
                )

                closed_users = cur.fetchall()

            conn.commit()

        deleted_checklists = 0

        for user in closed_users:

            if not user["live_checklist_message_id"]:

                continue

            try:

                telegram_delete_message(
                    user["telegram_chat_id"],
                    user["live_checklist_message_id"]
                )

                deleted_checklists += 1

            except Exception as error:

                print(
                    "Midnight checklist delete error:",
                    user["telegram_chat_id"],
                    repr(error)
                )

        return {
            "ok": True,
            "action": "day_closed",
            "time": current_time,
            "users": len(closed_users),
            "deleted_checklists": deleted_checklists
        }

    # -----------------------------------------------------
    # User tanlagan ertalabki vaqti: faqat completed userni
    # atomar tarzda active qilamiz. Bir daqiqada endpoint bir necha
    # marta chaqirilsa ham xabar faqat bir marta yuboriladi.
    # -----------------------------------------------------
    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                UPDATE public.users
                SET
                    state = 'active',
                    last_active_date = (
                        CURRENT_TIMESTAMP
                        AT TIME ZONE 'Asia/Tashkent'
                    )::date
                WHERE state = 'completed'
                  AND morning_time IS NOT NULL
                  AND LEFT(morning_time::text, 5) = %s
                  AND (%s = false OR active_bot_id = %s)
                RETURNING
                    id,
                    telegram_chat_id,
                    first_name
                """,
                (current_time, require_joined(), bot_id())
            )

            activated_users = cur.fetchall()

        conn.commit()

    results = []

    for user in activated_users:

        chat_id = user["telegram_chat_id"]
        first_name = user["first_name"] or "Do‘st"

        try:

            telegram_send_message(
                chat_id,
                f"""🌅 Assalomu alaykum, {first_name}!

Yangi kun boshlandi.
1 kun — 24 soat, 1440 daqiqa.

Har kuni maqsad sari bir qadam.
Bugun hech bo‘lmaganda 1 ta vazifa yuboring.

Masalan:
• 20 bet kitob o‘qish
• 30 daqiqa sport
• 15 ta so‘z yodlash"""
            )

            results.append(
                {
                    "chat_id": chat_id,
                    "sent": True
                }
            )

        except Exception as error:

            error_text = str(error)

            if (
                "403" in error_text
                or "bot was blocked" in error_text.lower()
            ):

                with get_connection() as block_conn:

                    with block_conn.cursor() as block_cur:

                        block_cur.execute(
                            """
                            UPDATE public.users
                            SET state = 'blocked'
                            WHERE id = %s
                            """,
                            (user["id"],)
                        )

                    block_conn.commit()

            results.append(
                {
                    "chat_id": chat_id,
                    "sent": False,
                    "error": error_text
                }
            )

    return {
        "ok": True,
        "action": "morning_activation",
        "time": current_time,
        "activated": len(activated_users),
        "results": results
    }

def handle_live_checklist_reminders(period: str):
    """
    Mavjud 14:00 va 23:00 schedule uchun smart checklist yuboradi.
    Yangi reminder vaqti yaratmaydi; faqat eski umumiy xabar o'rniga
    real pending vazifalarni chiqaradi.
    """

    headings = {
        "midday": "☀️ Kunning yarmi — davom etamiz",
        "evening": "🌙 Kunni yakunlaymiz"
    }

    if period not in headings:

        raise HTTPException(
            status_code=400,
            detail="period must be midday or evening"
        )

    today = get_today()

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT DISTINCT
                    u.*
                FROM public.users u
                JOIN public.tasks t
                  ON t.user_id = u.id
                 AND t.task_date = %s
                 AND t.status = 'pending'
                WHERE u.telegram_chat_id IS NOT NULL
                  AND u.state != 'blocked'
                  AND (%s = false OR u.active_bot_id = %s)
                """,
                (today, require_joined(), bot_id())
            )

            users = cur.fetchall()

    results = []

    for user in users:

        chat_id = user["telegram_chat_id"]

        try:

            result = refresh_live_checklist(
                chat_id,
                user,
                heading=headings[period],
                force_new=True
            )

            results.append(
                {
                    "chat_id": chat_id,
                    "sent": True,
                    "pending": result["pending"]
                }
            )

        except Exception as error:

            print(
                "Live checklist reminder error:",
                chat_id,
                repr(error)
            )

            results.append(
                {
                    "chat_id": chat_id,
                    "sent": False,
                    "error": str(error)
                }
            )

    return {
        "ok": True,
        "period": period,
        "processed": len(results),
        "results": results
    }

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
                    AND (%s = false OR u.active_bot_id = %s)

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
                (require_joined(), bot_id(), today)
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

✍️ Bugungi 1–3 ta vazifangizni yozib yoki 🎙️ ovozli xabar orqali yuboring (aniq va sekin gapiring).

Masalan:
- Ingliz tilidan 20 ta so‘z yodlash
- 10 bet kitob o‘qish
- 30 daqiqa sport qilish

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
# PENDING VOICE TASKS (bazada, worker'lar orasida umumiy)
# =========================================================
#
# Bir nechta worker process bir vaqtda ishlagani uchun (ko'p
# foydalanuvchini qo'llab-quvvatlash maqsadida), xotirada
# saqlangan holat worker'lar orasida bo'linib qolar edi —
# bitta worker saqlagan ma'lumotni boshqa worker ko'rmasdi.
# Shuning uchun bu holat public.pending_voice_tasks jadvalida
# saqlanadi, u barcha worker'lar uchun umumiy.

def set_pending_voice_tasks(
    chat_id: int,
    tasks: list[str]
):

    with get_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                INSERT INTO public.pending_voice_tasks (
                    chat_id,
                    tasks,
                    created_at
                )
                VALUES (
                    %s,
                    %s,
                    now()
                )
                ON CONFLICT (chat_id) DO UPDATE
                SET
                    tasks = EXCLUDED.tasks,
                    created_at = EXCLUDED.created_at
                """,
                (
                    chat_id,
                    json.dumps(tasks)
                )
            )

        conn.commit()


def pop_pending_voice_tasks(
    chat_id: int
) -> Optional[list[str]]:

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                DELETE FROM public.pending_voice_tasks
                WHERE chat_id = %s
                  AND created_at > now() - (%s || ' minutes')::interval
                RETURNING tasks
                """,
                (
                    chat_id,
                    PENDING_VOICE_TTL_MINUTES
                )
            )

            row = cur.fetchone()

            # Eskirgan (TTL o'tgan) yozuvni ham tozalab qo'yamiz,
            # bo'lmasa u DELETE shartiga tushmay abadiy qolib
            # ketishi mumkin.
            cur.execute(
                """
                DELETE FROM public.pending_voice_tasks
                WHERE chat_id = %s
                """,
                (chat_id,)
            )

        conn.commit()

    if not row:

        return None

    tasks_value = row["tasks"]

    # psycopg2 odatda JSONB ustunini avtomatik list/dict'ga
    # aylantiradi, lekin ehtiyot chorasi sifatida string kelgan
    # holatni ham qo'llab-quvvatlaymiz.
    if isinstance(tasks_value, str):

        try:

            tasks_value = json.loads(tasks_value)

        except (TypeError, json.JSONDecodeError):

            return None

    return tasks_value


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
            "model": "whisper-large-v3",
            "language": "uz",
            "response_format": "json",
            "temperature": "0",
            "prompt": WHISPER_PROMPT_UZ
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
            "max_tokens": 4096,

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

"universitetke boraman" → "Universitetga borish"
"kursga boraman" → "Kursga borish"
"mektep ke bora mann" → "Maktabga borish"
"maktabga boraman" → "Maktabga borish"
"kitob oqish" → "Kitob o‘qish"
"söz yodlash" → "So‘z yodlash"
"ingliz tili darsiga boraman" → "Ingliz tili darsiga borish"

4. O‘zbek tilida tabiiy va grammatik jihatdan to‘g‘ri shakldan foydalaning.

5. Vaqt bo‘lsa, faqat asosiy vazifani ajrating (vaqtni task nomiga qo‘shmang).

6. Bir gapda bir nechta ish bo‘lsa, alohida tasklarga ajrating.

7. Filler so‘zlarni ("xo‘sh", "keyin", "yana" va h.k.) task deb hisoblamang.

8. Salomlashish, savol, suhbatni task qilmang.

9. Transcript qisman buzilgan bo‘lsa, lekin asosiy so‘z tanish bo‘lsa — confidence="low".

10. Tanish so‘z umuman topilmasa — bo‘sh massiv qaytaring.

11. Natija O‘ZBEK LOTIN ALIFBOSIDA bo‘lsin. KIRILL ISHLATMANG.

12. o‘, g‘ harflarini to‘g‘ri ishlating.

13. Tasklar qisqa, infinitiv shaklida: borish, qilish, o‘qish, yodlash.

14. Transcriptdagi xatoni saqlab qolmang.

15. confidence: "high" yoki "low". Ikkilanganda — "low".

16. Takroriy vazifani bir marta qaytaring.

17. Raqam/vaqt chalkash bo‘lsa, task matniga qo‘shmang.

Natija faqat JSON:
{
  "tasks": [
    {"text": "Maktabga borish", "confidence": "high"},
    {"text": "Kursga borish", "confidence": "low"}
  ]
}

Yoki:
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

        if groq_response.status_code == 400:
            return []

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

        telegram_send_message(
            chat_id,
            "🎙️ Ovozingizni tinglayapman..."
        )

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
    update: dict,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None)
):

    verify_webhook(x_telegram_bot_api_secret_token)
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

        if chat.get("type") != "private":
            return {"ok": True, "ignored": True}
        register_chat(chat_id)

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

        print(
            "WEBHOOK: ACTIVITY UPDATE START"
        )

        update_user_activity(
            chat_id
        )

        print(
            "WEBHOOK: ACTIVITY UPDATE DONE"
        )

        if message_text.split(maxsplit=1)[0:1] == ["/start"]:

            return handle_telegram_start(
                chat_id,
                username,
                first_name
            )

        if callback_query:

            callback_query_id = (
                callback_query.get("id")
            )

            callback_message_id = (
                callback_message.get(
                    "message_id"
                )
            )

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

            if callback_data.startswith(
                "fail_reason|"
            ):

                parts = (
                    callback_data.split("|")
                )

                if len(parts) == 3:

                    task_id = parts[1]

                    reason_code = parts[2]

                    return handle_fail_reason(
                        chat_id,
                        task_id,
                        reason_code,
                        callback_query_id,
                        callback_message_id
                    )

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

        # Eslatma: /hisobot, /haftalik, /oylik, /yillik matnli
        # buyruqlar endi mavjud emas — hisobotlar faqat Mini App
        # (Dashboard) orqali ko'riladi. Foydalanuvchi "Barcha
        # vazifalar belgilandi" xabaridagi tugma orqali yoki
        # botning menyu tugmasi orqali Dashboard'ni ochadi.

        if message_text == "/admin":

            return handle_admin(
                chat_id
            )

        if message_text == "/eski_xabar":

            if not is_admin(chat_id):

                telegram_send_message(
                    chat_id,
                    "⛔ Sizda admin huquqi yo‘q."
                )

                return {
                    "ok": False,
                    "route": "old_users_broadcast_denied"
                }

            background_tasks.add_task(
                handle_old_users_migration,
                chat_id
            )

            telegram_send_message(
                chat_id,
                "🚀 Eski userlarga migratsiya xabari yuborish boshlandi."
            )

            return {
                "ok": True,
                "route": "old_users_broadcast_started"
            }

        if message_text.startswith(
            "/xabar"
        ):
            # Admin huquqi va matn borligini DARHOL (webhook
            # ichida) tekshiramiz — faqat haqiqiy broadcast
            # ishini fon vazifasiga yuboramiz.

            if not is_admin(chat_id):

                telegram_send_message(
                    chat_id,
                    "⛔ Sizda admin huquqi yo‘q."
                )

                return {
                    "ok": False,
                    "route": "broadcast_denied"
                }

            broadcast_text = re.sub(
                r"^/xabar\s*",
                "",
                message_text,
                flags=re.IGNORECASE
            ).strip()

            if not broadcast_text:

                telegram_send_message(
                    chat_id,
                    """📢 Xabar yuborish formati:
/xabar Sizning xabaringiz"""
                )

                return {
                    "ok": False,
                    "route": "broadcast_empty"
                }

            background_tasks.add_task(
                handle_broadcast,
                chat_id,
                message_text
            )

            return {
                "ok": True,
                "route": "broadcast_started"
            }

        if message_text.startswith("/"):

            telegram_send_message(
                chat_id,
                "⚠️ Bu buyruq mavjud emas."
            )

            return {
                "ok": True,
                "route": "unknown_command"
            }

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

        if message_text:

            print(
                "TEXT TASK DETECTED"
            )

            return handle_create_tasks(
                chat_id,
                message_text
            )

        print(
            "WEBHOOK: UNSUPPORTED UPDATE IGNORED"
        )

        return {
            "ok": True,
            "ignored": True
        }

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

    reject_external_scheduler()
    return handle_reminders()


@router.post("/day-cycle/run")
def run_day_cycle(
    _: None = Depends(verify_api_key)
):
    """Har daqiqalik cron: 00:00 yopish va morning_time aktivatsiyasi."""

    reject_external_scheduler()
    return handle_day_cycle()


@router.post("/checklist-reminders/run")
def run_live_checklist_reminders(
    period: str = Query(..., pattern="^(midday|evening)$"),
    _: None = Depends(verify_api_key)
):
    """14:00: midday, 23:00: evening parametrida chaqiriladi."""

    reject_external_scheduler()
    return handle_live_checklist_reminders(
        period
    )


def reject_external_scheduler():
    if os.getenv("SCHEDULER_ENABLED", "false").lower() == "true":
        raise HTTPException(status_code=409, detail="Railway scheduler manages these jobs")


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
# MINI APP API (JSON, Telegram xabar yubormaydi)
# =========================================================
#
# Bu endpointlar Mini App (WebApp) sahifasi uchun mo'ljallangan.
# Ular handle_daily_report va shunga o'xshash funksiyalardan
# farqli o'laroq, Telegram'ga xabar yubormaydi — faqat JSON
# ma'lumot qaytaradi, dashboard shu ma'lumot bilan o'zini
# chizadi. Har biri get_miniapp_chat_id orqali Telegram
# initData'ni tekshiradi.

@router.get("/miniapp/me")
def miniapp_me(
    chat_id: int = Depends(get_miniapp_chat_id)
):

    user = get_user_by_chat_id(chat_id)

    if not user:

        raise HTTPException(
            status_code=404,
            detail="Foydalanuvchi topilmadi"
        )

    return {
        "ok": True,
        "chat_id": chat_id,
        "first_name": user.get("first_name"),
        "morning_time": user.get("morning_time"),
        "state": user.get("state"),
        "subscription_status": user.get("subscription_status"),
        "created_at": user.get("created_at"),
    }


def _tasks_to_json(tasks) -> list[dict]:
    """
    RealDictRow tasklar ro'yxatini Mini App uchun mos JSON
    ko'rinishga o'giradi: sabab kodini emoji+matn bilan
    birga beradi, sana/vaqt maydonlarini string qiladi.
    """

    result = []

    for task in tasks:

        task_date = task["task_date"]

        if not isinstance(task_date, str):

            task_date = task_date.isoformat()

        created_at = task.get("created_at")

        if created_at is not None and not isinstance(created_at, str):

            created_at = created_at.isoformat()

        result.append(
            {
                "id": str(task["id"]),
                "task_text": task["task_text"],
                "status": task["status"],
                "task_date": task_date,
                "created_at": created_at,
                "fail_reason": task.get("fail_reason"),
                "fail_reason_display": (
                    fail_reason_display(task.get("fail_reason"))
                    if task["status"] == "failed"
                    else None
                ),
            }
        )

    return result


@router.get("/miniapp/day")
def miniapp_day(
    selected_date: date = Query(..., alias="date"),
    chat_id: int = Depends(get_miniapp_chat_id)
):

    user = get_user_by_chat_id(chat_id)

    if not user:

        raise HTTPException(
            status_code=404,
            detail="Foydalanuvchi topilmadi"
        )

    if selected_date > get_today():

        raise HTTPException(
            status_code=400,
            detail="Kelajakdagi kunni ko‘rib bo‘lmaydi"
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
                (user["id"], selected_date)
            )

            tasks = cur.fetchall()

    return {
        "ok": True,
        "date": selected_date.isoformat(),
        "stats": calculate_stats(tasks),
        "tasks": _tasks_to_json(tasks),
    }


@router.get("/miniapp/daily")
def miniapp_daily(
    chat_id: int = Depends(get_miniapp_chat_id)
):

    user = get_user_by_chat_id(chat_id)

    if not user:

        raise HTTPException(
            status_code=404,
            detail="Foydalanuvchi topilmadi"
        )

    today = get_today()

    yesterday = today - timedelta(days=1)

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
                (user["id"], today)
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
                (user["id"], yesterday)
            )

            yesterday_tasks = cur.fetchall()

    today_stats = calculate_stats(today_tasks)
    yesterday_stats = calculate_stats(yesterday_tasks)

    top_code, top_count, breakdown = calculate_fail_reason_breakdown(
        today_tasks
    )

    return {
        "ok": True,
        "date": today.isoformat(),
        "stats": today_stats,
        "tasks": _tasks_to_json(today_tasks),
        "yesterday": {
            "date": yesterday.isoformat(),
            "stats": yesterday_stats,
        },
        "top_fail_reason": {
            "code": top_code,
            "display": fail_reason_display(top_code) if top_count else None,
            "count": top_count,
        } if top_count else None,
        "motivation": get_motivation(today_stats["percent"]),
    }


@router.get("/miniapp/weekly")
def miniapp_weekly(
    start: Optional[date] = Query(None),
    chat_id: int = Depends(get_miniapp_chat_id)
):

    user = get_user_by_chat_id(chat_id)

    if not user:

        raise HTTPException(
            status_code=404,
            detail="Foydalanuvchi topilmadi"
        )

    today = get_today()

    current_monday = today - timedelta(days=today.weekday())
    start_date = start or current_monday

    if start_date.weekday() != 0:

        raise HTTPException(
            status_code=400,
            detail="Hafta boshlanish sanasi dushanba bo‘lishi kerak"
        )

    if start_date > current_monday:

        raise HTTPException(
            status_code=400,
            detail="Kelajakdagi haftani ko‘rib bo‘lmaydi"
        )

    end_date = min(
        start_date + timedelta(days=6),
        today
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
                (user["id"], start_date, end_date)
            )

            tasks = cur.fetchall()

    stats = calculate_stats(tasks)

    day_names = [
        "Dushanba", "Seshanba", "Chorshanba", "Payshanba",
        "Juma", "Shanba", "Yakshanba"
    ]

    daily = {}

    for i in range(7):

        current_date = start_date + timedelta(days=i)

        daily[current_date.isoformat()] = {
            "date": current_date.isoformat(),
            "name": day_names[i],
            "total": 0,
            "completed": 0,
            "failed": 0,
            "percent": 0,
        }

    for task in tasks:

        task_date = task["task_date"]

        if isinstance(task_date, str):

            task_date = date.fromisoformat(task_date)

        key = task_date.isoformat()

        if key not in daily:

            continue

        daily[key]["total"] += 1

        if task["status"] == "completed":

            daily[key]["completed"] += 1

        elif task["status"] == "failed":

            daily[key]["failed"] += 1

    for item in daily.values():

        if item["total"] > 0:

            item["percent"] = round(
                item["completed"] / item["total"] * 100
            )

    top_code, top_count, breakdown = calculate_fail_reason_breakdown(
        tasks
    )

    return {
        "ok": True,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "can_go_next": start_date < current_monday,
        "stats": stats,
        "days": list(daily.values()),
        "top_fail_reason": {
            "code": top_code,
            "display": fail_reason_display(top_code) if top_count else None,
            "count": top_count,
        } if top_count else None,
        "motivation": get_motivation(stats["percent"]) if stats["total"] > 0 else None,
    }


@router.get("/miniapp/monthly")
def miniapp_monthly(
    year: Optional[int] = Query(None, ge=2000, le=2100),
    month: Optional[int] = Query(None, ge=1, le=12),
    chat_id: int = Depends(get_miniapp_chat_id)
):

    user = get_user_by_chat_id(chat_id)

    if not user:

        raise HTTPException(
            status_code=404,
            detail="Foydalanuvchi topilmadi"
        )

    today = get_today()

    if (year is None) != (month is None):

        raise HTTPException(
            status_code=400,
            detail="Yil va oy birga yuborilishi kerak"
        )

    start_date = (
        date(year, month, 1)
        if year is not None and month is not None
        else today.replace(day=1)
    )

    current_month_start = today.replace(day=1)

    if start_date > current_month_start:

        raise HTTPException(
            status_code=400,
            detail="Kelajakdagi oyni ko‘rib bo‘lmaydi"
        )

    if start_date.month == 12:

        next_month_start = date(
            start_date.year + 1,
            1,
            1
        )

    else:

        next_month_start = date(
            start_date.year,
            start_date.month + 1,
            1
        )

    end_date = min(
        next_month_start - timedelta(days=1),
        today
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
                (user["id"], start_date, end_date)
            )

            tasks = cur.fetchall()

    stats = calculate_stats(tasks)

    daily = {}

    for task in tasks:

        task_date = task["task_date"]

        if isinstance(task_date, str):

            task_date = date.fromisoformat(task_date)

        key = task_date.isoformat()

        if key not in daily:

            daily[key] = {
                "date": key,
                "total": 0,
                "completed": 0,
                "failed": 0,
                "percent": 0,
            }

        daily[key]["total"] += 1

        if task["status"] == "completed":

            daily[key]["completed"] += 1

        elif task["status"] == "failed":

            daily[key]["failed"] += 1

    for item in daily.values():

        item["percent"] = round(
            item["completed"] / item["total"] * 100
        )

    top_code, top_count, breakdown = calculate_fail_reason_breakdown(
        tasks
    )

    return {
        "ok": True,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "can_go_next": start_date < current_month_start,
        "stats": stats,
        "days": sorted(
            daily.values(),
            key=lambda item: item["date"]
        ),
        "top_fail_reason": {
            "code": top_code,
            "display": fail_reason_display(top_code) if top_count else None,
            "count": top_count,
        } if top_count else None,
        "motivation": get_motivation(stats["percent"]) if stats["total"] > 0 else None,
    }


@router.get("/miniapp/yearly")
def miniapp_yearly(
    chat_id: int = Depends(get_miniapp_chat_id)
):

    user = get_user_by_chat_id(chat_id)

    if not user:

        raise HTTPException(
            status_code=404,
            detail="Foydalanuvchi topilmadi"
        )

    today = get_today()
    year = today.year
    start_date = date(year, 1, 1)

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
                (user["id"], start_date, today)
            )

            tasks = cur.fetchall()

    stats = calculate_stats(tasks)

    month_names = {
        1: "Yanvar", 2: "Fevral", 3: "Mart", 4: "Aprel",
        5: "May", 6: "Iyun", 7: "Iyul", 8: "Avgust",
        9: "Sentabr", 10: "Oktabr", 11: "Noyabr", 12: "Dekabr",
    }

    monthly = {}

    for task in tasks:

        task_date = task["task_date"]

        if isinstance(task_date, str):

            task_date = date.fromisoformat(task_date)

        month = task_date.month

        if month not in monthly:

            monthly[month] = {
                "month": month,
                "month_name": month_names[month],
                "total": 0,
                "completed": 0,
                "failed": 0,
                "percent": 0,
            }

        monthly[month]["total"] += 1

        if task["status"] == "completed":

            monthly[month]["completed"] += 1

        elif task["status"] == "failed":

            monthly[month]["failed"] += 1

    for item in monthly.values():

        item["percent"] = round(
            item["completed"] / item["total"] * 100
        )

    top_code, top_count, breakdown = calculate_fail_reason_breakdown(
        tasks
    )

    return {
        "ok": True,
        "year": year,
        "stats": stats,
        "months": sorted(
            monthly.values(),
            key=lambda item: item["month"]
        ),
        "top_fail_reason": {
            "code": top_code,
            "display": fail_reason_display(top_code) if top_count else None,
            "count": top_count,
        } if top_count else None,
        "motivation": get_motivation(stats["percent"]) if stats["total"] > 0 else None,
    }


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
