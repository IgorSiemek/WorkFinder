from __future__ import annotations

from sqlalchemy import select

from app.models import Application, ApplicationActivity, Job
from app.services.seed import seed_defaults, seed_demo


def test_demo_seed_is_idempotent(session) -> None:  # noqa: ANN001
    seed_defaults(session)
    first = seed_demo(session)
    second = seed_demo(session)
    assert first == 24
    assert second == 0
    assert len(session.scalars(select(Job)).all()) == 24


def test_application_history_is_append_only(session) -> None:  # noqa: ANN001
    seed_demo(session)
    application = session.scalar(select(Application).limit(1))
    session.add(
        ApplicationActivity(application=application, event_type="status", description="Applied")
    )
    session.add(
        ApplicationActivity(application=application, event_type="status", description="Interview")
    )
    session.commit()
    assert [item.description for item in application.activities][-2:] == ["Applied", "Interview"]
