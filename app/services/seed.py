from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    Application,
    ApplicationActivity,
    CandidateProfile,
    CompanyPreference,
    CvVersion,
    Job,
    JobOccurrence,
    SavedSearch,
    utcnow,
)
from app.scrapers.base import JobPosting
from app.services.search import import_posting

WATCHLIST = [
    "State Street",
    "Motorola Solutions",
    "Shell",
    "ABB",
    "Aon",
    "ING",
    "Santander",
    "PKO BP",
    "Pekao",
    "BNP Paribas",
    "Citi",
    "mBank",
    "Alior",
    "Goldman Sachs",
    "JPMorgan",
    "UBS",
    "EY",
    "PwC",
    "Deloitte",
    "KPMG",
    "Accenture",
    "NielsenIQ",
    "Hitachi",
    "BBH",
    "HID",
    "HSBC",
    "Aptiv",
    "Capgemini",
    "Philip Morris International",
    "HEINEKEN Global Shared Services",
    "Cisco",
    "NBP",
    "GUS",
    "Polski Instytut Ekonomiczny",
]

DEMO_JOBS = [
    (
        "Financial Analyst Intern",
        "UBS",
        "Hybrid",
        "Internship",
        "Excel SQL reporting KPI financial analysis",
    ),
    (
        "Data Analytics Intern",
        "NielsenIQ",
        "Hybrid",
        "Internship",
        "Python SQL Power BI data analysis statistics",
    ),
    (
        "Junior FP&A Analyst",
        "Accenture",
        "Hybrid",
        "Full-time",
        "Excel budgeting forecasting variance analysis KPI",
    ),
    (
        "Risk Analyst Internship",
        "ING",
        "Hybrid",
        "Internship",
        "SQL risk reporting financial analysis English",
    ),
    (
        "Controlling Intern",
        "Deloitte",
        "On-site",
        "Internship",
        "Excel controlling budgeting reporting SAP",
    ),
    (
        "Research Assistant — Economics",
        "NBP",
        "On-site",
        "Part-time",
        "economic research statistics Python English",
    ),
    (
        "Business Analyst Graduate",
        "KPMG",
        "Hybrid",
        "Graduate",
        "business analysis Excel presentation reporting",
    ),
    (
        "Credit Analyst Intern",
        "BNP Paribas",
        "Hybrid",
        "Internship",
        "credit risk financial statements accounting Excel",
    ),
    ("Treasury Intern", "Citi", "Hybrid", "Internship", "treasury finance Excel reporting English"),
    (
        "Junior Reporting Analyst",
        "State Street",
        "Hybrid",
        "Full-time",
        "reporting KPI Excel SQL financial data",
    ),
    (
        "Investment Analysis Intern",
        "Goldman Sachs",
        "On-site",
        "Internship",
        "investment financial analysis Excel English",
    ),
    (
        "Finance Operations Intern",
        "Shell",
        "Hybrid",
        "Internship",
        "financial data reporting KPI Excel SAP",
    ),
    (
        "Market Analyst Intern",
        "PwC",
        "Hybrid",
        "Internship",
        "market research data analysis Excel presentation",
    ),
    (
        "Accounting & Reporting Intern",
        "EY",
        "Hybrid",
        "Internship",
        "accounting financial statements Excel reporting",
    ),
    (
        "Junior Data Analyst",
        "Santander",
        "Hybrid",
        "Full-time",
        "SQL Python Power BI databases statistics",
    ),
    (
        "Economic Research Intern",
        "GUS",
        "On-site",
        "Internship",
        "economics research statistics Excel Python",
    ),
    (
        "AML Analyst — Entry Level",
        "HSBC",
        "Hybrid",
        "Full-time",
        "risk data analysis reporting English",
    ),
    (
        "Corporate Finance Intern",
        "KPMG",
        "Hybrid",
        "Internship",
        "corporate finance valuation Excel financial analysis",
    ),
    (
        "Performance Reporting Analyst",
        "Brown Brothers Harriman",
        "Hybrid",
        "Full-time",
        "reporting Excel SQL KPI",
    ),
    (
        "Junior Business Intelligence Analyst",
        "mBank",
        "Remote",
        "Full-time",
        "Power BI SQL data analysis databases",
    ),
    (
        "Finance Transformation Intern",
        "Capgemini",
        "Hybrid",
        "Internship",
        "finance automation Excel Python reporting",
    ),
    (
        "Pricing Analyst Intern",
        "Philip Morris International",
        "Hybrid",
        "Internship",
        "Excel financial analysis data analysis",
    ),
    (
        "Audit Analytics Intern",
        "Deloitte",
        "Hybrid",
        "Internship",
        "accounting data analytics SQL Power BI",
    ),
    (
        "Senior Finance Manager",
        "Example Corp",
        "On-site",
        "Full-time",
        "manager 6+ years sales forecasting budgeting",
    ),
]


