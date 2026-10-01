from __future__ import annotations

import pytest

from app.services.normalization import (
    canonicalize_url,
    normalize_company,
    normalize_location,
    normalize_title,
)


def test_normalizes_polish_company_and_location() -> None:
    assert normalize_company("Example Polska Sp. z o.o.") == "example polska"
    assert normalize_location("Cracow, Poland") == "krakow"


def test_normalizes_internship_title_variants() -> None:
    assert normalize_title("Finance Analyst Internship") == "analyst finance intern"


def test_canonical_url_strips_tracking_and_fragment() -> None:
    assert (
        canonicalize_url("HTTPS://Example.COM/job/42/?utm_source=x#apply")
        == "https://example.com/job/42"
    )


def test_rejects_unsafe_url() -> None:
    with pytest.raises(ValueError):
        canonicalize_url("javascript:alert(1)")
