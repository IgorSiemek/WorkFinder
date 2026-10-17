from __future__ import annotations

from dataclasses import dataclass

from app.models import CandidateProfile, Job
from app.scrapers.common import is_internship_role

SKILL_ALIASES: dict[str, tuple[str, ...]] = {
    "Excel": ("excel", "spreadsheets"),
    "SQL": ("sql", "postgresql", "mysql"),
    "Python": ("python",),
    "Power BI": ("power bi", "powerbi"),
    "Data analysis": ("data analysis", "data analytics", "analiza danych"),
    "Statistics": ("statistics", "statystyka"),
    "Financial analysis": ("financial analysis", "analiza finansowa"),
    "Accounting": ("accounting", "księgowość", "financial statements"),
    "FP&A": ("fp&a", "financial planning"),
    "Budgeting": ("budgeting", "budżetowanie"),
    "Forecasting": ("forecasting", "prognozowanie"),
    "KPI": ("kpi", "key performance indicator"),
    "Variance analysis": ("variance analysis", "analiza odchyleń"),
    "Reporting": ("reporting", "raportowanie"),
    "English": ("english", "angielski"),
    "SAP": ("sap",),
    "Presentation": ("presentation", "powerpoint"),
}

CATEGORY_TERMS: dict[str, tuple[str, ...]] = {
    "Financial Analysis": ("financial analyst", "financial analysis", "analiza finansowa"),
    "FP&A": ("fp&a", "financial planning", "forecasting"),
    "Controlling": ("controlling", "controller", "variance analysis"),
    "Data Analytics": ("data analyst", "data analysis", "sql", "power bi"),
    "Business Analysis": ("business analyst", "business analysis"),
    "Risk": ("risk", "credit analyst"),
    "Banking": ("bank", "banking", "treasury"),
    "Investment Analysis": ("investment", "portfolio", "equity research"),
    "Research/Economics": ("research", "economic", "economics"),
    "Accounting": ("accounting", "ledger", "financial statements"),
    "Consulting": ("consulting", "consultant"),
    "Operations": ("operations", "process"),
}


@dataclass(slots=True)
class MatchResult:
    score: int
    explanation: list[dict[str, object]]
    skills: list[str]
    missing_skills: list[str]
    categories: list[str]


def extract_skills(text: str) -> list[str]:
    content = text.casefold()
    return [
        name
        for name, aliases in SKILL_ALIASES.items()
        if any(alias in content for alias in aliases)
    ]


def classify_job(text: str) -> list[str]:
    content = text.casefold()
    found = [
        name for name, terms in CATEGORY_TERMS.items() if any(term in content for term in terms)
    ]
    return found or ["Other"]


def score_job(job: Job, profile: CandidateProfile | None = None) -> MatchResult:
    text = " ".join(
        value or ""
        for value in [
            job.title,
            job.description,
            job.requirements,
            job.responsibilities,
            job.employment_type,
        ]
    ).casefold()
    explanation: list[dict[str, object]] = []
    score = 20

    def add(label: str, points: int) -> None:
        nonlocal score
        score += points
        explanation.append({"label": label, "points": points})

    if is_internship_role(job.title, job.employment_type, job.seniority):
        add("Internship or trainee level", 20)
    positive = [
        (("junior", "entry level", "graduate", "młodszy"), "Entry-level seniority", 14),
        (("financial analysis", "finance", "fp&a", "controlling"), "Finance focus", 12),
        (("analyst", "analysis", "analityk", "analitycz"), "Analytical work", 12),
        (("reporting", "kpi", "forecast", "budget", "variance"), "Reporting and planning", 8),
        (("sql",), "SQL", 7),
        (("excel",), "Excel", 7),
        (("python", "power bi"), "Analytics technology", 5),
    ]
    for terms, label, points in positive:
        if any(term in text for term in terms):
            add(label, points)
    negative = [
        (("senior", "lead ", "manager", "director", "head of"), "Senior role", -28),
        (("5+ years", "five years", "6+ years"), "Extensive experience required", -18),
        (("sales representative", "call center", "customer support"), "Sales/support focus", -22),
        (("software engineer", "devops", "site reliability"), "Unrelated engineering role", -25),
    ]
    for terms, label, points in negative:
        if any(term in text for term in terms):
            add(label, points)
    if "krak" in job.location.casefold() or "cracow" in job.location.casefold():
        add("Kraków location", 8)

    skills = extract_skills(text)
    profile_skills = {
        item.casefold() for item in ((profile.skills + profile.technologies) if profile else [])
    }
    missing = [skill for skill in skills if skill.casefold() not in profile_skills]
    categories = classify_job(text)
    return MatchResult(min(100, max(0, score)), explanation, skills, missing, categories)
