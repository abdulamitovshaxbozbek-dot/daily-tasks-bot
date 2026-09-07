from datetime import date, timedelta
from typing import Optional
import os
import requests

from fastapi import APIRouter, HTTPException, Header, Depends, Query
from pydantic import BaseModel, Field
from psycopg2.extras import RealDictCursor

from database import get_connection


router = APIRouter(prefix="/api")


# =========================================================
# API KEY
# =========================================================

def verify_api_key(x_api_key: Optional[str] = Header(default=None)):
    expected = os.environ.get("API_KEY")

    # API_KEY Render'da hali qo'yilmagan bo'lsa,
    # development/test rejimida endpoint ishlaydi.
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
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
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
                    (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Tashkent')::date
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


# =========================================================
# TELEGRAM HELPERS
# =========================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

LEGACY_BACKEND_URL = os.environ.get(
    "LEGACY_BACKEND_URL"
)


def telegram_send_message(chat_id: int, text: str):
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
        raise HTTPException(
            status_code=500,
            detail="Telegram sendMessage failed"
        )

    return response.json()


def telegram_send_message_with_keyboard(
    chat_id: int,
    text: str,
    keyboard: list
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
            "reply_markup": {
                "inline_keyboard": keyboard
            }
        },
        timeout=15
    )

    if not response.ok:
        raise HTTPException(
            status_code=500,
            detail="Telegram sendMessage failed"
        )

    return response.json()


def telegram_answer_callback(
    callback_query_id: str,
    text: str = ""
):
    if not TELEGRAM_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="TELEGRAM_TOKEN is not configured"
        )

    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
        json={
            "callback_query_id": callback_query_id,
            "text": text
        },
        timeout=15
    )

    if not response.ok:
        raise HTTPException(
            status_code=500,
            detail="Telegram answerCallbackQuery failed"
        )

    return response.json()


# =========================================================
# TELEGRAM /START
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
                    subscription_status
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    'Asia/Tashkent',
                    'waiting_morning_time',
                    'trial'
                )
                RETURNING id
                """,
                (
                    chat_id,
                    username,
                    first_name
                )
            )

            user_id = cur.fetchone()[0]

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
    user = get_user_by_chat_id(telegram_chat_id)

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
        return {
            "ok": True,
            "created": False,
            "user": existing
        }

    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO public.users
                (
                    telegram_chat_id,
                    telegram_username,
                    first_name,
                    timezone,
                    state,
                    subscription_status
                )
                VALUES
                (
                    %s,
                    %s,
                    %s,
                    'Asia/Tashkent',
                    'waiting_morning_time',
                    'trial'
                )
                RETURNING *
                """,
                (
                    data.telegram_chat_id,
                    data.telegram_username,
                    data.first_name
                )
            )

            user = cur.fetchone()

    return {
        "ok": True,
        "created": True,
        "user": user
    }


@router.patch("/users/{telegram_chat_id}/morning-time")
def set_morning_time(
    telegram_chat_id: int,
    data: MorningTimeRequest,
    _: None = Depends(verify_api_key)
):
    user = get_user_by_chat_id(telegram_chat_id)

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
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE public.users
                SET
                    morning_time = %s,
                    state = 'active'
                WHERE telegram_chat_id = %s
                RETURNING *
                """,
                (
                    data.morning_time,
                    telegram_chat_id
                )
            )

            updated = cur.fetchone()

    return {
        "ok": True,
        "user": updated
    }


# =========================================================
# TASKS — READ
# =========================================================

@router.get("/users/{telegram_chat_id}/tasks")
def get_tasks(
    telegram_chat_id: int,
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    _: None = Depends(verify_api_key)
):
    user = get_user_by_chat_id(telegram_chat_id)

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
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
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
                ORDER BY task_date ASC, created_at ASC
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

def normalize_telegram_task(text: str):
    return (
        text
        .lower()
        .replace("\r", "")
        .strip()
    )


def clean_telegram_task(text: str):
    import re

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
        with conn.cursor(cursor_factory=RealDictCursor) as cur:

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
                    duplicates.append(task_text)
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

                added.append(cur.fetchone())
                current_input.add(normalized)

    return {
        "ok": True,
        "date": today,
        "added_count": len(added),
        "duplicate_count": len(duplicates),
        "added": added,
        "duplicates": duplicates
    }


# =========================================================
# TASK STATUS
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

    if data.status not in ("completed", "failed"):
        raise HTTPException(
            status_code=400,
            detail="Status must be completed or failed"
        )

    user = get_user_by_chat_id(telegram_chat_id)

    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:

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
# FINISH DAY
# =========================================================

@router.post("/users/{telegram_chat_id}/finish-day")
def finish_day(
    telegram_chat_id: int,
    _: None = Depends(verify_api_key)
):
    user = get_user_by_chat_id(telegram_chat_id)

    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    today = get_today()

    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:

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
                SET state = 'completed'
                WHERE id = %s
                RETURNING *
                """,
                (user["id"],)
            )

            updated_user = cur.fetchone()

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

