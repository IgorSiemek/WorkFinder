from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from app.config import settings
from app.scrapers.common import parse_date, plain_text


@dataclass(frozen=True)
class PublicationDate:
    value: date | None = None
    method: str = "not_found"
    evidence: str = ""
    estimated: date | None = None


def date_evidence(result: PublicationDate, url: str) -> dict:
    return {
        "status": "verified" if result.value else "relative" if result.evidence else "not_found",
        "date": result.value.isoformat() if result.value else None,
        "estimated_date": result.estimated.isoformat() if result.estimated else None,
        "method": result.method,
        "evidence": result.evidence,
        "source_url": url,
        "checked_on": datetime.now(UTC).date().isoformat(),
        "checked_at": datetime.now(UTC).isoformat(),
    }


def source_date(value: object, *, today: date | None = None) -> date | None:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    try:
        parsed = parse_date(value)
    except (OverflowError, TypeError):
        return None
    return parsed if parsed and parsed <= (today or datetime.now(UTC).date()) else None


class DateMarkup(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self.dates: list[str] = []
        self.script: list[str] | None = None
        self.headings: list[str] = []
        self.heading: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "h1":
            self.heading = []
        if (
            tag == "script"
            and (attributes.get("type") or "").split(";")[0].lower() == "application/ld+json"
        ):
            self.script = []
        names = " ".join(attributes.get(key) or "" for key in ("itemprop", "name", "property"))
        if "dateposted" in names.casefold().split():
            value = attributes.get("content") or attributes.get("datetime")
            if value:
                self.dates.append(value)

    def handle_data(self, data: str) -> None:
        if self.script is not None:
            self.script.append(data)
        if self.heading is not None:
            self.heading.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.script is not None:
            self.scripts.append("".join(self.script))
            self.script = None
        if tag == "h1" and self.heading is not None:
            self.headings.append("".join(self.heading))
            self.heading = None


def job_nodes(value: object, depth: int = 0) -> list[dict]:
    if depth > 30:
        return []
    if isinstance(value, list):
        return [node for child in value for node in job_nodes(child, depth + 1)]
    if not isinstance(value, dict):
        return []
    types = value.get("@type", [])
    if isinstance(types, str):
        types = [types]
    if not isinstance(types, list):
        types = []
    own = [value] if any(str(kind).rsplit("/", 1)[-1] == "JobPosting" for kind in types) else []
    return own + [node for child in value.values() for node in job_nodes(child, depth + 1)]


def same_page(left: str, right: str) -> bool:
    a, b = urlsplit(left), urlsplit(right)
    return a.hostname == b.hostname and unquote(a.path).rstrip("/") == unquote(b.path).rstrip("/")


def extract_publication_date(html: str, url: str, title: str) -> PublicationDate:
    markup = DateMarkup()
    markup.feed(html)
    nodes = []
    for script in markup.scripts:
        try:
            nodes.extend(job_nodes(json.loads(script)))
        except (ValueError, RecursionError):
            continue
    matching = []
    for node in nodes:
        declared = node.get("url")
        if isinstance(declared, str):
            matches = same_page(urljoin(url, declared), url)
        else:
            matches = plain_text(node.get("title") or "").casefold() == title.casefold() or (
                len(nodes) == 1
                and bool(re.search(r"/(?:job|jobs|job-invite|oferta)/|Offer\.aspx", url, re.I))
            )
        if matches:
            parsed = source_date(node.get("datePosted"))
            if parsed:
                matching.append((parsed, str(node["datePosted"])))
    if len({item[0] for item in matching}) == 1:
        return PublicationDate(matching[0][0], "JobPosting datePosted", matching[0][1])
    # Do not borrow a recommendation's date or resolve conflicting job metadata by guessing.
    if nodes:
        return PublicationDate()
    dates = [(source_date(value), value) for value in markup.dates]
    dates = [(value, original) for value, original in dates if value is not None]
    if len({item[0] for item in dates}) == 1:
        return PublicationDate(dates[0][0], "datePosted markup", dates[0][1])
    if dates or not re.search(r"/(?:job|jobs|job-invite|oferta)/|Offer\.aspx", url, re.I):
        return PublicationDate()
    text = plain_text(html)
    page_title = plain_text(markup.headings[0] if markup.headings else title)
    title_at = text.casefold().find(page_title.casefold()) if page_title else -1
    if title_at < 0:
        return PublicationDate()
    text = text[max(0, title_at - 200) : title_at + 1500]
    absolute = re.findall(
        r"(?:date posted|posted on|publication date|data publikacji|opublikowano|dodano)"
        r"\s*:?\s*(\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{4})\b",
        text,
        re.IGNORECASE,
    )
    converted = [
        value if "-" in value else "-".join(reversed(value.split("."))) for value in absolute
    ]
    valid = {value for raw in converted if (value := source_date(raw)) is not None}
    if len(valid) == 1:
        return PublicationDate(valid.pop(), "Labelled publication date", absolute[0])
    relative = re.findall(r"\bPosted\s+(\d+)(\+?)\s+Days?\s+Ago\b", text, re.IGNORECASE)
    if len(relative) == 1:
        days, plus = relative[0]
        if int(days) > 36500:
            return PublicationDate()
        estimated = None if plus else datetime.now(UTC).date() - timedelta(days=int(days))
        return PublicationDate(
            method="Relative source label",
            evidence=f"Posted {days}{plus} days ago",
            estimated=estimated,
        )
    return PublicationDate()


def supported_url(url: str) -> bool:
    from app.scrapers.registry import company_career_pages

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    host = parsed.hostname or ""
    hosts = {urlsplit(item["url"]).hostname for item in company_career_pages().values()}
    hosts.update({"assaabloy.jobs2web.com", "skk.erecruiter.pl", "nbp.pl", "www.nbp.pl"})
    return bool(
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and port in {None, 443}
        and (host in hosts or host.endswith(".myworkdayjobs.com"))
    )


def lookup_publication_date(url: str, title: str) -> tuple[PublicationDate, dict]:
    if not supported_url(url):
        info = date_evidence(PublicationDate(), url)
        info["status"] = "unsupported"
        return PublicationDate(), info
    parsed = urlsplit(url)
    path = parsed.path
    if (parsed.hostname or "").endswith(".myworkdayjobs.com"):
        parts = path.strip("/").split("/")
        if parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
            parts.pop(0)
        if len(parts) >= 3 and parts[1] in {"job", "details"}:
            tenant = parsed.hostname.split(".")[0]
            path = f"/wday/cxs/{tenant}/{parts[0]}/job/" + "/".join(parts[2:])
            url = f"https://{parsed.hostname}{path}"
    try:
        for _ in range(5):
            response = httpx.get(
                url,
                timeout=min(settings.request_timeout_seconds, 5),
                headers={"User-Agent": settings.user_agent},
                follow_redirects=False,
            )
            if response.is_redirect:
                redirected = urljoin(url, response.headers.get("location", ""))
                if not supported_url(redirected):
                    raise ValueError("Unrecognized careers redirect")
                url = redirected
                continue
            response.raise_for_status()
            if "/wday/cxs/" in urlsplit(url).path:
                data = response.json().get("jobPostingInfo", {})
                posted = source_date(data.get("startDate"))
                result = PublicationDate(
                    posted, "Workday startDate", str(data.get("startDate") or "")
                )
                if posted is None:
                    result = PublicationDate()
            else:
                result = extract_publication_date(response.text[:2_000_000], url, title)
            return result, date_evidence(result, url)
        raise ValueError("Too many redirects")
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        info = date_evidence(PublicationDate(), url)
        info["status"] = "failed"
        return PublicationDate(), info
