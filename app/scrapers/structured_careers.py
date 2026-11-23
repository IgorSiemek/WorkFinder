from __future__ import annotations

import json
import re
from urllib.parse import urlencode, urlsplit

from app.scrapers.base import JobPosting, SearchCriteria
from app.scrapers.career_pages import CareerPageSource
from app.scrapers.common import is_internship_role, is_talent_pool, plain_text, relevant_to_krakow
from app.scrapers.early_careers import public_client
from app.scrapers.employer_feeds import matches
from app.scrapers.publication_dates import PublicationDate, date_evidence, source_date


class SmartRecruitersSource(CareerPageSource):
    """Read the postings and details published by a SmartRecruiters company board."""

    def search_jobs(self, criteria: SearchCriteria) -> list[JobPosting]:
        self.scan_complete = self.can_confirm_absence = False
        missing_city = False
        company_slug = urlsplit(self.config.url).path.strip("/")
        api_url = f"https://api.smartrecruiters.com/v1/companies/{company_slug}/postings"
        seen: set[str] = set()
        results: list[JobPosting] = []
        offset = 0
        with public_client() as client:
            for _ in range(20):
                page = client.get(api_url, params={"limit": 100, "offset": offset})
                page.raise_for_status()
                data = page.json()
                rows, total = data["content"], data["totalFound"]
                before = len(seen)
                for row in rows:
                    identifier = str(row["id"])
                    if not identifier.isdecimal():
                        raise ValueError("Invalid SmartRecruiters posting ID")
                    if identifier in seen:
                        continue
                    seen.add(identifier)
                    employment = (row.get("typeOfEmployment") or {}).get("label", "")
                    if not is_internship_role(row["name"], employment):
                        continue
                    detail = client.get(f"{api_url}/{identifier}")
                    if detail.status_code in {404, 410}:
                        continue
                    detail.raise_for_status()
                    item = detail.json()
                    if not item["active"] or item["visibility"] != "PUBLIC":
                        continue
                    location = item["location"]
                    city = location.get("city") or ""
                    missing_city = missing_city or (not city and not location.get("remote"))
                    if not relevant_to_krakow(city, "", bool(location.get("remote"))):
                        continue
                    url = item["postingUrl"]
                    parsed_url = urlsplit(url)
                    if (
                        parsed_url.scheme != "https"
                        or parsed_url.netloc != "jobs.smartrecruiters.com"
                    ):
                        raise ValueError("Unexpected SmartRecruiters posting URL")
                    sections = item["jobAd"]["sections"]
                    description = plain_text(sections.get("jobDescription", {}).get("text"))
                    requirements = plain_text(sections.get("qualifications", {}).get("text"))
                    additional = plain_text(sections.get("additionalInformation", {}).get("text"))
                    description = "\n\n".join(
                        part for part in (description, requirements, additional) if part
                    )
                    title = plain_text(item["name"])
                    published = item.get("releasedDate")
                    posted = source_date(published)
                    posting = JobPosting(
                        source=self.name,
                        source_job_id=identifier,
                        url=url,
                        title=title,
                        company=self.config.company,
                        location=city,
                        description=description,
                        remote_type="Hybrid" if location.get("hybrid") else "On-site",
                        employment_type=item["typeOfEmployment"]["label"],
                        date_posted=posted,
                        raw_payload={
                            "talent_pool": is_talent_pool(title, description),
                            "_publication_check": date_evidence(
                                PublicationDate(
                                    posted, "SmartRecruiters releasedDate", str(published)
                                )
                                if posted
                                else PublicationDate(),
                                url,
                            ),
                        },
                    )
                    if matches(posting, criteria):
                        results.append(posting)
                if len(seen) == total:
                    self.scan_complete = self.can_confirm_absence = not missing_city
                    break
                if len(seen) == before or offset + len(rows) >= total:
                    break
                offset += len(rows)
        self.last_search_note = (
            "SmartRecruiters internship location unavailable"
            if missing_city
            else ""
            if self.scan_complete
            else "SmartRecruiters pagination incomplete"
        )
        return results


