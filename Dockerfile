FROM python:3.12-slim
ARG VCS_REF=local
LABEL org.opencontainers.image.revision=$VCS_REF

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_BUILD_REF=$VCS_REF

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 workfinder \
    && mkdir /app/data \
    && chown workfinder:workfinder /app/data

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .

USER workfinder
CMD ["python", "-m", "app.main"]
