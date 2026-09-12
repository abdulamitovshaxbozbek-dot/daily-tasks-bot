import os
from contextlib import contextmanager
from psycopg2.pool import ThreadedConnectionPool
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]

pool = ThreadedConnectionPool(
    minconn=1,
    maxconn=30,
    dsn=DATABASE_URL
)


def _is_connection_alive(conn) -> bool:
    """
    Pool'dan olingan connection hali ishlayaptimi tekshiradi.
    Yopilgan/uzilgan bo'lsa False qaytaradi.
    """
    if conn.closed != 0:
        return False

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return True
    except Exception:
        return False


@contextmanager
def get_connection():
    conn = pool.getconn()

    # MUHIM: pool'dan kelgan connection eskirgan/uzilgan bo'lishi
    # mumkin (masalan uzoq vaqt idle turgach SSL yopilib qolsa).
    # Shunday connection'ni foydalanuvchiga bermasdan, uni pool'dan
    # butunlay chiqarib tashlab, yangisini olamiz.
    if not _is_connection_alive(conn):
        try:
            pool.putconn(conn, close=True)
        except Exception:
            pass

        conn = pool.getconn()

    try:
        yield conn

    except psycopg2.OperationalError:
        # Connection so'rov davomida uzilib qolgan bo'lishi mumkin.
        # Uni pool'ga qaytarmasdan yopamiz, keyingi so'rov yangi
        # connection oladi.
        try:
            pool.putconn(conn, close=True)
        except Exception:
            pass

        raise

    else:
        pool.putconn(conn)