@router.get("/users/{telegram_chat_id}/reports/daily")
def daily_report(
    telegram_chat_id: int,
    _: None = Depends(verify_api_key)
):
    user = get_user_by_chat_id(telegram_chat_id)

    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    today = get_today()
    yesterday = today - timedelta(days=1)

    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
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
                ORDER BY task_date ASC, created_at ASC
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

    today_stats = calculate_stats(today_tasks)
    yesterday_stats = calculate_stats(yesterday_tasks)

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
            today_stats["percent"] -
            yesterday_stats["percent"]
    }


# =========================================================
# WEEKLY REPORT
# =========================================================

@router.get("/users/{telegram_chat_id}/reports/weekly")
def weekly_report(
    telegram_chat_id: int,
    _: None = Depends(verify_api_key)
):
    user = get_user_by_chat_id(telegram_chat_id)

    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    today = get_today()

    current_monday = (
        today - timedelta(days=today.weekday())
    )

    start_date = current_monday - timedelta(days=7)
    end_date = current_monday - timedelta(days=1)

    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
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
                ORDER BY task_date ASC, created_at ASC
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
        current_date = start_date + timedelta(days=i)

        day_tasks = [
            task
            for task in tasks
            if task["task_date"] == current_date
        ]

        day_stats = calculate_stats(day_tasks)

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

@router.get("/users/{telegram_chat_id}/reports/monthly")
def monthly_report(
    telegram_chat_id: int,
    _: None = Depends(verify_api_key)
):
    user = get_user_by_chat_id(telegram_chat_id)

    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    today = get_today()

    start_date = today.replace(day=1)

    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
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
                ORDER BY task_date ASC, created_at ASC
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

    daily_list = list(daily.values())

    for item in daily_list:
        item["percent"] = (
            round(
                item["completed"] /
                item["total"] *
                100
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

@router.get("/users/{telegram_chat_id}/reports/yearly")
def yearly_report(
    telegram_chat_id: int,
    _: None = Depends(verify_api_key)
):
    user = get_user_by_chat_id(telegram_chat_id)

    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    today = get_today()

    start_date = date(today.year, 1, 1)

    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
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
                ORDER BY task_date ASC, created_at ASC
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

    for (year, month), month_tasks in sorted(
        monthly.items()
    ):
        monthly_stats.append({
            "year": year,
            "month": month,
            "stats": calculate_stats(month_tasks)
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
    """
    Migration bridge.

    Hozircha:
    - /start -> FastAPI
    - oddiy matn -> FastAPI
    - qolgan Telegram actionlar -> eski Node backend
    """

    message = update.get("message") or {}
    callback_query = update.get("callback_query") or {}

    chat_id = (
        message.get("chat", {}).get("id")
        or callback_query.get("message", {})
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

    # -----------------------------------------------------
    # /START = FASTAPI
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # ODDIY MATN = VAZIFA QO'SHISH
    # -----------------------------------------------------

    if (
        message_text
        and not message_text.startswith("/")
    ):
        user = get_user_by_chat_id(chat_id)

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

        added_count = result["added_count"]
        duplicate_count = result["duplicate_count"]

        response_text = ""

        if added_count > 0:
            response_text += (
                f"🎉 {added_count} ta yangi vazifa qabul qilindi!\n\n"
            )

        if duplicate_count > 0:
            response_text += (
                f"🔄 {duplicate_count} ta vazifa oldin qo'shilgan.\n\n"
            )

        response_text += "🤲 Kuningiz barakali o'tsin!"

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

    # -----------------------------------------------------
    # QOLGAN ACTIONLAR — VAQTINCHA NODE
    # -----------------------------------------------------

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