def seed_defaults(session: Session) -> None:
    profile = session.scalar(select(CandidateProfile).limit(1))
    if profile is None:
        profile = CandidateProfile(
            name="My Kraków profile",
            education="Economics / finance / analytics student",
            skills=["Excel", "Data analysis", "Statistics", "English", "Polish", "Reporting"],
            technologies=["SQL", "Python"],
            languages=["Polish", "English"],
            preferred_jobs=["Finance analyst", "Data analyst", "Business analyst", "Research"],
            preferred_industries=["Banking", "Consulting", "Analytics", "Economics"],
            preferred_locations=["Kraków"],
            remote_preference="Hybrid",
        )
        session.add(profile)
    for company in WATCHLIST:
        if not session.scalar(
            select(CompanyPreference).where(CompanyPreference.company == company)
        ):
            session.add(CompanyPreference(company=company))
    for name in [
        "CV — Finance Analyst",
        "CV — Data Analyst",
        "CV — Business Analyst",
        "CV — Generic Internship",
    ]:
        if not session.scalar(select(CvVersion).where(CvVersion.name == name)):
            session.add(CvVersion(name=name))
    if not session.scalar(select(SavedSearch).limit(1)):
        for name, keywords in [
            ("Finance internships", ["finance", "intern"]),
            ("Data analyst internships", ["data analyst", "intern"]),
            ("Business analyst internships", ["business analyst", "intern", "trainee"]),
            ("Economic research", ["economic", "research"]),
            ("Controlling / FP&A", ["controlling", "fp&a"]),
        ]:
            session.add(SavedSearch(name=name, keywords=keywords, locations=["Kraków"]))
    session.commit()


def seed_demo(session: Session) -> int:
    seed_defaults(session)
    profile = session.scalar(select(CandidateProfile).limit(1))
    if session.scalar(select(Job).where(Job.is_demo.is_(True)).limit(1)):
        return 0
    today = date.today()
    created: list[Job] = []
    for index, (title, company, remote, employment, terms) in enumerate(DEMO_JOBS, start=1):
        description = (
            f"Join {company} in Kraków. You will work with {terms}. "
            "You will analyze information, support the team, prepare clear insights "
            "and learn from experts. "
            "We value analytical thinking, communication and attention to detail."
        )
        posting = JobPosting(
            source="Demo",
            source_job_id=f"demo-{index}",
            url=f"https://example.invalid/jobs/demo-{index}",
            title=title,
            company=company,
            location="Kraków" if remote == "On-site" else f"Kraków · {remote}",
            remote_type=remote,
            employment_type=employment,
            seniority="Internship" if "Intern" in title else "Entry level",
            description=description,
            requirements=f"Required: {terms}. Interest in learning and fluent English.",
            responsibilities=(
                "Analyze data, prepare reports, work with stakeholders and track KPIs."
            ),
            date_posted=today - timedelta(days=index % 12),
            raw_payload={"demo": True},
        )
        job, _ = import_posting(session, posting, profile)
        job.is_demo = True
        job.date_discovered = utcnow() - timedelta(days=index % 9)
        job.first_seen = job.date_discovered
        created.append(job)
    session.flush()
    statuses = ["Saved", "Preparing", "Applied", "Recruiter Screen", "Final Interview", "Rejected"]
    for job, status in zip(created[2:8], statuses, strict=False):
        application = Application(job=job, status=status)
        if status not in {"Saved", "Preparing"}:
            application.date_applied = utcnow() - timedelta(days=4)
        session.add(application)
        session.flush()
        session.add(
            ApplicationActivity(
                application=application,
                event_type="status",
                description=f"Status changed to {status}",
            )
        )
    session.commit()
    return len(created)


def remove_demo(session: Session) -> int:
    ids = session.scalars(select(Job.id).where(Job.is_demo.is_(True))).all()
    if ids:
        session.execute(delete(JobOccurrence).where(JobOccurrence.job_id.in_(ids)))
        session.execute(delete(Job).where(Job.id.in_(ids)))
        session.commit()
    return len(ids)
