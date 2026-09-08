from fastapi import FastAPI

from database import get_connection

from routes import router

app = FastAPI(

    title="Vazifalarim API",

    version="1.0.0"

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



