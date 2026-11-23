"""Exercise registered Mini App handlers against the disposable CI database."""

import os
import sys
from pathlib import Path
from uuid import uuid4

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))


def main():
    if os.environ.get("TELEGRAM_TOKEN"):
        raise RuntimeError("This check requires Telegram to be disabled")

    from miniapp_reminders import MiniappTaskReminderRequest
    from miniapp_task_creation import MiniappTomorrowTaskRequest
    from miniapp_task_status import MiniappTaskStatusRequest
    from retention import initialize
    from routes import get_today, router
    from unittest.mock import patch

    action = {
        (method, route.path): route.endpoint
        for route in router.routes
        for method in getattr(route, "methods", ())
    }
    with psycopg2.connect(os.environ["DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                ALTER TABLE public.users ADD COLUMN telegram_chat_id bigint UNIQUE,
                    ADD COLUMN state text, ADD COLUMN morning_time time,
                    ADD COLUMN last_active_date date,
                    ADD COLUMN last_completion_notified_date date;
                CREATE TABLE public.tasks (
                    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    user_id bigint NOT NULL REFERENCES public.users(id),
                    task_text text NOT NULL, task_date date NOT NULL,
                    status text NOT NULL, reminder_time time, reminder_sent_at timestamp,
                    fail_reason text, fail_reason_text text,
                    reason_message_id bigint, reason_text_message_id bigint
                );
                INSERT INTO public.users (id, telegram_chat_id, state, morning_time)
                VALUES (1, 101, 'active', '08:00');
            """)
        conn.commit()
        initialize(conn)
        with patch("requests.sessions.Session.request", side_effect=AssertionError("External HTTP request")) as external:
            payload = MiniappTomorrowTaskRequest(
                task_text="Read a book", task_date=get_today(), request_id=str(uuid4())
            )
            create = action["POST", "/api/miniapp/tasks"]
            result = create(payload, 101)
            assert result["added"] == 1, result
            assert create(payload, 101) == result
            task_id = result["task_id"]
            reminder = action["PUT", "/api/miniapp/tasks/{task_id}/reminder"]
            assert reminder(task_id, MiniappTaskReminderRequest(reminder_time="09:15"), 101)["reminder_time"] == "09:15"
            status = action["POST", "/api/miniapp/tasks/{task_id}/status"]
            assert status(task_id, MiniappTaskStatusRequest(status="completed"), 101)["task"]["status"] == "completed"
            external.assert_not_called()
        with conn.cursor() as cur:
            cur.execute("SELECT task_text, reminder_time::text, status FROM public.tasks WHERE user_id = 1")
            assert cur.fetchall() == [("Read a book", "09:15:00", "completed")]
    print("Task creation, replay, reminder and completion persisted in PostgreSQL")


if __name__ == "__main__":
    main()
