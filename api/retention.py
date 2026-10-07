"""Task-based daily messages and persistent, bot-scoped notification claims."""
import html
from datetime import timedelta


def initialize(conn):
    with conn.cursor() as cur:
        cur.execute('SELECT pg_advisory_xact_lock(716240924)')
        cur.execute('''CREATE TABLE IF NOT EXISTS public.qadam_notification_claims (
            bot_id TEXT NOT NULL, user_id TEXT NOT NULL, kind TEXT NOT NULL,
            event_key TEXT NOT NULL, claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (bot_id, user_id, kind, event_key)
        )''')
        cur.execute("""CREATE TABLE IF NOT EXISTS public.qadam_miniapp_task_requests (
            bot_id TEXT NOT NULL, user_id TEXT NOT NULL, request_id TEXT NOT NULL,
            input_hash TEXT NOT NULL, result JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY(bot_id,user_id,request_id)
        )""")
    conn.commit()


def claim(connection, bot, user_id, kind, key):
    # Claim before Telegram: retries cannot create duplicate notifications.
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute('''INSERT INTO public.qadam_notification_claims(bot_id,user_id,kind,event_key)
                VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING user_id''',
                (bot, str(user_id), kind, str(key)))
            acquired = cur.fetchone() is not None
        conn.commit()
    return acquired


def task_counts(tasks):
    return {status: sum(task['status'] == status for task in tasks)
            for status in ('completed', 'failed', 'pending')}


def morning_text(name, tasks):
    greeting = f'<b>🌅 Assalomu alaykum, {html.escape(str(name or "Do‘st"))}!</b>'
    if not tasks:
        return greeting + '\n\nBugun qilmoqchi bo‘lgan bitta muhim ishingizni yozing yoki 🎙️ ovozli xabar yuboring.'
    counts = task_counts(tasks)
    if counts['pending']:
        return greeting + '\n\nBugunga rejalashtirgan vazifalaringiz. Bajarganlaringizni quyida belgilang.'
    return greeting + f'\n\nBugungi {len(tasks)} ta vazifangizning natijasi belgilangan. ✅ Bajarildi: {counts["completed"]} · ❌ Bajarilmadi: {counts["failed"]}.'


def evening_text(tasks):
    counts = task_counts(tasks)
    text = (f'<b>🌙 Bugungi natijangiz</b>\n\n'
            f'✅ {len(tasks)} ta vazifadan {counts["completed"]} tasi bajarildi.\n'
            f'❌ Bajarilmadi: {counts["failed"]} · ⏳ Belgilanmagan: {counts["pending"]}.')
    if counts['pending']:
        text += '\n\nAvval bajarganlaringizni ✅, bajara olmaganlaringizni ❌ bilan belgilang.'
    else:
        text += '\n\nBugungi natijangiz saqlandi. 👣'
    return text + '\n\nErtaga eng muhim qaysi ishni qilmoqchisiz? Rejangizni shu chatga yozing.'


def weekly_advice(tasks, reasons):
    """Use only recorded reasons; never infer a cause from unfinished tasks."""
    recorded = [task for task in tasks if task['status'] == 'failed'
                and task.get('fail_reason') in reasons]
    if not recorded:
        if any(task['status'] == 'failed' for task in tasks):
            return 'Sabablar yetarli yozilmagan. Keyingi safar qisqacha sabab belgilash tahlilga yordam beradi.'
        return 'Keyingi hafta uchun bitta muhim maqsadni tanlang va uni kichik vazifalarga bo‘ling.'
    counts = {}
    for task in recorded:
        code = task['fail_reason']
        counts[code] = counts.get(code, 0) + 1
    highest = max(counts.values())
    leaders = [code for code, count in counts.items() if count == highest]
    labels = ', '.join(reasons[code]['label'] for code in leaders)
    text = f'Eng ko‘p qayd etilgan sabab: {labels} ({highest} martadan).'
    if len(leaders) != 1:
        return text + ' Keyingi rejani shu sabablarni hisobga olib tuzing.'
    advice = {
        'no_time': 'Kunlik rejani kichraytirib, eng muhim ishga vaqt ajratib ko‘ring.',
        'forgot': 'Muhim vazifangizga 🔔 orqali eslatma qo‘yib ko‘ring.',
        'too_hard': 'Qiyin vazifani kichik, bajarish oson qadamlarga bo‘lib ko‘ring.',
        'priority': 'Kun boshida eng muhim bitta vazifani ajratib ko‘ring.',
        'bad_mood': 'Keyingi rejada kuchingizga mos kichik qadamni tanlang.',
    }
    return text + ' ' + advice.get(leaders[0], 'Keyingi rejani yozishda qayd etgan sabablaringizni hisobga oling.')
