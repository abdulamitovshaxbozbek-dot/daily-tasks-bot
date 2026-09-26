"""Admin paneli: faqat joriy botga tegishli, bazada mavjud ma'lumotlar statistikasi."""
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg2.extras import RealDictCursor
from database import get_connection
from bot_identity import bot_id
from routes import ADMIN_CHAT_ID, get_miniapp_chat_id, fail_reason_display, FAIL_REASONS

router = APIRouter(prefix="/api/admin")


def verify_admin(chat_id: int = Depends(get_miniapp_chat_id)) -> int:
    """Telegram imzosidan olingan shaxsni tekshiradi; mijoz yuborgan rolga ishonmaydi."""
    if str(chat_id) != str(ADMIN_CHAT_ID):
        raise HTTPException(status_code=403, detail="Admin huquqi yo‘q")
    return chat_id


def _context(cur):
    """Hisoblarni bir xil baza snapshoti va Toshkent sanasida o'qish uchun tayyorlaydi."""
    cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    cur.execute("SELECT CURRENT_TIMESTAMP AS generated_at, (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Tashkent')::date AS today")
    clock = cur.fetchone()
    current_bot = bot_id()
    if current_bot is None or str(current_bot).strip() == "":
        raise HTTPException(status_code=503, detail="Bot identifikatori hali tayyor emas.")
    # users.created_at turi PostgreSQL'da timestamptz ekanligi tekshirilgan.
    created_date = "(u.created_at AT TIME ZONE 'Asia/Tashkent')::date"
    today = clock["today"]
    params = {"bot": current_bot, "today": today, "yesterday": today - timedelta(days=1),
              "tomorrow": today + timedelta(days=1), "week_start": today - timedelta(days=6)}
    # Faqat joriy bot userlari. Faollik dublikatlari UNION orqali yo'qotiladi.
    cte = f"""WITH scoped_users AS (
        SELECT u.*, {created_date} AS joined_date
        FROM public.users u WHERE u.active_bot_id = %(bot)s
    ), recorded_activity AS (
        SELECT a.user_id, a.activity_date
        FROM public.user_activity_daily a JOIN scoped_users u ON u.id = a.user_id
        WHERE a.activity_date <= %(today)s
        UNION
        SELECT id, last_active_date FROM scoped_users
        WHERE last_active_date <= %(today)s
    ), scoped_tasks AS (
        SELECT t.* FROM public.tasks t JOIN scoped_users u ON u.id = t.user_id
    ) """
    meta = {"date": today.isoformat(), "timezone": "Asia/Tashkent",
            "generated_at": clock["generated_at"].isoformat(),
            "signup_dates_available": True,
            "signup_date_note": None,
            "scope_note": "Joriy botga biriktirilgan foydalanuvchilar. Faollik — botda qayd etilgan harakat; Mini Appni shunchaki ochish hisoblanmaydi. Qo‘shilgan sana — bazadagi ro‘yxatdan o‘tish sanasi, yangi botga ko‘chgan sana emas.",
            "task_note": "Vazifalar belgilangan sanasi bo‘yicha hisoblanadi. Faqat hozir bazada mavjud yozuvlar: o‘chirilgan vazifalar va tahrirlash tarixi hisoblanmaydi."}
    return params, cte, meta


def _percent(part, whole):
    """Maxraj nol bo'lsa mavjud bo'lmagan foiz o'rniga None qaytaradi."""
    return round(100 * part / whole, 1) if whole else None


@router.get("/me")
def admin_me(chat_id: int = Depends(verify_admin)):
    """Admin kirish huquqini tasdiqlaydi."""
    return {"ok": True, "chat_id": chat_id}


