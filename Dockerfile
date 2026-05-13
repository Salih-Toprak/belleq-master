FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV APP_PORT=9000
RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
RUN mkdir -p /app/data
EXPOSE 9000
CMD ["sh", "-c", "python -m uvicorn app.main:app --host 0.0.0.0 --port ${APP_PORT:-9000}"]
