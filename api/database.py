import os
from contextlib import contextmanager

from psycopg2 import pool as pg_pool


# =========================================================
# CONNECTION POOL
#
# Bitta TCP/TLS/auth handshake narxi ~50-300ms bo'lishi mumkin.
# Pool bo'lmasa, har bir get_connection() shu narxni to'laydi.
# Pool bilan esa connectionlar qayta ishlatiladi.
# =========================================================

MIN_CONN = int(os.getenv("DB_POOL_MIN", "2"))
MAX_CONN = int(os.getenv("DB_POOL_MAX", "10"))

_pool = None


def _get_pool():
    """
    Pool birinchi so'rovda (lazy) yaratiladi, import vaqtida
    emas. Shu bilan DB vaqtincha mavjud bo'lmasa ham modul
    import bosqichida (masalan deploy/health-check paytida)
    ilova qulab tushmaydi.
    """

    global _pool

    if _pool is None:

        _pool = pg_pool.ThreadedConnectionPool(
            MIN_CONN,
            MAX_CONN,
            os.environ["DATABASE_URL"]
        )

    return _pool


@contextmanager
def get_connection():
    """
    Poolli connection.

    Eski kod bilan bir xil ishlaydi:

        with get_connection() as conn:
            with conn.cursor() as cur:
                ...

    Farqi: yopilganda connection asl socket yopilmaydi,
    poolga qaytariladi va keyingi so'rov uni qayta ishlatadi.
    """

    pool = _get_pool()

    conn = pool.getconn()

    try:

        yield conn

    finally:

        pool.putconn(conn)


def close_pool():

    global _pool

    if _pool is not None:

        _pool.closeall()

        _pool = None
