FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    EXHIBITION_DB=/data/exhibition.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite WAL file and database live on a mounted volume.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# Two workers share the same SQLite file safely: WAL + BEGIN IMMEDIATE
# serializes writers; busy_timeout makes lock waits non-fatal.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
