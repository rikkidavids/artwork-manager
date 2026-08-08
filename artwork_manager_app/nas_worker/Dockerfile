FROM python:3.12-slim

LABEL org.opencontainers.image.title="Artwork Manager NAS Worker" \
      org.opencontainers.image.version="5.04" \
      org.opencontainers.image.description="NAS-local artwork embed, convert, and deep-check worker for Artwork Manager"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AMW_HOST=0.0.0.0 \
    AMW_PORT=8765 \
    AMW_MUSIC_ROOT=/music \
    AMW_BACKUP_DIR=/backups

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY server.py /app/server.py

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/', timeout=4).status == 200 else 1)"
CMD ["python", "/app/server.py"]