class TauronSource(CareerPageSource):
    """Read the Kraków internships in TAURON Dystrybucja's public eRecruiter widget."""

    board = "45dc4cb9b14a49bb8000746aa2316da0"
    origin = "https://skk.erecruiter.pl"
    rows = re.compile(r"<tr\b([^>]*)>(.*?)</tr>", re.I | re.S)
    attributes = re.compile(r"(\w+)\s*=\s*(['\"])(.*?)\2")
    cells = re.compile(r"<td\b[^>]*>(.*?)</td>", re.I | re.S)

    def search_jobs(self, criteria: SearchCriteria) -> list[JobPosting]:
        self.scan_complete = self.can_confirm_absence = False
        self.last_search_note = "TAURON pagination incomplete"
        missing_city = False
        seen: set[str] = set()
        results: list[JobPosting] = []
        with public_client() as client:
            for page in range(1, 21):
                response = client.get(
                    f"{self.origin}/GetHtml.ashx",
                    params={
                        "cfg": self.board,
                        "grid": "rows",
                        "pn": page,
                        "jsoncallback": "workfinder",
                    },
                )
                response.raise_for_status()
                raw = (
                    response.text.strip()
                    .removesuffix(";")
                    .removeprefix("workfinder(")
                    .removesuffix(")")
                )
                rows = self.rows.findall(json.loads(raw)["htm"])
                if not rows:
                    raise ValueError("TAURON careers response contains no result rows")
                footer = self.row_attributes(rows[-1][0])
                if footer.get("pn") != str(page):
                    break
                total, pages = int(footer["tr"]), int(footer["tp"])
                before = len(seen)
                for attrs, row_html in rows[:-1]:
                    row = self.row_attributes(attrs)
                    if row.get("skkresult") != "offer":
                        continue
                    identifier = row["jobofferid"]
                    if identifier in seen:
                        continue
                    seen.add(identifier)
                    title, city, contract, level, company, deadline = map(
                        plain_text, self.cells.findall(row_html)
                    )
                    if company.casefold() != "tauron dystrybucja s.a." or not is_internship_role(
                        title, contract, level
                    ):
                        continue
                    missing_city = missing_city or not city
                    if not relevant_to_krakow(city, "", False):
                        continue
                    query = urlencode(
                        {
                            "oid": row["offerid"],
                            "cfg": self.board,
                            "ejoId": row["externaljobofferid"],
                            "ejorId": row["externaljobofferregionid"],
                            "comId": row["comid"],
                        }
                    )
                    url = f"{self.origin}/Offer.aspx?{query}"
                    detail = client.get(url)
                    detail.raise_for_status()
                    if not re.search(r"id=['\"]JobTitle['\"]", detail.text, re.I):
                        raise ValueError("TAURON job details are unavailable")
                    description = plain_text(detail.text)
                    posting = JobPosting(
                        source=self.name,
                        source_job_id=identifier,
                        url=url,
                        title=title,
                        company=self.config.company,
                        location=city,
                        description=description,
                        employment_type=contract,
                        seniority=level,
                        remote_type="Hybrid" if "hybrydow" in description.casefold() else "On-site",
                        raw_payload={"deadline_label": deadline},
                    )
                    if matches(posting, criteria):
                        results.append(posting)
                if page >= pages:
                    self.scan_complete = self.can_confirm_absence = (
                        len(seen) == total and not missing_city
                    )
                    if self.scan_complete:
                        self.last_search_note = ""
                    break
                if len(seen) == before:
                    break
        if missing_city:
            self.last_search_note = "TAURON internship location unavailable"
        return results

    @classmethod
    def row_attributes(cls, markup: str) -> dict[str, str]:
        return {name.casefold(): value for name, _, value in cls.attributes.findall(markup)}
