from contextlib import asynccontextmanager

from fastapi import FastAPI

from database import get_connection, close_pool
from routes import router, close_http_client


@asynccontextmanager
async def lifespan(app: FastAPI):

    # STARTUP - hech narsa qilish shart emas,
    # pool va http client "lazy" - birinchi
    # ishlatilganda o'zi yaratiladi.

    yield

    # SHUTDOWN - resurslarni ozodlik bilan yopamiz

    await close_http_client()

    close_pool()


app = FastAPI(
    title="Vazifalarim API",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/health")
def health():

    return {
        "status": "ok"
    }


@app.get("/health/db")
def health_db():

    with get_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("SELECT 1")

            result = cur.fetchone()

    return {
        "status": "ok",
        "database": result[0] == 1
    }


app.include_router(router)
