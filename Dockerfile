# Official Playwright Python image ships Chromium + every system dep it needs,
# so screenshots work out of the box without `playwright install-deps`.
# Pinned to match the Playwright pip version in backend/requirements.txt.
# When bumping one, bump the other to the same vX.Y.Z — otherwise the bundled
# Chromium under /ms-playwright won't match what the lib expects.
FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

WORKDIR /app

# Install Python deps first for better layer caching.
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# Application code.
COPY backend /app/backend
COPY static /app/static

ENV APP_PORT=8888 \
    CACHE_DIR=/data \
    PYTHONUNBUFFERED=1

EXPOSE 8888
VOLUME ["/data"]

# Shell form so ${APP_PORT} expands at runtime.
CMD uvicorn backend.app:app --host 0.0.0.0 --port ${APP_PORT}
