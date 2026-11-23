"""Mini App task creation route."""

import hashlib
import json
from datetime import date, timedelta

from fastapi import Depends, HTTPException
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel, Field


class MiniappTomorrowTaskRequest(BaseModel):
    task_text: str = Field(min_length=1, max_length=10000)
    task_date: date
    request_id: str = Field(min_length=36, max_length=36)


def install(
    router, *,
    get_miniapp_chat_id,
    get_user_by_chat_id,
    require_joined,
    bot_id,
    uzbek_to_latin,
    clean_task_text,
    get_today,
    get_connection,
    normalize_task,
    format_uz_date,
    telegram_send_message_with_keyboard,
    MINIAPP_URL,
    refresh_live_checklist,
):
    """Register task creation using the bot's shared services."""
    @router.post('/miniapp/tasks')
    def miniapp_create_tomorrow_task(payload: MiniappTomorrowTaskRequest, chat_id: int = Depends(get_miniapp_chat_id)):
        """Authenticated, idempotent today/tomorrow planning; notifications follow commit."""
        user = get_user_by_chat_id(chat_id)
        if not user or user['state']=='blocked' or (require_joined() and user.get('active_bot_id')!=bot_id()):
            raise HTTPException(status_code=403, detail='Vazifa qo‘shish uchun botda /start bosing.')
        from uuid import UUID
        try:
            request_id=str(UUID(payload.request_id))
        except ValueError:
            raise HTTPException(status_code=422, detail='So‘rov identifikatori noto‘g‘ri.')
        tasks = [uzbek_to_latin(clean_task_text(line)) for line in payload.task_text.splitlines() if line.strip()]
        if not tasks or len(tasks)>20 or len(payload.task_text)>10000 or any(not task or len(task)>1000 for task in tasks):
            raise HTTPException(status_code=422, detail='Har bir vazifani yangi qatorda yozing: 20 tagacha, har biri 1000 belgigacha.')
        fingerprint=hashlib.sha256((payload.task_date.isoformat()+'\n'+'\n'.join(tasks)).encode()).hexdigest()
        today=get_today()
        with get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Same user lock as edit/carry operations prevents parallel duplicate saves.
                cur.execute('SELECT id FROM public.users WHERE id=%s FOR UPDATE', (user['id'],))
                cur.execute("SELECT input_hash,result FROM public.qadam_miniapp_task_requests WHERE bot_id=%s AND user_id=%s AND request_id=%s", (bot_id(),str(user['id']),request_id))
                cached=cur.fetchone()
                if cached:
                    if cached['input_hash']!=fingerprint:
                        raise HTTPException(status_code=409, detail='O‘zgargan vazifa uchun yangi so‘rov yuboring.')
                    return cached['result']
                if payload.task_date not in (today, today+timedelta(days=1)):
                    raise HTTPException(status_code=422, detail='Faqat bugun yoki ertaga qo‘shish mumkin. Kun ro‘yxatini qayta oching.')
                cur.execute('SELECT EXISTS (SELECT 1 FROM public.tasks WHERE user_id=%s) AS has_tasks', (user['id'],))
                first_task=not cur.fetchone()['has_tasks']
                cur.execute('SELECT id,task_text FROM public.tasks WHERE user_id=%s AND task_date=%s', (user['id'],payload.task_date))
                existing={normalize_task(row['task_text']):row['id'] for row in cur.fetchall()}
                task_ids=[]; added_tasks=[]; duplicate_count=0
                for text in tasks:
                    normalized=normalize_task(text)
                    if normalized in existing:
                        task_id=existing[normalized]; duplicate_count+=1
                    else:
                        cur.execute("INSERT INTO public.tasks(user_id,task_text,task_date,status) VALUES(%s,%s,%s,'pending') RETURNING id", (user['id'],text,payload.task_date))
                        task_id=cur.fetchone()['id']; existing[normalized]=task_id; added_tasks.append(text)
                    task_ids.append(str(task_id))
                if added_tasks and payload.task_date == today:
                    cur.execute('UPDATE public.users SET last_completion_notified_date=NULL WHERE id=%s', (user['id'],))
                offer_morning=first_task and bool(added_tasks) and user.get('morning_time') is None
                cur.execute("UPDATE public.users SET last_active_date=%s WHERE id=%s", (today, user["id"]))
                result={'ok':True,'task_id':task_ids[0],'task_ids':task_ids,'added':len(added_tasks),'duplicates':duplicate_count,
                        'task_date':payload.task_date.isoformat(),'duplicate':not added_tasks,'chat_notified':False}
                cur.execute("INSERT INTO public.qadam_miniapp_task_requests(bot_id,user_id,request_id,input_hash,result) VALUES(%s,%s,%s,%s,%s::jsonb)", (bot_id(),str(user['id']),request_id,fingerprint,json.dumps(result)))
            conn.commit()
        if added_tasks:
            target_label = 'Bugungi' if payload.task_date == today else 'Ertangi'
            confirmation=(f"✅ {target_label} rejangizga qo‘shildi:" if len(added_tasks)==1 else f"✅ {target_label} rejangizga {len(added_tasks)} ta vazifa qo‘shildi:")
            confirmation+='\n'+'\n'.join(f"{i}. {text[:120]}{'…' if len(text)>120 else ''}" for i,text in enumerate(added_tasks,1))
            confirmation+=f"\n📅 {format_uz_date(payload.task_date)}"
            if duplicate_count: confirmation+=f"\n🔄 {duplicate_count} ta takroriy vazifa qayta qo‘shilmadi."
            if payload.task_date != today and user.get('morning_time') is not None:
                confirmation+=f"\n\n🌅 Ertaga soat {str(user['morning_time'])[:5]} da rejangizni eslataman."
            keyboard = [[{"text": "🏠 Bugun", "web_app": {"url": MINIAPP_URL}}]]
            if offer_morning:
                keyboard.append([{"text": "🌅 Ertalabki eslatma", "callback_data": "morning_setup"}])
            try:
                telegram_send_message_with_keyboard(chat_id, confirmation, {"inline_keyboard": keyboard})
                result['chat_notified'] = True
            except Exception as error:
                # Task is committed. Never report a failed save or resend on request replay.
                print('Tomorrow planning notification failed:',type(error).__name__)
            if payload.task_date == today:
                try:
                    refresh_live_checklist(chat_id, user)
                except Exception as error:
                    print('Created task checklist sync failed:', type(error).__name__)
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE public.qadam_miniapp_task_requests SET result=%s::jsonb WHERE bot_id=%s AND user_id=%s AND request_id=%s", (json.dumps(result),bot_id(),str(user['id']),request_id))
                conn.commit()
        if result.get("added", 0):
            from journey import record
            record(user["id"], "task_saved", "miniapp", request_id)
        return result
