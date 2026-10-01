from __future__ import annotations

from dataclasses import dataclass

from app.scrapers.arbeitnow import ArbeitnowSource
from app.scrapers.base import JobSource
from app.scrapers.career_pages import CareerPageConfig, CareerPageSource
from app.scrapers.workday import WorkdayConfig, WorkdaySource


@dataclass(frozen=True)
class ProviderInfo:
    name: str
    status: str
    note: str


def _company_sources() -> list[JobSource]:
    workday_sources = [
        WorkdaySource(
            WorkdayConfig(
                name="BBH Careers",
                company="BBH",
                host="bbh.wd5.myworkdayjobs.com",
                tenant="bbh",
                site="BBH",
            )
        ),
        WorkdaySource(
            WorkdayConfig(
                name="Aptiv Careers",
                company="Aptiv",
                host="aptiv.wd5.myworkdayjobs.com",
                tenant="aptiv",
                site="APTIV_CAREERS",
            )
        ),
    ]
    pages = [
        CareerPageConfig(
            "Hitachi Careers",
            "Hitachi",
            "https://careers.hitachi.com/search/jobs",
        ),
        CareerPageConfig(
            "UBS Careers",
            "UBS",
            "https://jobs.ubs.com/TGnewUI/Search/home/"
            "HomeWithPreLoad?LinkID=12815&PageType=searchResults&"
            "SearchType=linkquery&partnerid=25008&siteid=5012",
        ),
        CareerPageConfig(
            "HID Careers",
            "HID",
            "https://careers.hidglobal.com/?field_jp_location_term_target_id=3174",
            True,
        ),
        CareerPageConfig(
            "HSBC Careers",
            "HSBC",
            "https://mycareer.hsbc.com/en_GB/external/SearchJobs/"
            "?pipelineRecordsPerPage=100&location=Krakow",
            True,
        ),
        CareerPageConfig(
            "Alior Careers",
            "Alior",
            "https://www.aliorbank.pl/dodatkowe-informacje/kariera/aktualne-oferty-pracy.html",
        ),
        CareerPageConfig(
            "Capgemini Careers",
            "Capgemini",
            "https://careers.capgemini.com/search/"
            "?createNewAlert=false&q=&locationsearch=Krak%C3%B3w",
            True,
        ),
        CareerPageConfig(
            "PMI Careers",
            "Philip Morris International",
            "https://join.pmicareers.com/gb/en/search-results?keywords=&location=Krakow",
            True,
        ),
        CareerPageConfig(
            "HEINEKEN Careers",
            "HEINEKEN Global Shared Services",
            "https://careers.theheinekencompany.com/Job-Listing?operatings_company%5B0%5D=272",
            True,
        ),
        CareerPageConfig(
            "Cisco Careers",
            "Cisco",
            "https://jobs.cisco.com/jobs/SearchJobs/?keyword=&location=Krakow%2C%20Poland",
            True,
        ),
    ]
    return workday_sources + [CareerPageSource(config) for config in pages]


def enabled_sources() -> list[JobSource]:
    return [ArbeitnowSource(), *_company_sources()]


def provider_catalog() -> list[ProviderInfo]:
    live = [
        ProviderInfo(source.name, "Enabled", source.description) for source in enabled_sources()
    ]
    requested = [
        "LinkedIn Jobs",
        "Pracuj.pl",
        "Talent Days",
        "EY",
        "PwC",
        "Deloitte",
        "KPMG",
        "Accenture",
        "ING",
        "Santander",
        "PKO BP",
        "Pekao",
        "BNP Paribas",
        "Citi",
        "mBank",
        "Goldman Sachs",
        "JPMorgan",
        "NielsenIQ",
        "NBP",
        "GUS",
        "Polski Instytut Ekonomiczny",
    ]
    return live + [
        ProviderInfo(name, "Planned", "No stable public feed verified; safe provider stub only.")
        for name in requested
    ]
