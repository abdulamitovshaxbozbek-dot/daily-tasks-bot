FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY api/requirements.txt /app/api/requirements.txt
RUN pip install --no-cache-dir -r api/requirements.txt
COPY api /app/api
CMD ["sh", "-c", "cd api && exec python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 4"]
