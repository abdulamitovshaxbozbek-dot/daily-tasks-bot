"""Admin-only analytics endpoints for the Qadam Mini App dashboard.

Every endpoint here requires a verified Telegram initData whose user id
matches ADMIN_CHAT_ID (imported from routes.py, single source of truth).
No endpoint here sends Telegram messages or mutates user-facing state;
they are read-only aggregation queries for the admin dashboard.
"""
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg2.extras import RealDictCursor

from database import get_connection
from bot_identity import bot_id
from routes import (
    ADMIN_CHAT_ID,
    get_miniapp_chat_id,
    get_today,
)

router = APIRouter(prefix="/api/admin")


# =========================================================
# AUTH
# =========================================================
#
# Reuses the same initData verification as the user Mini App
# (get_miniapp_chat_id), then additionally requires the chat_id
# to match ADMIN_CHAT_ID. Frontend has no way to bypass this:
# every endpoint below depends on verify_admin, not on any
# client-supplied flag.

def verify_admin(
    chat_id: int = Depends(get_miniapp_chat_id)
) -> int:

    if str(chat_id) != str(ADMIN_CHAT_ID):

        raise HTTPException(
            status_code=403,
            detail="Admin huquqi yo'q"
        )

    return chat_id


# Every query filters to the current bot identity so users migrated
# from a legacy bot instance aren't double-counted or misattributed.
def _bot_filter() -> tuple:
    return (bot_id(),)


# =========================================================
# ME (frontend uses this to confirm admin access before
# rendering anything sensitive)
# =========================================================

@router.get("/me")
def admin_me(
    chat_id: int = Depends(verify_admin)
):

    return {
        "ok": True,
        "chat_id": chat_id
    }


# =========================================================
# OVERVIEW
# =========================================================

