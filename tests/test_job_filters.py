from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_session
from app.main import app
from app.models import Application, Job
from app.routes import actions


def _job(title: str, age_days: int) -> Job:
    slug = title.casefold().replace(" ", "-")
    return Job(
        source="Test",
        source_job_id=slug,
        canonical_url=f"https://example.com/{slug}",
        original_url=f"https://example.com/{slug}",
        title=title,
        company="Example",
        location="Kraków",
        description="Finance internship",
        job_hash=slug,
        date_discovered=datetime.now() - timedelta(days=age_days),
    )


def test_new_jobs_hide_applied_and_support_day_window(
    session: Session, monkeypatch
) -> None:  # noqa: ANN001
    recent = _job("Recent Open Role", 1)
    applied = _job("Recent Applied Role", 1)
    ignore_from_new = _job("Ignore From New", 1)
    old = _job("Older Open Role", 20)
    session.add_all([recent, applied, ignore_from_new, old])
    session.flush()
    session.add(Application(job=applied, status="Applied"))
    session.commit()

    def override_session():  # noqa: ANN202
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        client = TestClient(app)
        default_new = client.get("/jobs?view=new")
        assert default_new.status_code == 200
        assert "Recent Open Role" in default_new.text
        assert "Ignore From New" in default_new.text
        assert "Recent Applied Role" not in default_new.text
        assert "Older Open Role" not in default_new.text

        show_all = client.get("/jobs?view=new&hide_applied=0&days=0")
        assert "Recent Applied Role" in show_all.text
        assert "Older Open Role" in show_all.text

        last_three_days = client.get("/jobs?days=3&hide_applied=0")
        assert "Recent Open Role" in last_three_days.text
        assert "Recent Applied Role" in last_three_days.text
        assert "Older Open Role" not in last_three_days.text

        ignored = client.post(
            f"/actions/jobs/{ignore_from_new.id}/ignore",
            data={"return_to": "/jobs?view=new&days=3"},
            follow_redirects=False,
        )
        assert ignored.status_code == 303
        assert ignored.headers["location"].startswith("/jobs?view=new&days=3&message=")
        assert "Ignore From New" not in client.get("/jobs?view=new&days=3").text

        calls = []

        def fake_excel_sync(path, job, applied_on):  # noqa: ANN001, ANN202
            calls.append((path, job.id, applied_on))
            return SimpleNamespace(created=False, row_number=4)

        monkeypatch.setattr(actions, "mark_applied_in_excel", fake_excel_sync)
        marked = client.post(
            f"/actions/jobs/{recent.id}/mark-applied",
            data={"return_to": "/jobs?view=new"},
            follow_redirects=False,
        )
        assert marked.status_code == 303
        session.expire_all()
        assert recent.application is not None
        assert recent.application.status == "Applied"
        assert recent.application.date_applied is not None
        assert len(calls) == 1
    finally:
        app.dependency_overrides.pop(get_session, None)