@router.get("/overview")
def admin_overview(_: int = Depends(verify_admin)):
    """Bugungi va ertangi rejalarni ajratadi; kun yopilishiga bog'liq hisob ishlatmaydi."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            p, cte, meta = _context(cur)
            cur.execute(cte + """
                SELECT COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE joined_date = %(today)s) AS today_new,
                    COUNT(*) FILTER (WHERE joined_date = %(yesterday)s) AS yesterday_new,
                    COUNT(*) FILTER (WHERE joined_date BETWEEN %(week_start)s AND %(today)s) AS week_new,
                    COUNT(*) FILTER (WHERE state = 'blocked') AS blocked,
                    COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM recorded_activity a
                        WHERE a.user_id = u.id AND a.activity_date = %(today)s)) AS today_active,
                    COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM recorded_activity a
                        WHERE a.user_id = u.id AND a.activity_date BETWEEN %(week_start)s AND %(today)s)) AS week_active
                FROM scoped_users u
            """, p)
            users = dict(cur.fetchone())
            if not meta["signup_dates_available"]:
                for key in ("today_new", "yesterday_new", "week_new"):
                    users[key] = None
            cur.execute(cte + """
                SELECT COUNT(*) FILTER (WHERE task_date = %(today)s) AS today_total,
                    COUNT(DISTINCT user_id) FILTER (WHERE task_date = %(today)s) AS today_users_with_tasks,
                    COUNT(*) FILTER (WHERE task_date = %(tomorrow)s) AS tomorrow_total,
                    COUNT(DISTINCT user_id) FILTER (WHERE task_date = %(tomorrow)s) AS tomorrow_users_with_tasks
                FROM scoped_tasks
            """, p)
            tasks = dict(cur.fetchone())
            cur.execute(cte + """
                SELECT COUNT(*) AS all_marked_users,
                    COUNT(*) FILTER (WHERE all_completed) AS all_completed_users
                FROM (
                    SELECT user_id, BOOL_AND(status = 'completed') AS all_completed
                    FROM scoped_tasks WHERE task_date = %(today)s GROUP BY user_id
                    HAVING BOOL_AND(COALESCE(status IN ('completed', 'failed'), false))
                ) marked
            """, p)
            users.update(dict(cur.fetchone()))
            cur.execute(cte + """
                SELECT COUNT(*) AS pending_date_choices FROM public.pending_late_tasks p
                JOIN scoped_users u ON u.telegram_chat_id = p.chat_id
            """, p)
            tasks.update(dict(cur.fetchone()))
            # Kartalar va grafik bitta snapshotdan olinadi.
            p["start"] = p["week_start"]
            cur.execute(cte + """
                SELECT d::date AS day,
                    (SELECT COUNT(*) FROM scoped_users u WHERE u.joined_date = d::date) AS new_users,
                    (SELECT COUNT(*) FROM recorded_activity a WHERE a.activity_date = d::date) AS active_users
                FROM generate_series(%(start)s::date, %(today)s::date, interval '1 day') d ORDER BY d
            """, p)
            trends = {"days": [{"date": r["day"].isoformat(), "new_users": r["new_users"],
                                "active_users": r["active_users"]} for r in cur.fetchall()]}
    return {"ok": True, **meta, "users": users, "tasks": tasks, "trends": trends}


@router.get("/trends")
def admin_trends(days: int = Query(7, ge=1, le=90), _: int = Depends(verify_admin)):
    """Joriy botning kunlik ro'yxatdan o'tish va qayd etilgan faollik dinamikasini beradi."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            p, cte, meta = _context(cur)
            p["start"] = p["today"] - timedelta(days=days - 1)
            cur.execute(cte + """
                SELECT d::date AS day,
                    (SELECT COUNT(*) FROM scoped_users u WHERE u.joined_date = d::date) AS new_users,
                    (SELECT COUNT(*) FROM recorded_activity a WHERE a.activity_date = d::date) AS active_users
                FROM generate_series(%(start)s::date, %(today)s::date, interval '1 day') d ORDER BY d
            """, p)
            rows = [{"date": r["day"].isoformat(), "new_users": r["new_users"] if meta["signup_dates_available"] else None,
                     "active_users": r["active_users"]} for r in cur.fetchall()]
    return {"ok": True, **meta, "days": rows}


