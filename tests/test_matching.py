from __future__ import annotations

import pytest

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


@pytest.mark.parametrize(
    ("title", "category"),
    [
        ("Risk Control Internship in CRO IB", "Risk"),
        ("Risk Analyst Intern", "Risk"),
        ("Staż - Analityk ryzyka", "Risk"),
        ("Staż - Analityk finansowy", "Financial Analysis"),
        ("Staż - Analityk danych", "Data Analytics"),
        ("Business Intelligence Intern", "Data Analytics"),
        ("BI Analyst Intern", "Data Analytics"),
        ("BI Intern", "Data Analytics"),
        ("Data Analytics Intern", "Data Analytics"),
    ],
)
def test_analytical_internship_titles_reach_notification_threshold(title, category):
    result = score_job(make_job(title, ""))
    assert category in result.categories
    assert result.score >= 60


def test_billing_is_not_a_business_intelligence_keyword():
    assert "Data Analytics" not in classify_job("Billing Intern")


def test_excellent_does_not_count_as_excel_skill_or_score():
    description = "Excellent communication and excellent organizational skills."
    result = score_job(make_job("Student Internship - Talent Acquisition", description))
    assert "Excel" not in extract_skills(description)
    assert not any(reason["label"] == "Excel" for reason in result.explanation)
    assert result.score == 48


@pytest.mark.parametrize("product", ["Excel", "MS Excel", "Microsoft Excel", "Excel/VBA"])
def test_excel_product_names_still_match(product):
    result = score_job(make_job("Student Internship", f"Knowledge of {product}."))
    assert "Excel" in result.skills
    assert {"label": "Excel", "points": 7} in result.explanation


@pytest.mark.parametrize(
    "description",
    [
        "Finance analysis and Excel. Report to the Finance Manager.",
        "Finance analysis and Excel. Learn from senior analysts.",
        "Finance analysis and Excel. Work with the Director of Finance.",
    ],
)
def test_internship_is_not_penalized_for_mentors_in_its_description(description):
    result = score_job(make_job("Trainee", description))
    assert result.score >= 60
    assert not any(reason["label"] == "Senior role" for reason in result.explanation)


@pytest.mark.parametrize("seniority", ["Senior", "Lead", "Manager"])
def test_senior_metadata_still_reduces_the_score(seniority):
    job = make_job("Finance Analyst", "Financial reporting and Excel.")
    job.seniority = seniority
    assert {"label": "Senior role", "points": -28} in score_job(job).explanation
