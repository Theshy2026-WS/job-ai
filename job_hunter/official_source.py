"""Read explicitly verified UHG public detail URLs; applications stay manual.

The supported public page publishes schema.org JobPosting JSON-LD and an
ordinary external Apply anchor. This module does not search a site, visit Taleo,
accept privacy agreements, collect credentials, upload or submit anything.
Only the reviewed UHG careers-to-external-Taleo route is supported initially.
"""
from __future__ import annotations

import json
import re
from html import unescape
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from .sources import SourceError, _JSONLDScripts, _job_postings, _record, _iso_publication, _country, _strings


OFFICIAL_DOMAIN = "unitedhealthgroup.com"
OFFICIAL_HOST = "careers.unitedhealthgroup.com"
APPLY_HOST = "uhg.taleo.net"
APPLY_PATH = "/careersection/10000/jobapply.ftl"
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
DETAIL_PATH = re.compile(r"/job/[a-z0-9-]+/[a-z0-9-]+/[0-9]+/[0-9]+")


def _https(value):
    if (not isinstance(value, str) or not value or len(value) > 2048
            or any(c.isspace() for c in value) or "\\" in value):
        raise SourceError("Official source URLs must be explicit nonempty HTTPS URLs")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise SourceError("Malformed official source URL") from exc
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or port is not None or parsed.fragment
            or parsed.netloc.lower() != parsed.hostname.lower()):
        raise SourceError("Official source URLs cannot contain credentials, ports or fragments")
    return parsed


def _apply_id(url):
    parsed = _https(url)
    if (parsed.hostname != APPLY_HOST or parsed.path != APPLY_PATH
            or not re.fullmatch(r"job=[0-9]+", parsed.query)):
        raise SourceError("Only the verified external UHG Taleo apply route is supported")
    return parse_qs(parsed.query)["job"][0]


def validate_official_board(board):
    """Validate exact public URLs and exact approved destinations before any GET."""
    if not isinstance(board, dict) or board.get("source") != "official":
        raise SourceError("Expected an official public-detail source")
    if not isinstance(board.get("company"), str) or not board["company"].strip():
        raise SourceError("Official source company is required")
    if not isinstance(board.get("board"), str) or not TOKEN.fullmatch(board["board"]):
        raise SourceError("Official board must be an explicit local source identifier")
    if board.get("official_domain") != OFFICIAL_DOMAIN:
        raise SourceError("This official reader supports only the verified UHG domain")
    if board.get("application_mode", "manual") != "manual":
        raise SourceError("Official detail sources support manual applications only")
    urls, applies = board.get("job_urls"), board.get("approved_apply_urls")
    for values, name in ((urls, "job_urls"), (applies, "approved_apply_urls")):
        if not isinstance(values, list) or not 1 <= len(values) <= 100:
            raise SourceError(f"Official source requires 1-100 explicitly verified {name}")
    for url in urls:
        parsed = _https(url)
        if parsed.hostname != OFFICIAL_HOST or parsed.query or not DETAIL_PATH.fullmatch(parsed.path):
            raise SourceError("Official job URL must be an exact verified UHG detail path")
    for apply_url in applies:
        _apply_id(apply_url)
    urls, applies = list(dict.fromkeys(urls)), list(dict.fromkeys(applies))
    if len(urls) != len(applies):
        raise SourceError("Each verified public job URL requires one exact approved apply URL")
    if len({_apply_id(url) for url in applies}) != len(applies):
        raise SourceError("Approved apply URLs must identify distinct requisitions")
    return {**board, "job_urls": urls, "approved_apply_urls": applies, "application_mode": "manual"}


class _PublicJobPage(_JSONLDScripts):
    def __init__(self):
        super().__init__()
        self.links = []
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if tag.lower() == "a":
            self.anchor = {"attrs": dict(attrs), "text": []}

    def handle_data(self, data):
        super().handle_data(data)
        if self.anchor is not None:
            self.anchor["text"].append(data)

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        if tag.lower() == "a" and self.anchor is not None:
            attrs = self.anchor["attrs"]
            self.links.append({"href": unescape(attrs.get("href") or ""),
                               "label": unescape(" ".join(self.anchor["text"])) or attrs.get("aria-label", "")})
            self.anchor = None


def _identifier(posting):
    value = posting.get("identifier")
    value = value.get("value") if isinstance(value, dict) else None
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).isdecimal():
        raise SourceError("Official JobPosting requires an explicit numeric requisition identifier")
    return str(value)


