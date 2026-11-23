"""Mini App routes for setting and clearing task reminders."""

from datetime import timedelta

from fastapi import Depends, HTTPException
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel, Field


class MiniappTaskReminderRequest(BaseModel):
    reminder_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


def install(router, *, get_miniapp_chat_id, get_user_by_chat_id, get_today, get_connection):
    """Register reminder routes using the bot's shared services."""
    @router.put("/miniapp/tasks/{task_id}/reminder")
    def miniapp_set_task_reminder(
        task_id: str,
        payload: MiniappTaskReminderRequest,
        chat_id: int = Depends(get_miniapp_chat_id)
    ):
        """Bugungi yoki ertangi pending vazifaga Toshkent vaqti bilan eslatma qo'yadi."""
        user = get_user_by_chat_id(chat_id)
        if not user or user["state"] == "blocked":
            raise HTTPException(status_code=403, detail="Eslatma qo‘yish uchun botda /start bosing.")
        today = get_today()
        with get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    UPDATE public.tasks
                    SET reminder_time = %s::time, reminder_sent_at = NULL
                    WHERE id::text = %s AND user_id = %s AND status = 'pending'
                      AND task_date IN (%s, %s)
                    RETURNING id, task_date, reminder_time
                """, (payload.reminder_time, task_id, user["id"], today, today + timedelta(days=1)))
                task = cur.fetchone()
            conn.commit()
        if not task:
            raise HTTPException(status_code=404, detail="Vazifa topilmadi, belgilangan yoki sanasi o‘tgan.")
        return {"ok": True, "task_id": str(task["id"]), "reminder_time": str(task["reminder_time"])[:5]}


    @router.delete("/miniapp/tasks/{task_id}/reminder")
    def miniapp_clear_task_reminder(task_id: str, chat_id: int = Depends(get_miniapp_chat_id)):
        """Vazifaning eslatmasini o‘chiradi; vazifaning o‘zi saqlanadi."""
        user = get_user_by_chat_id(chat_id)
        if not user or user["state"] == "blocked":
            raise HTTPException(status_code=403, detail="Eslatmani boshqarish uchun botda /start bosing.")
        today = get_today()
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE public.tasks
                    SET reminder_time = NULL, reminder_sent_at = NULL
                    WHERE id::text = %s AND user_id = %s AND status = 'pending'
                      AND task_date IN (%s, %s)
                    RETURNING id
                """, (task_id, user["id"], today, today + timedelta(days=1)))
                task = cur.fetchone()
            conn.commit()
        if not task:
            raise HTTPException(status_code=404, detail="Vazifa topilmadi, belgilangan yoki sanasi o‘tgan.")
        return {"ok": True, "task_id": task_id}
