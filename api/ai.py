"""Groq chat wrapper.

Every Groq failure is turned into AIUnavailable so callers can tell the user
to contact the admin, and the admin is warned once per hour per reason.
"""
import json
import os
import re
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = os.getenv("GROQ_CHAT_MODEL", "openai/gpt-oss-20b")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "Shakhbozbek_Abdulamitov").lstrip("@")
ADMIN_URL = "https://t.me/" + ADMIN_USERNAME
ZONE = ZoneInfo("Asia/Tashkent")

REASON_TEXT = {
    "rate_limit": "AI yordamchining limiti tugagan",
    "unavailable": "AI yordamchi vaqtincha ishlamayapti",
    "no_key": "AI yordamchi sozlanmagan yoki kaliti yaroqsiz",
}

_notify = None
_claim = None
_cooldown_until = 0.0
_last_alert = {}
_lock = threading.Lock()


class AIUnavailable(Exception):
    def __init__(self, reason="unavailable", retry_after=None):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


def configure(notify, claim):
    """notify(text) messages the admin; claim(kind, key) -> True once per key."""
    global _notify, _claim
    _notify, _claim = notify, claim


def api_key():
    return os.getenv("GROQ_API_KEY")


def available():
    return bool(api_key()) and time.time() >= _cooldown_until


def user_text(error=None):
    reason = getattr(error, "reason", "unavailable")
    base = "⚠️ " + REASON_TEXT.get(reason, REASON_TEXT["unavailable"]) + "."
    return base + " Iltimos, adminga murojaat qiling: @" + ADMIN_USERNAME


def error_payload(error):
    """JSON body for Mini App endpoints."""
    return {
        "ai_unavailable": True,
        "reason": error.reason,
        "detail": user_text(error),
        "admin_url": ADMIN_URL,
        "admin_username": ADMIN_USERNAME,
    }


def admin_button():
    return {"inline_keyboard": [[{"text": "👤 Adminga yozish", "url": ADMIN_URL}]]}


def _report(reason, where, retry_after):
    now = time.time()
    with _lock:
        if now - _last_alert.get(reason, 0) < 600:
            return
        _last_alert[reason] = now
    if not _notify:
        return
    try:
        hour = datetime.now(ZONE).strftime("%Y-%m-%d-%H")
        if _claim and not _claim("groq_" + reason, hour):
            return
        text = "⚠️ Groq: " + REASON_TEXT.get(reason, reason) + "\nJoy: " + str(where)
        if retry_after:
            text += "\nQayta urinish: ~" + str(int(retry_after)) + " soniyadan keyin"
        _notify(text + "\nFoydalanuvchilarga «adminga murojaat qiling» xabari ko‘rsatildi.")
    except Exception as error:
        print("AI admin alert failed:", type(error).__name__)


def fail(reason, where, retry_after=None):
    global _cooldown_until
    if reason == "rate_limit" and retry_after:
        _cooldown_until = max(_cooldown_until, time.time() + min(float(retry_after), 300))
    _report(reason, where, retry_after)
    raise AIUnavailable(reason, retry_after)


def check_response(resp, where):
    """Raise AIUnavailable for limit/auth/server failures; ignore 2xx."""
    status = resp.status_code
    if status < 400:
        return
    try:
        retry_after = float(resp.headers.get("retry-after"))
    except (TypeError, ValueError):
        retry_after = None
    if status == 429:
        fail("rate_limit", where, retry_after)
    if status in (401, 403):
        fail("no_key", where)
    if status >= 500 or status in (404, 408):
        fail("unavailable", where)


def chat(messages, where="chat", max_tokens=2500, temperature=0.4, timeout=40):
    """Return the assistant text or raise AIUnavailable."""
    if not api_key():
        fail("no_key", where)
    if time.time() < _cooldown_until:
        raise AIUnavailable("rate_limit", _cooldown_until - time.time())
    body = {"model": MODEL, "messages": messages, "max_tokens": max_tokens,
            "temperature": temperature, "reasoning_effort": "low"}
    headers = {"Authorization": "Bearer " + api_key(), "Content-Type": "application/json"}
    try:
        resp = requests.post(GROQ_URL, headers=headers, json=body, timeout=timeout)
        if resp.status_code == 400 and "reasoning_effort" in resp.text:
            body.pop("reasoning_effort")
            resp = requests.post(GROQ_URL, headers=headers, json=body, timeout=timeout)
    except requests.RequestException:
        fail("unavailable", where)
    check_response(resp, where)
    if resp.status_code != 200:
        fail("unavailable", where)
    try:
        return (resp.json()["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, ValueError, TypeError):
        fail("unavailable", where)


def extract_json(text):
    """First JSON object in a model reply, or None."""
    if not text:
        return None
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start:i + 1])
                        if isinstance(value, dict):
                            return value
                    except ValueError:
                        pass
                    break
        start = text.find("{", start + 1)
    return None
