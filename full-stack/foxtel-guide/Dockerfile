FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BRIDGE_PORT=8765

WORKDIR /app
COPY bridge.py /app/bridge.py
USER 10001:10001
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "from urllib.request import urlopen; assert urlopen('http://127.0.0.1:8765/healthz', timeout=3).status == 200"

CMD ["python", "/app/bridge.py"]