def _employment(value):
    values = value if isinstance(value, list) else [value]
    canonical = {"FULLTIME": "Full-time", "PARTTIME": "Part-time", "CONTRACTOR": "Contract",
                 "TEMPORARY": "Temporary", "INTERN": "Internship", "INTERNSHIP": "Internship"}
    types = {canonical.get(re.sub(r"[\s_-]", "", str(item)).upper(), "unknown") for item in values}
    return types.pop() if len(types) == 1 else "unknown"


def _locations(posting):
    locations = posting.get("jobLocation", [])
    locations = locations if isinstance(locations, list) else [locations]
    labels, countries = [], []
    for location in locations:
        address = location.get("address", {}) if isinstance(location, dict) else {}
        if not isinstance(address, dict):
            continue
        country = address.get("addressCountry", "")
        country = country.get("name", "") if isinstance(country, dict) else country
        if not isinstance(country, str):
            raise SourceError("Official location country must be structured text")
        labels.append(", ".join(_strings([address.get("addressLocality"), address.get("addressRegion"), country])))
        # UHG sometimes puts the literal Remote into addressCountry. Preserve
        # that location label but do not invent a country from it.
        if country and country.casefold() != "remote":
            countries.append(_country(country))
    countries = _strings(countries)
    return _strings(labels), countries


def normalize_official_page(board, content, source_url):
    board = validate_official_board(board)
    if source_url not in board["job_urls"]:
        raise SourceError("Unregistered official job detail URL")
    parser = _PublicJobPage()
    parser.feed(content)
    postings = []
    for script in parser.scripts:
        try:
            postings.extend(_job_postings(json.loads(script)))
        except (json.JSONDecodeError, RecursionError, TypeError):
            continue
    postings = [posting for posting in postings if posting.get("url") == source_url]
    if len(postings) != 1:
        raise SourceError("No unique JobPosting matches the exact registered official URL")
    posting = postings[0]
    req = _identifier(posting)
    approved = [url for url in board["approved_apply_urls"] if _apply_id(url) == req]
    observed = [link for link in parser.links if link["href"] in approved
                and re.search(r"\bapply\b", link["label"], re.I)]
    if len(approved) != 1 or not observed:
        raise SourceError("Official Apply link must match the approved Taleo URL and JSON-LD requisition")
    if not isinstance(posting.get("description"), str) or not posting["description"].strip():
        raise SourceError("Official JobPosting description is missing")
    published = _iso_publication(posting.get("datePosted"))
    employment = _employment(posting.get("employmentType"))
    locations, countries = _locations(posting)
    hiring = posting.get("hiringOrganization", {})
    return _record(board, posting, id=req, title=posting.get("title") or "", internal_job_id=req,
        description=posting["description"], location="; ".join(locations),
        country="US" if "US" in countries else (countries[0] if countries else ""), countries=countries,
        office_locations=locations, employment_type=employment,
        employment_type_source="jsonld.employmentType" if employment != "unknown" else "",
        workplace_type="Remote" if posting.get("jobLocationType") == "TELECOMMUTE" else "unknown",
        url=source_url, apply_url=approved[0], source_url=source_url,
        published_at=published, published_at_source="jsonld.datePosted" if published else "",
        published_at_evidence=source_url if published else "", base_salary=posting.get("baseSalary") or {},
        hiring_organization=hiring.get("name", "") if isinstance(hiring, dict) else "",
        application_mode="manual", source_coverage="verified_direct_urls_only",
        application_support_reason="Read-only official listing; Taleo application is manual and is never submitted by this reader",
        official_domain=board["official_domain"], official_job_url=source_url,
        official_approved_apply_urls=approved, official_apply_link_text=" ".join(observed[0]["label"].split()))


def fetch_official_board(board, timeout=20):
    from . import sources
    board = validate_official_board(board)
    jobs = {}
    for url in board["job_urls"]:
        try:
            html = sources.fetch_metadata_html(url, timeout=timeout, allowed_hosts={OFFICIAL_HOST})
        except (HTTPError, URLError, OSError, UnicodeError) as exc:
            raise SourceError(f"Official detail GET failed for {url}: {type(exc).__name__}") from exc
        record = normalize_official_page(board, html, url)
        jobs[record["id"]] = record
    return list(jobs.values())
