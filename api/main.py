from fastapi import FastAPI
from fastapi.responses import FileResponse
import os
from contextlib import asynccontextmanager

from scheduler import start_scheduler
from bot_identity import initialize_identity
from database import get_connection

from routes import router
from migration import router as migration_router
from admin_routes import router as admin_router


@asynccontextmanager
async def lifespan(app):
    initialize_identity()
    stop, thread = start_scheduler()
    yield
    stop.set()


app = FastAPI(
    title="Qadam API",
    version="1.0.0",
    lifespan=lifespan
)


MINIAPP_HTML_PATH = os.path.join(
    os.path.dirname(__file__),
    "miniapp",
    "index.html"
)

ADMIN_MINIAPP_HTML_PATH = os.path.join(
    os.path.dirname(__file__),
    "admin_miniapp",
    "index.html"
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


@app.get("/miniapp")
def miniapp_page():
    return FileResponse(
        MINIAPP_HTML_PATH,
        media_type="text/html"
    )


@app.get("/admin-miniapp")
def admin_miniapp_page():
    return FileResponse(
        ADMIN_MINIAPP_HTML_PATH,
        media_type="text/html"
    )


app.include_router(router)
app.include_router(migration_router)
app.include_router(admin_router)
