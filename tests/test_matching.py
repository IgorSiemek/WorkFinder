from __future__ import annotations

from app.models import CandidateProfile, Job
from app.services.matching import classify_job, extract_skills, score_job


def make_job(title: str, text: str, location: str = "Kraków · Hybrid") -> Job:
    return Job(
        source="Test",
        source_job_id="1",
        canonical_url="https://example.com/1",
        original_url="https://example.com/1",
        title=title,
        company="Example",
        location=location,
        description=text,
        job_hash="x",
    )


def test_extracts_skills_without_substring_guessing() -> None:
    skills = extract_skills("Use Excel, SQL and Power BI for financial reporting in English.")
    assert {"Excel", "SQL", "Power BI", "Reporting", "English"} <= set(skills)


def test_scores_relevant_intern_above_senior_manager() -> None:
    profile = CandidateProfile(skills=["Excel", "English"], technologies=["SQL"])
    intern = make_job(
        "Financial Analyst Intern",
        "Excel SQL financial analysis reporting KPI budgeting internship",
    )
    manager = make_job("Senior Sales Manager", "6+ years sales representative and customer support")
    assert score_job(intern, profile).score >= 80
    assert score_job(manager, profile).score < 30


def test_classifies_multiple_categories() -> None:
    categories = classify_job("FP&A role with financial planning and data analysis in a bank")
    assert {"FP&A", "Data Analytics", "Banking"} <= set(categories)
