FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

RUN useradd -u 65532 -m -s /usr/sbin/nologin appuser

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN mkdir -p /data && chown -R 65532:65532 /data /srv

USER 65532
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python","-c","import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3); sys.exit(0)"]

CMD ["python","-m","uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]