@router.get("/activity")
def admin_activity(_: int = Depends(verify_admin)):
    """Mustaqil guruhlarni funnel deb ko'rsatmaydi; sabablar va pending vazifalarni ajratadi."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            p, cte, meta = _context(cur)
            cur.execute(cte + """
                SELECT COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM recorded_activity a WHERE a.user_id=u.id AND a.activity_date=%(today)s)) AS active_today,
                    COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM scoped_tasks t WHERE t.user_id=u.id AND t.task_date=%(today)s)) AS has_plan,
                    COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM scoped_tasks t WHERE t.user_id=u.id AND t.task_date=%(today)s AND t.status IN ('completed','failed'))) AS has_marked,
                    COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM scoped_tasks t WHERE t.user_id=u.id AND t.task_date=%(today)s)
                        AND NOT EXISTS (SELECT 1 FROM scoped_tasks t WHERE t.user_id=u.id AND t.task_date=%(today)s AND (t.status IS NULL OR t.status NOT IN ('completed','failed')))) AS all_marked,
                    COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM scoped_tasks t WHERE t.user_id=u.id AND t.task_date=%(today)s)
                        AND NOT EXISTS (SELECT 1 FROM scoped_tasks t WHERE t.user_id=u.id AND t.task_date=%(today)s AND t.status IS DISTINCT FROM 'completed')) AS all_completed
                FROM scoped_users u
            """, p)
            groups = dict(cur.fetchone())
            cur.execute(cte + """
                SELECT COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status='completed') AS completed,
                    COUNT(*) FILTER (WHERE status='failed') AS failed,
                    COUNT(*) FILTER (WHERE status='pending') AS pending,
                    COUNT(*) FILTER (WHERE status IS NULL OR status NOT IN ('completed','failed','pending')) AS unknown,
                    COUNT(*) FILTER (WHERE status='failed' AND fail_reason IS NULL) AS missing_reason,
                    COUNT(*) FILTER (WHERE status='failed' AND fail_reason='other' AND NULLIF(BTRIM(fail_reason_text),'') IS NOT NULL) AS custom_reason,
                    COUNT(*) FILTER (WHERE status='failed' AND fail_reason='other' AND NULLIF(BTRIM(fail_reason_text),'') IS NULL) AS other_without_text
                FROM scoped_tasks WHERE task_date=%(today)s
            """, p)
            tasks = dict(cur.fetchone())
            cur.execute(cte + """
                SELECT fail_reason, COUNT(*) AS count FROM scoped_tasks
                WHERE task_date=%(today)s AND status='failed'
                GROUP BY fail_reason ORDER BY count DESC, fail_reason NULLS LAST
            """, p)
            reasons = [{"code": r["fail_reason"], "label": (fail_reason_display(r["fail_reason"]) if r["fail_reason"] is None or r["fail_reason"] in FAIL_REASONS else "Noma’lum sabab kodi: " + r["fail_reason"]), "count": r["count"]} for r in cur.fetchall()]
            cur.execute(cte + """
                SELECT t.id, t.task_text, t.fail_reason_text, u.first_name
                FROM scoped_tasks t JOIN scoped_users u ON u.id=t.user_id
                WHERE t.task_date=%(today)s AND t.status='failed' AND t.fail_reason='other'
                  AND NULLIF(BTRIM(t.fail_reason_text),'') IS NOT NULL
                ORDER BY t.created_at DESC, t.id DESC LIMIT 50
            """, p)
            custom = [{"task_id": str(r["id"]), "task_text": r["task_text"], "reason": r["fail_reason_text"],
                       "first_name": r["first_name"]} for r in cur.fetchall()]
    metrics = [{"label": label, "count": groups[key], "percent_of_total": _percent(groups[key], groups["total"])}
               for key, label in (("active_today", "Bugun botda faol"), ("has_plan", "Bugunga vazifasi bor"),
                                  ("has_marked", "Bugungi kamida 1 vazifasi belgilangan"),
                                  ("all_marked", "Bugungi barcha vazifasi belgilangan"),
                                  ("all_completed", "Bugungi barcha vazifasi bajarilgan"))]
    for key in ("completed", "failed", "pending", "unknown"):
        tasks[key + "_percent"] = _percent(tasks[key], tasks["total"])
    return {"ok": True, **meta, "total_users": groups["total"], "metrics": metrics, "tasks": tasks,
            "reasons": reasons, "custom_reasons": custom, "custom_reasons_limit": 50}


@router.get("/retention")
def admin_retention(cohorts: int = Query(60, ge=1, le=90), _: int = Depends(verify_admin)):
    """Aniq D-kundagi qaytishni hisoblaydi; tugamagan kunni kutadi va guruh hajmi bo'yicha vaznlaydi."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            p, cte, meta = _context(cur)
            p["start"] = p["today"] - timedelta(days=cohorts - 1)
            cur.execute(cte + """
                SELECT MIN(a.activity_date) AS first_observed FROM public.user_activity_daily a
                JOIN scoped_users u ON u.id=a.user_id WHERE a.activity_date <= %(today)s
            """, p)
            first = cur.fetchone()["first_observed"]
            cur.execute(cte + """
                SELECT joined_date, COUNT(*) AS size FROM scoped_users
                WHERE joined_date BETWEEN %(start)s AND %(today)s GROUP BY joined_date ORDER BY joined_date DESC
            """, p)
            sizes = cur.fetchall()
            cur.execute(cte + """
                SELECT u.joined_date, a.activity_date-u.joined_date AS horizon, COUNT(DISTINCT u.id) AS retained
                FROM scoped_users u JOIN public.user_activity_daily a ON a.user_id=u.id
                WHERE u.joined_date BETWEEN %(start)s AND %(today)s
                  AND a.activity_date < %(today)s
                  AND a.activity_date-u.joined_date IN (1,3,7,30)
                GROUP BY u.joined_date, a.activity_date-u.joined_date
            """, p)
            returns = {(r["joined_date"], r["horizon"]): r["retained"] for r in cur.fetchall()}
    totals = {f"d{h}": {"retained": 0, "eligible": 0} for h in (1, 3, 7, 30)}
    rows = []
    for group in sizes:
        day, size = group["joined_date"], group["size"]
        row = {"cohort_date": day.isoformat(), "new_users": size}
        for h in (1, 3, 7, 30):
            key = f"d{h}"
            # Eng birinchi yozuv kuni to'liq kuzatilganiga kafolat yo'q.
            eligible = first is not None and day > first and day + timedelta(days=h) < p["today"]
            returned = returns.get((day, h), 0)
            row[key] = _percent(returned, size) if eligible else None
            if eligible:
                totals[key]["retained"] += returned
                totals[key]["eligible"] += size
        rows.append(row)
    headline = {key: _percent(value["retained"], value["eligible"]) for key, value in totals.items()}
    return {"ok": True, **meta, "tracking_start": first.isoformat() if first else None,
            "headline": headline, "headline_counts": totals, "cohorts": rows, "window_days": cohorts,
            "retention_note": "D1/D3/D7/D30 — ro‘yxatdan o‘tgandan aniq shu kun o‘tib botda qayd etilgan faollik. Bugun hali tugamagan bo‘lsa hisoblanmaydi. Umumiy foiz foydalanuvchilar soni bilan vaznlangan. Tarixiy yozuvlar to‘liq yig‘ilgan davrlar uchungina ishonchli; ilk yozuv kuzatuv boshlanganining kafolati emas."}


