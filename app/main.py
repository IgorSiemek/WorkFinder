from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app.config import PROJECT_ROOT, settings
from app.database import SessionLocal, init_database
from app.models import Job
from app.routes import actions, pages
from app.services.backups import automatic_backup
from app.services.seed import seed_defaults, seed_demo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    automatic_backup()
    init_database()
    with SessionLocal() as session:
        seed_defaults(session)
        if (session.scalar(select(func.count(Job.id))) or 0) == 0:
            seed_demo(session)
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "app" / "static"), name="static")
templates = Jinja2Templates(directory=PROJECT_ROOT / "app" / "templates")
app.state.templates = templates
app.include_router(pages.router)
app.include_router(actions.router)


@app.middleware("http")
async def local_security_headers(request: Request, call_next):  # noqa: ANN001, ANN202
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response
