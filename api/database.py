import os
from contextlib import contextmanager
from psycopg2.pool import ThreadedConnectionPool

DATABASE_URL = os.environ["DATABASE_URL"]

pool = ThreadedConnectionPool(
    minconn=1,
    maxconn=50,
    dsn=DATABASE_URL
)


@contextmanager
def get_connection():
    conn = pool.getconn()

    try:
        yield conn
    finally:
        pool.putconn(conn)
