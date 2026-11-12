FROM python:3.12-slim
ARG VCS_REF=local
LABEL org.opencontainers.image.revision=$VCS_REF

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_BUILD_REF=$VCS_REF
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /opt/playwright \
    && useradd --create-home --uid 10001 workfinder \
    && mkdir /app/data \
    && chown workfinder:workfinder /app/data

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .

USER workfinder
CMD ["python", "-m", "app.main"]