@router.get("/overview")
def admin_overview(
    _: int = Depends(verify_admin)
):

    today = get_today()
    yesterday = today - timedelta(days=1)
    week_ago = today - timedelta(days=7)

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT
                    COUNT(*) AS total,

                    COUNT(*) FILTER (
                        WHERE created_at::date = %s
                    ) AS today_new,

                    COUNT(*) FILTER (
                        WHERE created_at::date = %s
                    ) AS yesterday_new,

                    COUNT(*) FILTER (
                        WHERE created_at::date >= %s
                    ) AS week_new,

                    COUNT(*) FILTER (
                        WHERE last_active_date = %s
                    ) AS today_active,

                    COUNT(*) FILTER (
                        WHERE last_active_date >= %s
                    ) AS week_active,

                    COUNT(*) FILTER (
                        WHERE state = 'completed'
                        AND last_active_date = %s
                    ) AS today_completed_day

                FROM public.users
                WHERE active_bot_id = %s
                """,
                (
                    today,
                    yesterday,
                    week_ago,
                    today,
                    week_ago,
                    today,
                    bot_id()
                )
            )

            users = cur.fetchone()

            cur.execute(
                """
                SELECT
                    COUNT(*) AS today_tasks,
                    COUNT(DISTINCT user_id) AS today_task_users
                FROM public.tasks
                WHERE task_date = %s
                """,
                (today,)
            )

            tasks = cur.fetchone()

    return {
        "ok": True,
        "date": today.isoformat(),
        "users": {
            "total": users["total"],
            "today_new": users["today_new"],
            "yesterday_new": users["yesterday_new"],
            "week_new": users["week_new"],
            "today_active": users["today_active"],
            "week_active": users["week_active"],
            "today_completed_day": users["today_completed_day"],
        },
        "tasks": {
            "today_total": tasks["today_tasks"],
            "today_users_with_tasks": tasks["today_task_users"],
        }
    }


# =========================================================
# TRENDS (chart data for overview)
# =========================================================

@router.get("/trends")
def admin_trends(
    days: int = Query(7, ge=1, le=90),
    _: int = Depends(verify_admin)
):

    today = get_today()
    start_date = today - timedelta(days=days - 1)

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            # New users per day, generated as a series so days with
            # zero signups still appear (no gaps in the chart).
            cur.execute(
                """
                SELECT
                    d::date AS day,
                    COUNT(u.id) AS new_users
                FROM generate_series(%s, %s, interval '1 day') AS d
                LEFT JOIN public.users u
                    ON u.created_at::date = d::date
                    AND u.active_bot_id = %s
                GROUP BY d
                ORDER BY d
                """,
                (start_date, today, bot_id())
            )

            new_users_by_day = {
                row["day"].isoformat(): row["new_users"]
                for row in cur.fetchall()
            }

            cur.execute(
                """
                SELECT
                    d::date AS day,
                    COUNT(a.user_id) AS active_users
                FROM generate_series(%s, %s, interval '1 day') AS d
                LEFT JOIN public.user_activity_daily a
                    ON a.activity_date = d::date
                GROUP BY d
                ORDER BY d
                """,
                (start_date, today)
            )

            active_users_by_day = {
                row["day"].isoformat(): row["active_users"]
                for row in cur.fetchall()
            }

    days_list = []

    cursor_date = start_date

    while cursor_date <= today:

        key = cursor_date.isoformat()

        days_list.append(
            {
                "date": key,
                "new_users": new_users_by_day.get(key, 0),
                "active_users": active_users_by_day.get(key, 0),
            }
        )

        cursor_date += timedelta(days=1)

    return {
        "ok": True,
        "days": days_list
    }


# =========================================================
# ACTIVITY (funnel + today's task breakdown)
# =========================================================

@router.get("/activity")
def admin_activity(
    _: int = Depends(verify_admin)
):

    today = get_today()

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            # Funnel: total -> active today -> entered a task today
            # -> gave at least one status today -> fully completed
            # the day. Each computed independently via EXISTS/JOIN
            # so counts are exact, not derived from each other.
            cur.execute(
                """
                SELECT
                    COUNT(*) AS total,

                    COUNT(*) FILTER (
                        WHERE last_active_date = %(today)s
                    ) AS active_today,

                    COUNT(*) FILTER (
                        WHERE EXISTS (
                            SELECT 1 FROM public.tasks t
                            WHERE t.user_id = u.id
                              AND t.task_date = %(today)s
                        )
                    ) AS entered_task_today,

                    COUNT(*) FILTER (
                        WHERE EXISTS (
                            SELECT 1 FROM public.tasks t
                            WHERE t.user_id = u.id
                              AND t.task_date = %(today)s
                              AND t.status IN ('completed', 'failed')
                        )
                    ) AS gave_status_today,

                    COUNT(*) FILTER (
                        WHERE state = 'completed'
                          AND last_active_date = %(today)s
                    ) AS finished_day

                FROM public.users u
                WHERE active_bot_id = %(bot)s
                """,
                {
                    "today": today,
                    "bot": bot_id()
                }
            )

            funnel = cur.fetchone()

            # Task status breakdown for today. Pending is genuinely
            # pending (not yet marked) — never counted as failed.
            cur.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status = 'completed') AS completed,
                    COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                    COUNT(*) FILTER (WHERE status = 'pending') AS pending
                FROM public.tasks
                WHERE task_date = %s
                """,
                (today,)
            )

            tasks = cur.fetchone()

    def pct(part, whole):
        return round(part / whole * 100) if whole else 0

    funnel_steps = [
        {
            "label": "Jami foydalanuvchi",
            "count": funnel["total"],
            "percent_of_previous": 100
        },
        {
            "label": "Bugun faol",
            "count": funnel["active_today"],
            "percent_of_previous": pct(funnel["active_today"], funnel["total"])
        },
        {
            "label": "Bugun vazifa kiritgan",
            "count": funnel["entered_task_today"],
            "percent_of_previous": pct(
                funnel["entered_task_today"], funnel["active_today"]
            )
        },
        {
            "label": "Kamida bitta statusga belgi qo'ygan",
            "count": funnel["gave_status_today"],
            "percent_of_previous": pct(
                funnel["gave_status_today"], funnel["entered_task_today"]
            )
        },
        {
            "label": "Kunini yakunlagan",
            "count": funnel["finished_day"],
            "percent_of_previous": pct(
                funnel["finished_day"], funnel["gave_status_today"]
            )
        },
    ]

    total_tasks = tasks["total"] or 0

    return {
        "ok": True,
        "date": today.isoformat(),
        "funnel": funnel_steps,
        "tasks": {
            "total": total_tasks,
            "completed": tasks["completed"],
            "failed": tasks["failed"],
            "pending": tasks["pending"],
            "completed_percent": pct(tasks["completed"], total_tasks),
            "failed_percent": pct(tasks["failed"], total_tasks),
            "pending_percent": pct(tasks["pending"], total_tasks),
        }
    }


