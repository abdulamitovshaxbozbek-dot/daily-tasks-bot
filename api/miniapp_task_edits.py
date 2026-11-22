"""Mini App task editing and deletion routes."""

from datetime import timedelta

from fastapi import Depends, HTTPException
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel, Field


class MiniappTaskEditRequest(BaseModel):
    task_text: str = Field(min_length=1, max_length=1000)


def install(
    router, *, get_miniapp_chat_id, get_user_by_chat_id, get_today,
    get_connection, clean_task_text, uzbek_to_latin, normalize_task,
    refresh_live_checklist,
):
    """Register task editing routes with the bot's shared services."""
    def _miniapp_change_task(chat_id, task_id, new_text=None):
        """Faqat egasining bugungi/ertangi pending vazifasini tahrirlaydi yoki o'chiradi."""
        user = get_user_by_chat_id(chat_id)
        if not user or user["state"] == "blocked":
            raise HTTPException(status_code=403, detail="Vazifalarni boshqarish uchun botda /start bosing.")
        if new_text is not None:
            new_text = uzbek_to_latin(clean_task_text(new_text))
            if not new_text or len(new_text) > 1000 or "\n" in new_text or "\r" in new_text:
                raise HTTPException(status_code=400, detail="Vazifani 1–1000 belgili bitta qatorda yozing.")
        today = get_today()
        with get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id FROM public.users WHERE id = %s FOR UPDATE", (user["id"],))
                cur.execute("""
                    SELECT id, task_date FROM public.tasks
                    WHERE id::text = %s AND user_id = %s AND status = 'pending'
                      AND task_date IN (%s, %s) FOR UPDATE
                """, (task_id, user["id"], today, today + timedelta(days=1)))
                task = cur.fetchone()
                if not task:
                    raise HTTPException(status_code=404, detail="Vazifa topilmadi, belgilangan yoki sanasi o‘tgan. Ro‘yxatni yangilang.")
                if new_text is None:
                    cur.execute("DELETE FROM public.tasks WHERE id::text = %s AND user_id = %s", (task_id, user["id"]))
                else:
                    cur.execute("""
                        SELECT task_text FROM public.tasks
                        WHERE user_id = %s AND task_date = %s AND id::text <> %s
                    """, (user["id"], task["task_date"], task_id))
                    if normalize_task(new_text) in {normalize_task(row["task_text"]) for row in cur.fetchall()}:
                        raise HTTPException(status_code=409, detail="Shu sanada bunday vazifa allaqachon bor.")
                    cur.execute("UPDATE public.tasks SET task_text = %s WHERE id::text = %s AND user_id = %s",
                                (new_text, task_id, user["id"]))
            conn.commit()
        checklist_updated = True
        if task["task_date"] == today:
            try:
                refresh_live_checklist(chat_id, user)
            except Exception as error:
                # Baza saqlangan: Telegram xatosi amalni qayta bajarishga olib kelmasin.
                checklist_updated = False
                print("Miniapp checklist update error:", type(error).__name__)
        return {"ok": True, "task_id": task_id, "checklist_updated": checklist_updated}


    @router.patch("/miniapp/tasks/{task_id}")
    def miniapp_edit_task(task_id: str, payload: MiniappTaskEditRequest, chat_id: int = Depends(get_miniapp_chat_id)):
        """Telegram imzosi bilan tekshirilgan foydalanuvchining vazifa matnini yangilaydi."""
        return _miniapp_change_task(chat_id, task_id, payload.task_text)


    @router.delete("/miniapp/tasks/{task_id}")
    def miniapp_delete_task(task_id: str, chat_id: int = Depends(get_miniapp_chat_id)):
        """Mini Appda tasdiqlangan o'chirishni foydalanuvchi huquqlari bilan bajaradi."""
        return _miniapp_change_task(chat_id, task_id)
