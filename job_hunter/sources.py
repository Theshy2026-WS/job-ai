"""Read-only clients for public employer ATS job boards (Python standard library).

API contracts, checked against the official documentation:
https://docs.greenhouse.io/job-board
https://github.com/lever/postings-api
https://developers.ashbyhq.com/docs/public-job-posting-api

These GET endpoints need no employer credentials. Application POST endpoints do;
this module deliberately does not attempt to submit or discover private keys.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from html import unescape
from html.parser import HTMLParser
import json
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


USER_AGENT = "WenfeiJobHunter/1.0 (public career-board reader)"
MAX_RESPONSE_BYTES = 30 * 1024 * 1024
RETRYABLE_CODES = {429, 500, 502, 503, 504}
ATS_HTML_HOSTS = {"jobs.lever.co", "boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.ashbyhq.com"}
MAX_HTML_BYTES = 3 * 1024 * 1024


def _iso_publication(value: Any) -> str:
    """Validate an explicit ISO publication Date/DateTime; never infer a date."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}(?:$|T|\s)", value):
        return ""
    try:
        if len(value) == 10:
            date.fromisoformat(value)
        else:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return value


class SourceError(RuntimeError):
    """An employer board could not be read or had an unexpected schema."""


class _NoPublicJSONRedirect(HTTPRedirectHandler):
    """Verified direct public endpoints must not redirect to another destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "li", "br", "div", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "li", "div", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def clean_html(value: Any) -> str:
    """Decode Greenhouse's nested HTML entities and retain paragraph boundaries."""
    text = str(value or "")
    for _ in range(3):
        decoded = unescape(text)
        if decoded == text:
            break
        text = decoded
    parser = _PlainText()
    parser.feed(text)
    text = "".join(parser.parts)
    text = re.sub(r"[^\S\n]+", " ", text)
    return re.sub(r"\n[ \t]*\n+", "\n", text).strip()