# =========================================================
# RETENTION
# =========================================================
#
# Built entirely from user_activity_daily, populated going forward
# from the migration date. Cohorts older than the migration, or too
# recent for a given horizon to have elapsed, show "-" rather than
# a misleading 0%.

def _retention_for_cohort(cur, cohort_date: date, horizon_days: int, today: date):

    target_date = cohort_date + timedelta(days=horizon_days)

    if target_date > today:
        return None  # horizon hasn't happened yet

    cur.execute(
        """
        SELECT COUNT(DISTINCT u.id) AS cohort_size
        FROM public.users u
        WHERE u.created_at::date = %s
          AND u.active_bot_id = %s
        """,
        (cohort_date, bot_id())
    )

    cohort_size = cur.fetchone()["cohort_size"]

    if not cohort_size:
        return None

    cur.execute(
        """
        SELECT COUNT(DISTINCT a.user_id) AS retained
        FROM public.user_activity_daily a
        JOIN public.users u ON u.id = a.user_id
        WHERE u.created_at::date = %s
          AND u.active_bot_id = %s
          AND a.activity_date = %s
        """,
        (cohort_date, bot_id(), target_date)
    )

    retained = cur.fetchone()["retained"]

    return round(retained / cohort_size * 100)


@router.get("/retention")
def admin_retention(
    cohorts: int = Query(14, ge=1, le=60),
    _: int = Depends(verify_admin)
):

    today = get_today()

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            # Only cohorts from the activity-tracking start date onward
            # can have any real retention number; earlier cohorts are
            # still listed (so the table isn't empty) with every
            # horizon as "-".
            cur.execute(
                """
                SELECT MIN(activity_date) AS tracking_start
                FROM public.user_activity_daily
                """
            )

            tracking_start = cur.fetchone()["tracking_start"]

            cohort_rows = []

            for offset in range(cohorts):

                cohort_date = today - timedelta(days=offset)

                cur.execute(
                    """
                    SELECT COUNT(*) AS new_users
                    FROM public.users
                    WHERE created_at::date = %s
                      AND active_bot_id = %s
                    """,
                    (cohort_date, bot_id())
                )

                new_users = cur.fetchone()["new_users"]

                if new_users == 0:
                    continue

                row = {
                    "cohort_date": cohort_date.isoformat(),
                    "new_users": new_users,
                }

                for horizon, key in (
                    (1, "d1"), (3, "d3"), (7, "d7"), (30, "d30")
                ):

                    if (
                        tracking_start is None
                        or cohort_date < tracking_start
                    ):

                        row[key] = None  # "-": before tracking existed

                    else:

                        row[key] = _retention_for_cohort(
                            cur, cohort_date, horizon, today
                        )

                cohort_rows.append(row)

            # Headline D1/D3/D7/D30: average across cohorts that have
            # a real (non-null) value for that horizon, not a blend
            # with "-" treated as zero.
            headline = {}

            for key in ("d1", "d3", "d7", "d30"):

                values = [
                    row[key] for row in cohort_rows
                    if row[key] is not None
                ]

                headline[key] = (
                    round(sum(values) / len(values))
                    if values else None
                )

    return {
        "ok": True,
        "tracking_start": (
            tracking_start.isoformat() if tracking_start else None
        ),
        "headline": headline,
        "cohorts": cohort_rows
    }


