"""Prepare the habits schema once before starting the HTTP workers."""

import os
import sys

from database import get_connection


def main():
    os.environ.pop("QADAM_HABITS_SCHEMA_PREPARED", None)
    from habits import initialize

    with get_connection() as conn:
        initialize(conn)

    env = dict(os.environ, QADAM_HABITS_SCHEMA_PREPARED="1")
    os.execve(
        sys.executable,
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "0.0.0.0",
         "--port", env.get("PORT", "8000"), "--workers", "4"],
        env,
    )


if __name__ == "__main__":
    main()
