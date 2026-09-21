"""Old bot replies only after the new bot has been verified and enabled."""
import hmac
import os
import requests
from fastapi import APIRouter, Header, HTTPException

router = APIRouter(prefix='/api')


@router.post('/telegram/legacy')
def legacy_webhook(update: dict, x_telegram_bot_api_secret_token: str = Header(default='')):
    secret = os.getenv('LEGACY_WEBHOOK_SECRET', '')
    if not secret or not hmac.compare_digest(secret, x_telegram_bot_api_secret_token):
        raise HTTPException(401, 'Invalid webhook secret')
    if os.getenv('MIGRATION_NOTICE_ENABLED', 'false').lower() != 'true':
        raise HTTPException(503, 'Migration is not ready')
    token = os.getenv('LEGACY_TELEGRAM_TOKEN')
    if not token:
        raise HTTPException(503, 'Legacy bot is not configured')
    message = update.get('message') or (update.get('callback_query') or {}).get('message') or {}
    chat = message.get('chat') or {}
    if chat.get('type') != 'private' or not chat.get('id'):
        return {'ok': True}
    try:
        response = requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={
                'chat_id': chat['id'],
                'text': '🚀 Biz yangi botga ko‘chdik!\n\nVazifalar va eslatmalardan foydalanishni davom ettirish uchun @bir_qadambot’ga o‘ting va Start tugmasini bosing.',
                'reply_markup': {'inline_keyboard': [[{
                    'text': 'Yangi botga o‘tish', 'url': 'https://t.me/bir_qadambot?start=migration'
                }]]},
            }, timeout=15,
        )
        if response.status_code == 403:
            return {'ok': True}
        if not response.ok or not response.json().get('ok'):
            raise HTTPException(502, 'Legacy reply failed')
    except (requests.RequestException, ValueError):
        raise HTTPException(502, 'Legacy reply failed') from None
    return {'ok': True}