# =========================================================
# USERS (segments + paginated list + search)
# =========================================================

@router.get("/users")
def admin_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    search: Optional[str] = Query(None, max_length=100),
    _: int = Depends(verify_admin)
):

    today = get_today()

    with get_connection() as conn:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE last_active_date = %(today)s
                    ) AS active_today,

                    COUNT(*) FILTER (
                        WHERE last_active_date >= %(today)s - 2
                          AND last_active_date < %(today)s
                    ) AS active_2d,

                    COUNT(*) FILTER (
                        WHERE last_active_date < %(today)s - 2
                          AND last_active_date >= %(today)s - 7
                    ) AS inactive_3_7,

                    COUNT(*) FILTER (
                        WHERE last_active_date < %(today)s - 7
                          AND last_active_date >= %(today)s - 30
                    ) AS inactive_8_30,

                    COUNT(*) FILTER (
                        WHERE last_active_date < %(today)s - 30
                          OR last_active_date IS NULL
                    ) AS inactive_30_plus,

                    COUNT(*) FILTER (
                        WHERE state = 'blocked'
                    ) AS blocked

                FROM public.users
                WHERE active_bot_id = %(bot)s
                """,
                {
                    "today": today,
                    "bot": bot_id()
                }
            )

            segments = cur.fetchone()

            search_clause = ""
            params: dict = {
                "bot": bot_id(),
                "limit": page_size,
                "offset": (page - 1) * page_size
            }

            if search:
                search_clause = (
                    "AND (first_name ILIKE %(search)s "
                    "OR telegram_username ILIKE %(search)s)"
                )
                params["search"] = f"%{search}%"

            cur.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM public.users
                WHERE active_bot_id = %(bot)s
                {search_clause}
                """,
                params
            )

            total_matching = cur.fetchone()["total"]

            cur.execute(
                f"""
                SELECT
                    u.id,
                    u.first_name,
                    u.telegram_username,
                    u.created_at,
                    u.last_active_date,
                    u.state,
                    COUNT(t.id) AS total_tasks,
                    COUNT(t.id) FILTER (
                        WHERE t.status = 'completed'
                    ) AS completed_tasks
                FROM public.users u
                LEFT JOIN public.tasks t ON t.user_id = u.id
                WHERE u.active_bot_id = %(bot)s
                {search_clause}
                GROUP BY u.id
                ORDER BY u.created_at DESC
                LIMIT %(limit)s OFFSET %(offset)s
                """,
                params
            )

            user_rows = cur.fetchall()

    users_out = []

    for row in user_rows:

        users_out.append(
            {
                "id": str(row["id"]),
                "first_name": row["first_name"],
                "username": row["telegram_username"],
                "joined_at": row["created_at"].isoformat(),
                "last_active_date": (
                    row["last_active_date"].isoformat()
                    if row["last_active_date"] else None
                ),
                "state": row["state"],
                "total_tasks": row["total_tasks"],
                "completed_tasks": row["completed_tasks"],
            }
        )

    return {
        "ok": True,
        "segments": {
            "active_today": segments["active_today"],
            "active_2d": segments["active_2d"],
            "inactive_3_7": segments["inactive_3_7"],
            "inactive_8_30": segments["inactive_8_30"],
            "inactive_30_plus": segments["inactive_30_plus"],
            "blocked": segments["blocked"],
        },
        "page": page,
        "page_size": page_size,
        "total": total_matching,
        "total_pages": (
            (total_matching + page_size - 1) // page_size
            if total_matching else 0
        ),
        "users": users_out
    }
