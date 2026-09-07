from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Header, Depends, Query
from pydantic import BaseModel, Field
from psycopg2.extras import RealDictCursor

from database import get_connection


router = APIRouter(prefix="/api")


# =========================================================
# API KEY
# =========================================================

def verify_api_key(x_api_key: Optional[str] = Header(default=None)):
    expected = __import__("os").environ.get("API_KEY")

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
        1 for task in tasks
        if task["status"] == "completed"
    )
    failed = sum(
        1 for task in tasks
        if task["status"] == "failed"
    )
    pending = sum(
        1 for task in tasks
        if task["status"] == "pending"
    )

    percent = round(
        completed / total * 100
    ) if total else 0

    return {
        "total": total,
        "completed": completed,
        "failed": failed,
        "pending": pending,
        "percent": percent
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

    if user["state"] != "active":
        raise HTTPException(
            status_code=409,
            detail="User is not active"
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

            # Bugungi mavjud vazifalar
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

            # Muhim: faqat pending vazifa o'zgaradi.
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
        task for task in rows
        if task["task_date"] == today
    ]

    yesterday_tasks = [
        task for task in rows
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
# Oldingi to'liq hafta: Dushanba - Yakshanba
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

    # Python weekday:
    # Monday=0 ... Sunday=6
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
            task for task in tasks
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
        1 for item in daily_list
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
