"""Mini App route for marking tasks completed or failed."""

from typing import Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field


class MiniappTaskStatusRequest(BaseModel):
    status: str
    reason_code: Optional[str] = None
    reason_text: Optional[str] = Field(default=None, max_length=1000)


def install(
    router, *, get_miniapp_chat_id, get_user_by_chat_id, transition_task,
    _remove_task_prompt, sync_task_result, _tasks_to_json,
):
    """Register the task status route using shared bot services."""
    @router.post('/miniapp/tasks/{task_id}/status')
    def miniapp_task_status(task_id: str, payload: MiniappTaskStatusRequest,
                            chat_id: int = Depends(get_miniapp_chat_id)):
        user = get_user_by_chat_id(chat_id)
        if not user or user['state'] == 'blocked':
            raise HTTPException(status_code=403, detail='Avval botda /start bosing.')
        if payload.status not in ('completed', 'failed'):
            raise HTTPException(status_code=422, detail='Status noto‘g‘ri.')
        if payload.status == 'failed' and payload.reason_code is None:
            raise HTTPException(status_code=422, detail='Bajarilmaganlik sababini tanlang.')
        result = transition_task(user, task_id, payload.status, payload.reason_code, payload.reason_text)
        if result['changed']:
            for field in ('reason_message_id', 'reason_text_message_id'):
                try:
                    _remove_task_prompt(chat_id, result['original'].get(field))
                except Exception as error:
                    print('Reason prompt cleanup failed:', type(error).__name__)
        synced = sync_task_result(chat_id, user, result['task'])
        response = {'ok': not result['conflict'], 'changed': result['changed'],
                    'checklist_updated': synced, 'task': _tasks_to_json([result['task']])[0]}
        if result['conflict']:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=409, content={**response, 'detail': 'Vazifa boshqa interfeysda belgilangan. Joriy holat ko‘rsatildi.'})
        return response
