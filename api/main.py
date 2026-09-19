from fastapi import FastAPI
from fastapi.responses import FileResponse
import os

from database import get_connection

from routes import router

app = FastAPI(

    title="Vazifalarim API",

    version="1.0.0"

)

MINIAPP_HTML_PATH = os.path.join(
    os.path.dirname(__file__),
    "miniapp",
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

app.include_router(router)
