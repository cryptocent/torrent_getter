FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=5000 \
    APP_DEBUG=false

WORKDIR /app

RUN useradd --create-home --shell /usr/sbin/nologin appuser

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/instance/posters \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 5000
VOLUME ["/app/instance"]

CMD ["python", "app.py"]