def fetch_json(url: str, timeout: float = 20, *, follow_redirects: bool = True) -> Any:
    """GET JSON, retrying transient status codes at most once.

    Retry-After is bounded at two seconds; socket/network failures are returned
    explicitly rather than hidden behind an empty job list. Never used for POST.
    """
    if urlparse(url).scheme != "https":
        raise SourceError("Only HTTPS public board URLs are accepted")
    open_request = urlopen if follow_redirects else build_opener(_NoPublicJSONRedirect()).open
    for attempt in range(2):
        try:
            request = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
            with open_request(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise SourceError("Board response exceeded 30 MiB safety limit")
                return json.loads(raw.decode("utf-8-sig"))
        except HTTPError as exc:
            if attempt == 0 and exc.code in RETRYABLE_CODES:
                try:
                    delay = min(2.0, max(0.25, float(exc.headers.get("Retry-After", "0.5"))))
                except (TypeError, ValueError):
                    delay = 0.5
                exc.close()
                time.sleep(delay)
                continue
            code = exc.code
            exc.close()
            raise SourceError(f"HTTP {code} fetching {url}") from exc
        except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceError(f"{type(exc).__name__} fetching {url}: {exc}") from exc
    raise SourceError(f"Unable to read {url}")


def _strings(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(str(v).strip() for v in values if v is not None and str(v).strip()))


def _country(code: Any) -> str:
    code = str(code or "").strip()
    return "US" if code.upper() in {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"} else code


def _record(board: dict, raw: dict, **fields: Any) -> dict:
    record = {
        "source": board["source"],
        "board": board["board"],
        "id": "",
        "company": board.get("company") or board["board"],
        "title": "",
        "location": "",
        "country": "",
        "employment_type": "unknown",
        "employment_type_source": "",
        "workplace_type": "unknown",
        "description": "",
        "url": "",
        "apply_url": "",
        "source_url": "",
        "published_at": "",
        "published_at_source": "",
        "published_at_evidence": "",
        "created_at": "",
        "updated_at": "",
    }
    record.update(fields)
    record["id"] = str(record["id"])
    record["description"] = clean_html(record["description"])
    if not record["id"] or not record["title"] or not record["url"]:
        raise SourceError(f"{board['source']}/{board['board']}: posting missing id, title, or URL")
    return record


def board_url(board: dict) -> str:
    """Return the public API URL for a configured board token."""
    source = str(board.get("source", "")).lower()
    if source == "workday":
        from .workday import workday_base_url
        return workday_base_url(board)
    if source == "official":
        from .official_source import validate_official_board
        return validate_official_board(board)["job_urls"][0]
    token = str(board.get("board", ""))
    if not token or not re.fullmatch(r"[A-Za-z0-9_.-]+", token):
        raise SourceError("Board token must contain only letters, numbers, '.', '_' or '-'")
    token = quote(token, safe="")
    if source == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    if source == "lever":
        return f"https://api.lever.co/v0/postings/{token}?mode=json"
    if source == "ashby":
        return f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
    raise SourceError(f"Unsupported source: {source!r}")


def normalize_greenhouse(board: dict, payload: Any, source_url: str) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise SourceError("Greenhouse response must contain a jobs array")
    jobs = []
    for raw in payload["jobs"]:
        if not isinstance(raw, dict):
            raise SourceError("Greenhouse jobs must be objects")
        # Greenhouse prospect/general-interest posts have no internal requisition.
        # Omit only an explicit null, not absent ids in older API responses.
        if "internal_job_id" in raw and raw["internal_job_id"] is None:
            continue
        offices = raw.get("offices") or []
        location = (raw.get("location") or {}).get("name") or ""
        office_locations = _strings([o.get("location") or o.get("name") for o in offices if isinstance(o, dict)])
        if not location:
            location = "; ".join(office_locations)
        metadata = raw.get("metadata") or []
        employment = "unknown"
        employment_source = ""
        workplace = "unknown"
        country = ""
        for item in metadata:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip().lower().replace("_", " ")
            value = item.get("value")
            if value is None:
                continue
            if name in {"employment type", "employment status", "job type", "commitment", "time type"}:
                employment = str(value)
                employment_source = "greenhouse.metadata." + str(item.get("name", ""))
            elif name in {"workplace type", "work type", "work arrangement"}:
                workplace = str(value)
            elif name in {"country", "job country"}:
                country = _country(value)
        url = raw.get("absolute_url") or f"https://job-boards.greenhouse.io/{board['board']}/jobs/{raw.get('id', '')}"
        jobs.append(_record(board, raw, id=raw.get("id", ""), title=raw.get("title", ""),
                            location=location, country=country, employment_type=employment,
                            employment_type_source=employment_source,
                            employment_type_evidence=source_url if employment_source else "",
                            workplace_type=workplace, description=raw.get("content", ""),
                            url=url, apply_url=f"https://job-boards.greenhouse.io/{board['board']}/jobs/{raw.get('id', '')}", source_url=source_url,
                            office_locations=office_locations,
                            departments=_strings([d.get("name") for d in raw.get("departments", []) if isinstance(d, dict)]),
                            internal_job_id=raw.get("internal_job_id"),
                            updated_at=raw.get("updated_at", ""), created_at=raw.get("created_at", ""),
                            first_published=raw.get("first_published", ""),
                            published_at=_iso_publication(raw.get("first_published")),
                            published_at_source="greenhouse.first_published" if _iso_publication(raw.get("first_published")) else "",
                            published_at_evidence=source_url if _iso_publication(raw.get("first_published")) else ""))
    return jobs


def normalize_lever(board: dict, payload: Any, source_url: str) -> list[dict]:
    if not isinstance(payload, list):
        raise SourceError("Lever response must be an array")
    jobs = []
    for raw in payload:
        if not isinstance(raw, dict):
            raise SourceError("Lever jobs must be objects")
        categories = raw.get("categories") or {}
        blocks = [raw.get("openingPlain") or raw.get("opening"),
                  raw.get("descriptionPlain") or raw.get("description")]
        for item in raw.get("lists", []) or []:
            if isinstance(item, dict):
                blocks.extend([item.get("text"), item.get("content")])
        blocks.extend([raw.get("additionalPlain") or raw.get("additional"),
                       raw.get("closingPlain") or raw.get("closing")])
        url = raw.get("hostedUrl") or f"https://jobs.lever.co/{board['board']}/{raw.get('id', '')}"
        locations = _strings([categories.get("location")] + list(categories.get("allLocations") or []))
        jobs.append(_record(board, raw, id=raw.get("id", ""), title=raw.get("text", ""),
                            location="; ".join(locations), country=_country(raw.get("country")),
                            employment_type=categories.get("commitment") or "unknown",
                            workplace_type=raw.get("workplaceType") or "unknown",
                            description="\n".join(str(s) for s in blocks if s),
                            url=url, apply_url=raw.get("applyUrl") or url + "/apply", source_url=source_url,
                            departments=_strings([categories.get("department"), categories.get("team")]),
                            created_at=raw.get("createdAt", ""), updated_at=raw.get("updatedAt", "")))
    return jobs


def normalize_ashby(board: dict, payload: Any, source_url: str) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise SourceError("Ashby response must contain a jobs array")
    jobs = []
    for raw in payload["jobs"]:
        if not isinstance(raw, dict):
            raise SourceError("Ashby jobs must be objects")
        if raw.get("isListed") is False:
            continue
        secondary = [item for item in raw.get("secondaryLocations", []) or [] if isinstance(item, dict)]
        addresses = [(raw.get("address") or {}).get("postalAddress") or {}]
        addresses += [item.get("address") or {} for item in secondary]
        countries = _strings([_country(a.get("addressCountry")) for a in addresses])
        # A multi-country posting remains eligible for downstream US matching.
        country = "US" if "US" in countries else (countries[0] if len(countries) == 1 else "")
        url = raw.get("jobUrl") or ""
        job_id = raw.get("id") or urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        locations = _strings([raw.get("location")] + [s.get("location") for s in secondary])
        jobs.append(_record(board, raw, id=job_id, title=raw.get("title", ""),
                            location="; ".join(locations), country=country,
                            employment_type=raw.get("employmentType") or "unknown",
                            workplace_type=raw.get("workplaceType") or ("Remote" if raw.get("isRemote") else "unknown"),
                            description=raw.get("descriptionPlain") or raw.get("descriptionHtml", ""),
                            url=url, apply_url=raw.get("applyUrl") or url + "/application", source_url=source_url,
                            departments=_strings([raw.get("department"), raw.get("team")]),
                            countries=countries,
                            # Ashby explicitly documents this as LAST publication,
                            # which may be a republication. Preserve that distinction.
                            last_published_at=raw.get("publishedAt", ""),
                            published_at=_iso_publication(raw.get("publishedAt")),
                            published_at_source="ashby.publishedAt (last published)" if _iso_publication(raw.get("publishedAt")) else "",
                            published_at_evidence=source_url if _iso_publication(raw.get("publishedAt")) else "",
                            created_at=raw.get("createdAt", ""), updated_at=raw.get("updatedAt", ""),
                            compensation=raw.get("compensation") or {}))
    return jobs


NORMALIZERS = {"greenhouse": normalize_greenhouse, "lever": normalize_lever, "ashby": normalize_ashby}


def fetch_board(board: dict, timeout: float = 20) -> list[dict]:
    if str(board.get("source", "")).lower() == "workday":
        from .workday import fetch_workday_board
        return fetch_workday_board(board, timeout=timeout)
    if str(board.get("source", "")).lower() == "official":
        from .official_source import fetch_official_board
        return fetch_official_board(board, timeout=timeout)
    url = board_url(board)
    payload = fetch_json(url, timeout=timeout)
    return NORMALIZERS[board["source"]](board, payload, url)


def fetch_jobs(boards: list[dict], timeout: float = 20, max_workers: int = 6) -> tuple[list[dict], list[dict]]:
    """Read configured boards concurrently; return jobs and explicit per-board errors.

    A failed board is never represented as a successful empty board. Duplicate
    source/board/id entries are collapsed. Results are sorted for reproducibility.
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if not 1 <= max_workers <= 16:
        raise ValueError("max_workers must be between 1 and 16")
    jobs: list[dict] = []
    errors: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_board, board, timeout): board for board in boards}
        for future in as_completed(futures):
            board = futures[future]
            try:
                jobs.extend(future.result())
            except Exception as exc:
                errors.append({"source": board.get("source", ""), "board": board.get("board", ""),
                               "company": board.get("company", ""), "error": str(exc)})
    deduped = {(j["source"], j["board"], j["id"]): j for j in jobs}
    return (sorted(deduped.values(), key=lambda j: (j["company"].lower(), j["title"].lower(), j["id"])),
            sorted(errors, key=lambda e: (e["source"], e["board"])))


class _NoHTMLRedirect(HTTPRedirectHandler):
    """One GET per enrichment attempt; never follow a redirected application URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_metadata_html(url: str, timeout: float = 15, *, allowed_hosts=None) -> str:
    """One bounded anonymous metadata GET on an explicit exact host allowlist.

    The caller supplies already-verified public detail URLs. No redirects,
    retries, credentials, ports, application POSTs or browser interaction.
    """
    hosts = ATS_HTML_HOSTS if allowed_hosts is None else set(allowed_hosts)
    try:
        parsed = urlparse(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise SourceError("Malformed public metadata URL") from exc
    if (not isinstance(url, str) or any(c.isspace() for c in url) or "\\" in url
            or parsed.scheme != "https" or parsed.hostname not in hosts
            or parsed.username is not None or parsed.password is not None or port is not None):
        raise SourceError("Metadata GET requires an approved exact HTTPS host without credentials or ports")
    if timeout <= 0:
        raise SourceError("Metadata GET timeout must be positive")
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    with build_opener(_NoHTMLRedirect()).open(request, timeout=timeout) as response:
        raw = response.read(MAX_HTML_BYTES + 1)
        if len(raw) > MAX_HTML_BYTES:
            raise SourceError("Employment enrichment HTML exceeded 3 MiB limit")
        # Also guard against an incorrectly configured/custom transport that
        # followed a redirect despite the explicit no-redirect handler.
        get_url = getattr(response, "geturl", None)
        if callable(get_url) and get_url() != url:
            raise SourceError("Public metadata response changed the requested URL")
        return raw.decode("utf-8-sig")


class _JSONLDScripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[str] = []
        self.parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag.lower() == "script" and str(attributes.get("type", "")).lower().split(";", 1)[0].strip() == "application/ld+json":
            self.parts = []

    def handle_data(self, data: str) -> None:
        if self.parts is not None:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self.parts is not None:
            self.scripts.append("".join(self.parts))
            self.parts = None


def _job_postings(value: Any):
    if isinstance(value, dict):
        kinds = value.get("@type", [])
        if isinstance(kinds, str):
            kinds = [kinds]
        if any(str(kind).rstrip("/").rsplit("/", 1)[-1] == "JobPosting" for kind in kinds):
            yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _job_postings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _job_postings(child)


def _posting_identity(posting: dict, job: dict) -> bool:
    """Only disambiguate multiple JobPostings with explicit URL or identifier."""
    urls = {str(job.get(key, "")).rstrip("/") for key in ("url", "apply_url") if job.get(key)}
    if any(str(posting.get(key, "")).rstrip("/") in urls for key in ("url", "@id")):
        return True
    identifier = posting.get("identifier")
    identifiers = identifier if isinstance(identifier, list) else [identifier]
    for item in identifiers:
        value = item.get("value") if isinstance(item, dict) else item
        if value is not None and str(value) == str(job.get("id")):
            return True
    return False


def _enrich_page_metadata(job: dict, timeout: float = 15) -> dict:
    """Read explicit JobPosting employmentType/datePosted from a hosted ATS page.

    This makes one bounded read-only HTTPS GET, never retries or follows a
    redirect, and never submits information. Missing/ambiguous schema data is
    retained as unknown. Existing explicit API values are never overwritten.
    """
    result = dict(job)
    needs_employment = str(job.get("employment_type") or "unknown").strip().lower() in {"unknown", ""}
    needs_publication = not (result.get("published_at") and result.get("published_at_source"))
    if not needs_employment and not needs_publication:
        return result
    # Job description pages normally carry JobPosting JSON-LD; application
    # forms often do not. Prefer the description URL when it is on this ATS.
    expected_hosts = {"greenhouse":{"boards.greenhouse.io","job-boards.greenhouse.io"},
                      "lever":{"jobs.lever.co"}, "ashby":{"jobs.ashbyhq.com"}}.get(job.get("source"), ATS_HTML_HOSTS)
    candidates = [str(job.get(key) or "") for key in ("url", "apply_url")]
    url = next((candidate for candidate in candidates
                if urlparse(candidate).scheme == "https" and urlparse(candidate).hostname in expected_hosts
                and urlparse(candidate).username is None and urlparse(candidate).password is None),
               next((candidate for candidate in candidates if candidate), ""))
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ATS_HTML_HOSTS or parsed.username is not None or parsed.password is not None:
        result["enrichment_error"] = "Employment enrichment requires an HTTPS hosted ATS URL"
        return result
    if timeout <= 0:
        result["enrichment_error"] = "Employment enrichment timeout must be positive"
        return result
    try:
        content = fetch_metadata_html(url, timeout=timeout)
        parser = _JSONLDScripts()
        parser.feed(content)
        postings: list[dict] = []
        for script in parser.scripts:
            try:
                postings.extend(_job_postings(json.loads(script)))
            except (json.JSONDecodeError, RecursionError, TypeError):
                continue
        if len(postings) > 1:
            postings = [posting for posting in postings if _posting_identity(posting, job)]
        if len(postings) != 1:
            return result
        # datePosted means publication of the listing. Do not borrow dateModified,
        # dateCreated, validThrough, retrieval time, or the current date.
        # https://schema.org/datePosted
        published = _iso_publication(postings[0].get("datePosted"))
        if needs_publication and published:
            result.update(published_at=published, published_at_source="jsonld.datePosted", published_at_evidence=url)
        if not needs_employment:
            result.pop("enrichment_error", None)
            return result
        value = postings[0].get("employmentType")
        values = value if isinstance(value, list) else [value]
        # Schema.org standard values plus equivalent unambiguous spelling.
        canonical = {
            "FULLTIME": "Full-time", "PARTTIME": "Part-time", "CONTRACTOR": "Contract",
            "CONTRACT": "Contract", "TEMPORARY": "Temporary", "INTERN": "Internship",
            "INTERNSHIP": "Internship", "VOLUNTEER": "Volunteer", "PERDIEM": "Per diem", "OTHER": "Other",
        }
        normalized = []
        for item in values:
            if not isinstance(item, str):
                return result
            key = re.sub(r"[\s_-]", "", item).upper()
            if key not in canonical:
                return result
            normalized.append(canonical[key])
        distinct = set(normalized)
        if len(distinct) != 1:
            return result
        result["employment_type"] = distinct.pop()
        result["employment_type_evidence"] = url
        result.pop("enrichment_error", None)
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, SourceError) as exc:
        result["enrichment_error"] = f"{type(exc).__name__}: {exc}"
    return result


def enrich_employment(job: dict, timeout: float = 15) -> dict:
    """Backward-compatible employment enrichment; existing API types are kept."""
    if str(job.get("employment_type") or "unknown").strip().lower() not in {"unknown", ""}:
        return dict(job)
    return _enrich_page_metadata(job, timeout=timeout)


def enrich_job_metadata(job: dict, timeout: float = 15) -> dict:
    """Enrich publication and employment independently, without applicant data.

    Greenhouse's documented single-job GET exposes first_published even when its
    list endpoint omits it. Other publication evidence comes only from an official
    hosted ATS page's JobPosting.datePosted. Existing authoritative dates/types
    are retained. GETs are bounded; no application endpoint is ever called.
    """
    result = dict(job)
    # Remove unprovenanced legacy values (older Lever records incorrectly used
    # createdAt as published_at). They must not pass a publication-age filter.
    if result.get("published_at") and not result.get("published_at_source"):
        result["unverified_published_at"] = result["published_at"]
        result["published_at"] = ""
    result.setdefault("published_at", "")
    result.setdefault("published_at_source", "")
    result.setdefault("published_at_evidence", "")
    needs_employment = str(result.get("employment_type") or "unknown").strip().lower() in {"", "unknown"}
    if result.get("source") == "greenhouse" and (not result["published_at"] or needs_employment):
        token, identifier = str(result.get("board", "")), str(result.get("id", ""))
        if re.fullmatch(r"[A-Za-z0-9_.-]+", token) and identifier.isdecimal():
            detail_url = f"https://boards-api.greenhouse.io/v1/boards/{quote(token, safe='')}/jobs/{identifier}"
            try:
                detail = fetch_json(detail_url, timeout=timeout)
                if not isinstance(detail, dict) or str(detail.get("id")) != identifier:
                    raise SourceError("Greenhouse detail did not match the requested posting")
                published = _iso_publication(detail.get("first_published"))
                result["first_published"] = detail.get("first_published", "")
                if published:
                    result.update(published_at=published, published_at_source="greenhouse.first_published",
                                  published_at_evidence=detail_url)
                for key in ("updated_at", "created_at"):
                    if key in detail:
                        result[key] = detail[key]
                metadata_job = normalize_greenhouse({"source":"greenhouse", "board":token,
                                "company":result.get("company", token)}, {"jobs":[detail]}, detail_url)
                if metadata_job and str(result.get("employment_type") or "unknown").lower() == "unknown":
                    result["employment_type"] = metadata_job[0]["employment_type"]
                    if metadata_job[0].get("employment_type_source"):
                        result["employment_type_source"] = metadata_job[0]["employment_type_source"]
                        result["employment_type_evidence"] = detail_url
            except (OSError, RuntimeError, ValueError) as exc:
                result["metadata_error"] = f"Greenhouse detail unavailable: {type(exc).__name__}"
    return _enrich_page_metadata(result, timeout=timeout)
