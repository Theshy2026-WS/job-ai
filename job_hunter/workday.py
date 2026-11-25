"""Read verified Workday public job-detail paths with anonymous GET only.

This reader does not enumerate all Workday jobs, search via POST, log in, upload
files, or submit applications. The CVS fixture's jobPostingInfo.startDate was
cross-checked against its official careers postedDate (not dateCreated).
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .sources import SourceError, _record, _iso_publication, _country, _strings


TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
ORIGIN_HOST = re.compile(r"([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)\.wd\d+\.myworkdayjobs\.com")


def _plain_url(value):
    if not isinstance(value, str) or not value or any(c.isspace() for c in value) or "\\" in value:
        raise SourceError("Workday URLs must be nonempty HTTPS URLs without whitespace or backslashes")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise SourceError("Malformed Workday URL") from exc
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or port is not None
            or parsed.netloc.lower() != parsed.hostname.lower() or parsed.query or parsed.fragment):
        raise SourceError("Workday URL cannot contain credentials, ports, query or fragment")
    return parsed


def _job_path(value):
    if not isinstance(value, str) or len(value) > 2048 or not value.startswith("/job/"):
        raise SourceError("Workday job_paths must contain exact /job/.../slug_REQ paths")
    if any(char in value for char in ("%", "\\", "?", "#")):
        raise SourceError("Workday job path cannot contain encoded or alternate path syntax")
    parts = value.split("/")[1:]
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", part) or part in (".", "..") for part in parts):
        raise SourceError("Invalid Workday job path segment")
    if len(parts) < 2 or not re.fullmatch(r"[A-Za-z0-9_-]+_[A-Za-z0-9][A-Za-z0-9-]*", parts[-1]):
        raise SourceError("Workday job path must identify an explicit requisition slug")
    return value


def validate_workday_board(board: dict) -> dict:
    """Validate persisted tenant/site/origin/direct paths; return a copied dict."""
    if not isinstance(board, dict) or board.get("source") != "workday":
        raise SourceError("Expected a Workday board record")
    for field in ("company", "tenant", "site", "origin", "board"):
        if not isinstance(board.get(field), str) or not board[field].strip():
            raise SourceError(f"Workday {field} is required")
    tenant, site = board["tenant"], board["site"]
    if not TOKEN.fullmatch(tenant) or not TOKEN.fullmatch(site):
        raise SourceError("Workday tenant and site must be exact tokens")
    if board["board"] != tenant + "__" + site:
        raise SourceError("Workday board must exactly equal tenant__site")
    parsed = _plain_url(board["origin"])
    host_match = ORIGIN_HOST.fullmatch(parsed.hostname.lower())
    if not host_match or host_match.group(1) != tenant.lower() or parsed.path not in ("", "/"):
        raise SourceError("Workday origin must match https://{tenant}.wdN.myworkdayjobs.com")
    paths = board.get("job_paths")
    if not isinstance(paths, list) or not 1 <= len(paths) <= 100:
        raise SourceError("Workday requires 1-100 explicitly verified public job_paths")
    paths = list(dict.fromkeys(_job_path(path) for path in paths))
    return {**board, "origin":"https://" + parsed.hostname.lower(), "tenant":tenant, "site":site, "job_paths":paths}


def workday_base_url(board: dict) -> str:
    board = validate_workday_board(board)
    return f'{board["origin"]}/wday/cxs/{board["tenant"]}/{board["site"]}'


def normalize_workday_detail(board: dict, payload, job_path: str, source_url: str) -> dict | None:
    board = validate_workday_board(board)
    if job_path not in board["job_paths"]:
        raise SourceError("Unregistered Workday job path")
    expected_source = workday_base_url(board) + job_path
    if source_url != expected_source:
        raise SourceError("Workday detail URL does not match the registered origin/tenant/site/path")
    if not isinstance(payload, dict) or not isinstance(payload.get("jobPostingInfo"), dict):
        raise SourceError("Workday response must contain jobPostingInfo")
    info = payload["jobPostingInfo"]
    title, req, posting = info.get("title"), info.get("jobReqId"), info.get("jobPostingId")
    if any(not isinstance(value, str) or not value.strip() for value in (title, req, posting)):
        raise SourceError("Workday posting requires title, jobReqId and jobPostingId")
    expected_slug = job_path.rsplit("/", 1)[-1]
    if posting != expected_slug or not expected_slug.endswith("_" + req):
        raise SourceError("Workday response requisition does not match the requested job path")
    if info.get("jobPostingSiteId") != board["site"]:
        raise SourceError("Workday response belongs to a different posting site")
    if info.get("posted") is False or info.get("canApply") is False:
        return None
    if info.get("posted") is not True or info.get("canApply") is not True:
        raise SourceError("Workday response must explicitly identify an active public posting")
    external = _plain_url(info.get("externalUrl"))
    if "https://" + external.hostname.lower() != board["origin"]:
        raise SourceError("Workday external job URL must stay on the verified origin")
    parts = external.path.strip("/").split("/")
    if parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
        parts = parts[1:]
    if (len(parts) < 3 or parts[0] != board["site"] or parts[1] != "job"
            or parts[-1] != expected_slug or any(not re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in parts)):
        raise SourceError("Workday external URL must match the exact site and requisition")
    detail_path = "/" + "/".join(parts[1:])
    _job_path(detail_path)
    country = (info.get("jobRequisitionLocation") or {}).get("country") or info.get("country") or {}
    if not isinstance(country, dict):
        raise SourceError("Workday country must be structured metadata")
    country = _country(country.get("alpha2Code") or country.get("descriptor"))
    locations = _strings([info.get("location")] + list(info.get("additionalLocations") or []))
    published = _iso_publication(info.get("startDate"))
    url = info["externalUrl"]
    hiring = payload.get("hiringOrganization") or {}
    return _record(board, info, id=req, title=title, location="; ".join(locations), country=country,
        employment_type=info.get("timeType") or "unknown",
        employment_type_source="workday.jobPostingInfo.timeType" if info.get("timeType") else "",
        employment_type_evidence=source_url if info.get("timeType") else "",
        workplace_type=info.get("remoteType") or "unknown", description=info.get("jobDescription") or "",
        url=url, apply_url=url.rstrip("/") + "/apply", source_url=source_url,
        published_at=published, published_at_source="workday.jobPostingInfo.startDate" if published else "",
        published_at_evidence=source_url if published else "", internal_job_id=req,
        office_locations=locations, workday_origin=board["origin"], workday_tenant=board["tenant"],
        workday_site=board["site"], workday_job_path=job_path, workday_job_posting_id=posting,
        hiring_organization=hiring.get("name", "") if isinstance(hiring, dict) else "",
        application_deadline=info.get("endDate") or "", can_apply=True,
        source_coverage="verified_direct_paths_only", application_mode="manual",
        application_support_reason="Official Workday application requires its own supported browser workflow; public job reader does not submit applications")


def fetch_workday_board(board: dict, timeout: float = 20) -> list[dict]:
    """GET each registered detail only; no POST/search/application endpoint."""
    from . import sources
    board = validate_workday_board(board)
    base = workday_base_url(board)
    jobs = {}
    for path in board["job_paths"]:
        url = base + path
        payload = sources.fetch_json(url, timeout=timeout, follow_redirects=False)
        record = normalize_workday_detail(board, payload, path, url)
        if record is not None:
            jobs[record["id"]] = record
    return list(jobs.values())