@router.get("/users")
def admin_users(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                search: Optional[str] = Query(None, max_length=100), _: int = Depends(verify_admin)):
    """Segmentlarni ustma-ust hisoblamaydi; bloklanganlar va kelajak vazifalari alohida ko'rsatiladi."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            p, cte, meta = _context(cur)
            cte = cte.rstrip() + """, user_seen AS (
                SELECT u.*, (SELECT MAX(a.activity_date) FROM recorded_activity a WHERE a.user_id=u.id) AS seen
                FROM scoped_users u
            ) """
            cur.execute(cte + """
                SELECT COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE state='blocked') AS blocked,
                    COUNT(*) FILTER (WHERE state IS DISTINCT FROM 'blocked' AND seen=%(today)s) AS active_today,
                    COUNT(*) FILTER (WHERE state IS DISTINCT FROM 'blocked' AND %(today)s-seen BETWEEN 1 AND 2) AS active_2d,
                    COUNT(*) FILTER (WHERE state IS DISTINCT FROM 'blocked' AND %(today)s-seen BETWEEN 3 AND 7) AS inactive_3_7,
                    COUNT(*) FILTER (WHERE state IS DISTINCT FROM 'blocked' AND %(today)s-seen BETWEEN 8 AND 30) AS inactive_8_30,
                    COUNT(*) FILTER (WHERE state IS DISTINCT FROM 'blocked' AND %(today)s-seen > 30) AS inactive_30_plus,
                    COUNT(*) FILTER (WHERE state IS DISTINCT FROM 'blocked' AND seen IS NULL) AS never_active
                FROM user_seen
            """, p)
            segments = dict(cur.fetchone())
            clause = ""
            if search and search.strip():
                # % va _ qidiruvda oddiy belgi, yashirin wildcard emas.
                p["search"] = '%' + search.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
                clause = "AND (u.first_name ILIKE %(search)s OR u.telegram_username ILIKE %(search)s OR u.telegram_chat_id::text ILIKE %(search)s)"
            cur.execute(cte + f"SELECT COUNT(*) AS total FROM user_seen u WHERE true {clause}", p)
            total = cur.fetchone()["total"]
            pages = (total + page_size - 1) // page_size
            page = min(page, max(1, pages))
            p.update(limit=page_size, offset=(page-1)*page_size)
            cur.execute(cte + f"""
                SELECT u.id, u.first_name, u.telegram_username, u.joined_date, u.seen, u.state,
                    u.morning_time, u.last_morning_greeting_date,
                    COUNT(t.id) FILTER (WHERE t.task_date <= %(today)s) AS total_tasks,
                    COUNT(t.id) FILTER (WHERE t.task_date <= %(today)s AND t.status='completed') AS completed_tasks,
                    COUNT(t.id) FILTER (WHERE t.task_date > %(today)s) AS future_tasks
                FROM user_seen u LEFT JOIN scoped_tasks t ON t.user_id=u.id
                WHERE true {clause}
                GROUP BY u.id, u.first_name, u.telegram_username, u.joined_date, u.seen, u.state,
                         u.morning_time, u.last_morning_greeting_date, u.created_at
                ORDER BY u.created_at DESC NULLS LAST, u.id DESC LIMIT %(limit)s OFFSET %(offset)s
            """, p)
            rows = cur.fetchall()
    users = [{"id": str(r["id"]), "first_name": r["first_name"], "username": r["telegram_username"],
              "joined_at": r["joined_date"].isoformat() if r["joined_date"] else None,
              "last_active_date": r["seen"].isoformat() if r["seen"] else None, "state": r["state"],
              "morning_time": str(r["morning_time"])[:5] if r["morning_time"] else None,
              "last_morning_greeting_date": r["last_morning_greeting_date"].isoformat() if r["last_morning_greeting_date"] else None,
              "total_tasks": r["total_tasks"], "completed_tasks": r["completed_tasks"], "future_tasks": r["future_tasks"]} for r in rows]
    return {"ok": True, **meta, "segments": segments, "page": page, "page_size": page_size,
            "total": total, "total_pages": pages, "users": users}
