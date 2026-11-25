"""Deterministic job matching against the supplied resume and H-1B constraints.

The checks classify the posting's wording, not the employer's immigration policy.
Unstated sponsorship is deliberately retained. Unknown full-time status is held
for review rather than silently converted to full-time.
"""

from __future__ import annotations

from html import unescape
import math
import re
from typing import Any
from datetime import date


def clean_text(value: Any) -> str:
    """Convert API HTML or plain text into whitespace-normalized readable text."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(clean_text(item) for item in value)
    text = str(value)
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(unescape(text).replace("\xa0", " ").split())


def _normalized(value: Any) -> str:
    text = clean_text(value).lower()
    text = text.replace("’", "'").replace("–", "-").replace("—", "-")
    text = re.sub(r"\bu\.?\s*s\.?\s*a\.?\b", "usa", text)
    text = re.sub(r"\bu\.\s*s\.(?=\s|$)", "us", text)
    text = re.sub(r"\bh\s*-?\s*1\s*-?\s*b\b", "h1b", text)
    return text


def _first_match(patterns: tuple[str, ...], text: str) -> str | None:
    for pattern in patterns:
        found = re.search(pattern, text, flags=re.I)
        if found:
            return found.group(0)
    return None


_CITIZENSHIP_RESTRICTIONS = (
    r"\b(?:us|usa|united states)\s+citizens?\s*(?:and|or|/)\s*(?:us\s+)?(?:green[- ]card\s+holders?|permanent\s+residents?|gc(?:\s+holders?)?)(?:\s+only)?\b(?!\s*[,/]?\s*(?:or|and)\s+(?:visa\s+holders?|have|hold|possess|be\s+authorized))",
    r"\b(?:only|must\s+be(?:\s+(?:an?|either))?|required\s+to\s+be(?:\s+an?)?)\s+(?:us|usa|united states)\s+citizens?\b(?!\s+or\s+(?:have|hold|possess|be\s+authorized))",
    r"\b(?:us|usa|united states)\s+citizens?\s+(?:only|are\s+required|is\s+required)\b",
    r"\b(?:us|usa|united states)\s+citizenship\s+(?:(?:is|will\s+be)\s+)?(?:required|mandatory)\b",
    r"\b(?:us|usa|united states)\s+citizenship\s+(?:and|,)\s+[^.;]{0,140}\b(?:are|is)\s+required\b",
    r"\b(?:require[sd]?|must\s+(?:have|hold|possess))\s+(?:us|usa|united states)\s+citizenship\b",
    r"\b(?:citizenship|citizen\s+status)\s*:\s*(?:us|usa|united states)\s+citizen\b",
    r"\bmust\s+be\s+(?:an?\s+)?citizen\s+of\s+(?:the\s+)?(?:us|usa|united states)\b",
    r"\b(?:us|usa|united states)\s+citizenship\s+or\s+(?:an?\s+)?green[- ]card\s+(?:is\s+)?required\b",
    r"\b(?:must\s+(?:be|hold|have)|only)\s+(?:an?\s+)?(?:us\s+)?(?:permanent\s+resident|green[- ]card(?:\s+holder)?)\b",
    r"\b(?:green[- ]card\s+holders?|permanent\s+residents?)\s+only\b",
    r"\b(?:usc\s*(?:/|or|and)\s*gc|gc\s*(?:/|or|and)\s*usc)(?:\s+only)?\b",
    r"\b(?:us\s+citizens?\s*/\s*gc|gc\s*/\s*us\s+citizens?)(?:\s+only)?\b",
)

_NO_SPONSORSHIP = (
    r"\b(?:cannot|can't|can\s+not|will\s+not|won't|do\s+not|does\s+not|unable\s+to|not\s+able\s+to)\s+(?:(?:currently|at\s+this\s+time|be\s+able\s+to)\s+)?(?:(?:provide|offer|support)\s+)?(?:(?:employment|visa|immigration|work[- ]visa)\s+)?sponsor(?:ship)?\b",
    r"\bno\s+(?:(?:employment|work|visa|immigration|h1b)\s+){0,3}sponsorship\b",
    r"\b(?:visa|immigration|employment|work[- ]visa)\s+sponsorship\s+(?:is\s+|will\s+be\s+)?(?:not\s+(?:available|provided|offered|supported)|unavailable)\b",
    r"\b(?:must|should|need\s+to)\s+(?:not\s+require|be\s+able\s+to\s+work\s+without)\s+(?:(?:employment|visa|immigration|employer)\s+)?sponsorship\b",
    r"\b(?:(?:authorized|authorization)\s+to\s+work|(?:employment|work)\s+authorization|eligible\s+(?:to\s+work|for\s+employment))\b[^.;\n]{0,100}\bwithout\s+(?:(?:current|future|employment|visa|immigration|employer|any|or|and)\s+)*sponsorship\b",
    r"\b(?:do\s+not|must\s+not|cannot)\s+(?:now\s+or\s+in\s+the\s+future\s+)?require\s+(?:(?:employment|visa|immigration|employer)\s+)?sponsorship\b",
    r"\b(?:h1b\s+transfers?)\s+(?:are\s+)?(?:not\s+(?:accepted|supported|allowed|available|eligible)|unavailable)\b",
    r"\b(?:not\s+eligible|ineligible)\s+for\s+(?:(?:visa|immigration|employment|h1b)\s+)?sponsorship\b",
    r"\b(?:candidates?|applicants?)\s+(?:requiring|needing)\s+(?:visa\s+)?sponsorship\s+(?:will|can)\s+not\s+be\s+considered\b",
    r"\b(?:cannot|can't|do\s+not|does\s+not|will\s+not)\s+(?:accept|support|allow)\s+(?:existing\s+)?h1b\s+transfers?\b",
    r"\bh1b\s+(?:candidates?|applicants?|holders?)\s+(?:are\s+)?not\s+eligible\b",
)

_H1B_TRANSFERS = (
    r"\bh1b\s+(?:visa\s+)?transfers?\s+(?:are\s+|is\s+)?(?:welcome|accepted|supported|available|eligible|considered|allowed|ok)\b",
    r"\b(?:accept|accepts|support|supports|welcome|welcomes|consider|considers|allow|allows)\s+(?:(?:existing|current)\s+)?h1b\s+(?:visa\s+)?transfers?\b",
    r"\b(?:candidates?|applicants?)\s+(?:requiring|needing)\s+(?:an?\s+)?h1b\s+transfer\s+(?:may|can)\s+apply\b",
    r"\bh1b\s+transfer\s+sponsorship\s+(?:is\s+)?available\b",
)

_SPONSORSHIP_SUPPORTED = (
    r"\b(?:visa|immigration|employment)\s+sponsorship\s+(?:is\s+)?(?:available|provided|offered|supported)\b",
    r"\b(?:we|company|employer)\s+(?:can|will|does)\s+(?:provide\s+|offer\s+)?(?:visa\s+)?sponsor(?:ship)?\b",
    r"\b(?:we|company|employer)\s+(?:sponsor|sponsors)\s+(?:work\s+visas|h1b|visas)\b",
    r"\b(?:open\s+to|provides?|offers?)\s+(?:visa\s+|immigration\s+|h1b\s+)?sponsorship\b",
)


def _sponsorship(text: str) -> tuple[str, str]:
    # Explicitly inclusive alternatives avoid interpreting work-authorization
    # eligibility as citizenship-only. EEO boilerplate is not a requirement.
    # Segment first: an unanchored [^.;]* search becomes quadratic on long ATS
    # bullet lists without sentence punctuation.
    screened = " ".join(
        sentence for sentence in re.split(r"(?<=[.;\n])", text)
        if "without regard to" not in sentence and "regardless of" not in sentence
    )
    screened = re.sub(
        r"\b(?:us|usa|united states)\s+citizenship\s+(?:is\s+)?not\s+required\b",
        " ", screened,
    )
    restriction = _first_match(_CITIZENSHIP_RESTRICTIONS, screened)
    if restriction:
        return "restricted", f"Explicit citizenship/permanent-residency restriction: {restriction}"
    no_sponsorship = _first_match(_NO_SPONSORSHIP, screened)
    transfer = _first_match(_H1B_TRANSFERS, screened)
    no_transfers = re.search(r"\bh1b\s+transfers?\s+(?:are\s+)?not\s+(?:accepted|supported|allowed|available|eligible)\b|\b(?:cannot|can't|do\s+not|does\s+not|will\s+not)\s+(?:accept|support|allow)\s+(?:existing\s+)?h1b\s+transfers?\b", screened)
    if transfer and not no_transfers:
        return "supported", "Posting explicitly accepts H-1B transfers."
    if no_sponsorship:
        return "restricted", f"Explicit sponsorship restriction: {no_sponsorship}"
    if _first_match(_SPONSORSHIP_SUPPORTED, screened):
        return "supported", "Posting explicitly mentions available visa sponsorship."
    return "unclear", "Sponsorship/H-1B transfer is not clearly stated; retained for review."


_ROLE_FULLTIME_SALARY = re.compile(
    r"\b(?:the\s+)?range\s+of\s+annual\s+base\s+salary\s+for\s+full[- ]?time\s+employees\s+for\s+this\s+position\s+is\s+below\b"
)


def _employment(job: dict[str, Any], text: str) -> str:
    title = _normalized(job.get("title"))
    value = _normalized(job.get("employment_type"))
    # The role's title can expose a contract even when the ATS says FullTime.
    combined = f"{value} {title}"
    if re.search(r"\b(?:intern|internship|co[- ]?op)\b", combined):
        return "internship"
    if re.search(r"\b(?:contract(?:or)?|contract[- ]to[- ]hire|c2h|freelance|contingent)\b", combined):
        return "contract"
    if re.search(r"\b(?:part[- ]?time|parttime)\b", combined):
        return "part-time"
    if re.search(r"\b(?:temporary|seasonal|temp)\b", combined):
        return "temporary"
    # A clear statement about this role outranks a conflicting FullTime API enum.
    # Do not search for bare "contract": data contracts and customer contracts
    # describe engineering work rather than the candidate's employment type.
    kind = r"contract(?:or)?|temporary|temp|seasonal|part[- ]?time|parttime|internship"
    modifiers = r"(?:(?:full[- ]?time|remote|hybrid|on[- ]site|fixed[- ]term|w-?2|\d+[- ](?:month|week|year))\s+){0,3}"
    role_type_patterns = (
        rf"\b(?:employment|job|position)\s+type\s*[:\-]\s*{modifiers}(?P<kind>{kind})\b",
        rf"\b(?:this|the)\s+(?:role|position|job|opportunity)\s+(?:is|will\s+be)\s+(?:offered\s+as\s+)?(?:an?\s+)?{modifiers}(?P<kind>{kind})\b",
        rf"\b(?:this\s+is\s+(?:an?\s+)?|this\s+){modifiers}(?P<kind>{kind})\s+(?:role|position|opportunity|job)\b",
    )
    for pattern in role_type_patterns:
        found = re.search(pattern, text)
        if found:
            detected = found.group("kind")
            if detected.startswith("part"):
                return "part-time"
            if detected in {"temp", "temporary", "seasonal"}:
                return "temporary"
            return "contract" if detected.startswith("contract") else "internship"
    # FullTime is Ashby's documented enum spelling; accept other feed spellings.
    if re.search(r"\b(?:full[- ]?time|fulltime)\b", value):
        return "full-time"
    # Verified employer wording ties full-time status to this specific position.
    # Generic benefits for full-time employees do not establish the role's type.
    if _ROLE_FULLTIME_SALARY.search(text):
        return "full-time"
    if re.search(r"\b(?:full[- ]?time|fulltime)\b", title):
        return "full-time"
    if re.search(r"\b(?:employment\s+type|job\s+type|position\s+type|schedule)\s*[:\-]?\s*full[- ]?time\b|\bfull[- ]?time\s+(?:role|position|opportunity|job|employment)\b", text):
        return "full-time"
    return "unknown"


_US_STATES = (
    "alabama alaska arizona arkansas california colorado connecticut delaware florida "
    "georgia hawaii idaho illinois indiana iowa kansas kentucky louisiana maine maryland "
    "massachusetts michigan minnesota mississippi missouri montana nebraska nevada "
    "ohio oklahoma oregon pennsylvania tennessee texas utah vermont virginia washington wisconsin wyoming"
).split() + ["new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota", "rhode island", "south carolina", "south dakota", "west virginia", "district of columbia"]
_STATE_CODES = "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR".split()
_US_CITIES = (
    "san francisco", "san jose", "san diego", "los angeles", "new york city", "nyc", "boston",
    "seattle", "chicago", "austin", "dallas", "houston", "atlanta", "denver", "phoenix", "miami",
    "philadelphia", "pittsburgh", "minneapolis", "hartford", "charlotte", "raleigh", "durham",
    "baltimore", "detroit", "nashville", "tampa", "orlando", "salt lake city", "stamford",
    "sf bay area",
)
_NON_US = (
    "canada", "toronto", "vancouver", "montreal", "ottawa", "united kingdom", "uk", "london",
    "germany", "berlin", "munich", "india", "bangalore", "bengaluru", "hyderabad", "pune", "mumbai",
    "australia", "sydney", "melbourne", "singapore", "ireland", "dublin", "netherlands", "amsterdam",
    "france", "paris", "poland", "warsaw", "spain", "madrid", "barcelona", "portugal", "lisbon",
    "japan", "tokyo", "china", "hong kong", "brazil", "mexico", "israel", "tel aviv", "new zealand",
    "romania", "bucharest", "czechia", "prague", "south africa", "nigeria", "philippines", "pakistan",
)


def _location(job: dict[str, Any]) -> str:
    location_raw = clean_text(job.get("location"))
    location = _normalized(location_raw)
    country = _normalized(job.get("country"))
    all_location = f"{country} {location}"
    if re.search(r"\b(?:united states(?: of america)?|usa|us)\b", all_location):
        return "us"
    if country and country not in {"unknown", "none", "null", "global", "worldwide", "remote", "n/a"}:
        # Explicit non-US ISO/name from an ATS outranks an ambiguous US city name.
        if country not in {"us", "usa", "united states", "united states of america"}:
            return "non-us"
    if re.search(r",\s*(?:" + "|".join(_STATE_CODES) + r")\b", location_raw):
        return "us"
    if any(re.search(r"\b" + re.escape(state) + r"\b", location) for state in _US_STATES):
        return "us"
    if any(re.search(r"\b" + re.escape(place) + r"\b", location) for place in _NON_US):
        return "non-us"
    if any(re.search(r"\b" + re.escape(city) + r"\b", location) for city in _US_CITIES):
        return "us"
    # A posting's structured office locations are role-specific evidence. Use
    # them only after ruling out an explicit non-US primary country/location,
    # and only when every supplied office can independently be identified as US.
    offices = job.get("office_locations") or []
    if isinstance(offices, str):
        offices = [offices]
    generic_primary = not location or bool(re.fullmatch(
        r"remote|hybrid|in[- ]office|on[- ]?site|unknown|remote\s*[-,:]\s*(?:" + "|".join(_STATE_CODES) + r")",
        location, flags=re.I,
    ))
    if generic_primary and isinstance(offices, (list, tuple)):
        nonempty_offices = [clean_text(office) for office in offices if clean_text(office)]
        if nonempty_offices and all(_location({"location": office}) == "us" for office in nonempty_offices):
            return "us"
    # Unqualified "Remote", "North America", and "Americas" don't prove US eligibility.
    return "unknown"


_SKILL_GROUPS = (
    ("Python", r"\bpython\b", 5),
    ("SQL", r"\bsql\b", 5),
    ("GCP", r"\b(?:gcp|google cloud(?: platform)?)\b", 9),
    ("BigQuery", r"\bbig\s?query\b", 8),
    ("Airflow", r"\bairflow\b", 7),
    ("Cloud Composer", r"\b(?:cloud\s+composer|composer)\b", 4),
    ("Starburst", r"\b(?:starburst|trino|presto)\b", 5),
    ("GKE", r"\b(?:gke|google kubernetes engine)\b", 4),
    ("Kubernetes", r"\bkubernetes\b", 3),
    ("Spark", r"\b(?:pyspark|apache spark|spark)\b", 4),
    ("ETL/data pipelines", r"\b(?:etl|elt|data pipelines?|data integration)\b", 5),
    ("Healthcare", r"\b(?:healthcare|health care|pharmacy|pbm|prior authorization|clinical)\b", 5),
    ("Data migration", r"\b(?:data migration|teradata|platform migration)\b", 3),
)


def _profile_text(profile: dict[str, Any]) -> str:
    # The supplied resume is the default when a lightweight profile contains only
    # application details. Explicit skills restrict which skill groups score.
    value = profile.get("skills")
    if not value:
        return "python sql gcp google cloud bigquery airflow cloud composer starburst trino gke kubernetes pyspark spark etl data pipelines healthcare pharmacy pbm prior authorization teradata data migration"
    if isinstance(value, dict):
        value = list(value.values())
    return _normalized(value)


def _reported_data_years(profile: dict[str, Any]) -> float | None:
    """Use the applicant's confirmed experience floor without altering job dates."""
    summary = profile.get("experience_summary") or {}
    if not isinstance(summary, dict):
        return None
    value = summary.get("data_engineering_years_min")
    if value is None or isinstance(value, bool):
        return None
    try:
        years = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return years if math.isfinite(years) and years >= 0 else None


def _industry_years(profile: dict[str, Any]) -> float:
    """Prefer confirmed DE experience; otherwise count dated industry work only.

    A confirmed seven-year minimum is not evidence of eight years, a particular
    employer's tenure, or seven years with any individual technology.
    """
    reported = _reported_data_years(profile)
    if reported is not None:
        return reported
    months: set[int] = set()
    for role in profile.get("experience", []):
        title = _normalized(role.get("title"))
        if re.search(r"\b(?:intern|internship|research|graduate|student|fellow|scholar)\b", title):
            continue
        start, end = str(role.get("start") or ""), str(role.get("end") or "")
        start_match = re.match(r"^(\d{4})-(\d{2})", start)
        if not start_match:
            continue
        start_year, start_month = map(int, start_match.groups())
        if end.casefold() in {"", "present", "current"}:
            today = date.today()
            end_year, end_month = today.year, today.month
        else:
            end_match = re.match(r"^(\d{4})-(\d{2})", end)
            if not end_match:
                continue
            end_year, end_month = map(int, end_match.groups())
        if 1 <= start_month <= 12 and 1 <= end_month <= 12:
            first, last = start_year * 12 + start_month, end_year * 12 + end_month
            if 0 <= last - first <= 600:
                months.update(range(first, last))
    return len(months) / 12


def _experience_requirements(job: dict[str, Any]) -> tuple[list[int], list[int]]:
    """Extract relevant minimum years; preserve optional and alternative wording.

    This is a review signal only. It does not conclude whether an employer will
    credit research, equivalent education, or transferable experience.
    """
    raw = str(job.get("description") or "")
    raw = re.sub(r"</(?:p|li|div)>|<br\s*/?>", "\n", raw, flags=re.I)
    required: list[int] = []
    optional: list[int] = []
    preferred_section = False
    for raw_line in raw.splitlines():
        line = _normalized(raw_line)
        if not line:
            continue
        if len(line) < 120:
            if re.search(r"nice\s+to\s+have|preferred\s+(?:qualifications|experience)|bonus\s+points|desired\s+qualifications", line):
                preferred_section = True
            elif re.search(r"requirements|minimum\s+qualifications|required\s+qualifications|skills\s+you.+need|what\s+we\s+require|what\s+we.re\s+looking|you\s+might\s+thrive|you\s+will\s+love", line):
                preferred_section = False
        for sentence in re.split(r"[.;]", line):
            years_in_sentence = []
            preferred = preferred_section or bool(re.search(r"\b(?:preferred|preferably|desirable|ideally|bonus)\b|\ba\s+plus\b", sentence))
            matches = list(re.finditer(r"\b(\d{1,2})(?:\s*\+|\s*-\s*\d{1,2})?\s+years?\b", sentence))
            for index, found in enumerate(matches):
                # Avoid letting a later, different requirement supply the context
                # for an unrelated earlier number (company age, data retention).
                stop = matches[index + 1].start() if index + 1 < len(matches) else len(sentence)
                after = sentence[found.end():min(stop, found.end() + 150)]
                if not re.search(r"\b(?:experience|working|building|developing|development|designing|implementing)\b", after):
                    continue
                if not re.search(r"\b(?:data\s+engineer(?:ing)?|software\s+engineer(?:ing)?|software\s+development|data\s+pipelines?|data\s+platforms?|etl|elt|professional|industry|commercial|production)\b", after):
                    continue
                years_in_sentence.append(int(found.group(1)))
            if years_in_sentence:
                # "3 years X OR 7 years Y" is not a blanket seven-year minimum.
                alternative = len(years_in_sentence) > 1 and bool(re.search(r"\bor\b", sentence[matches[0].end():matches[-1].start()]))
                minimum = min(years_in_sentence) if alternative else max(years_in_sentence)
                (optional if preferred else required).append(minimum)
    return required, optional


def match_job(job: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """Rank a normalized posting and report constraints with human-readable evidence.

    ``eligible`` means the available posting text passes the search constraints;
    it never constitutes approval to submit an application. The caller must also
    enforce ``requires_review`` and resolve application questions.
    """
    title = _normalized(job.get("title"))
    description = _normalized(job.get("description"))
    text = f"{title}. {description}"
    reasons: list[str] = []
    exclusions: list[str] = []
    employment_type = _employment(job, text)
    location_status = _location(job)
    sponsorship, sponsor_reason = _sponsorship(text)
    required_years, optional_years = _experience_requirements(job)
    demonstrated_years = _industry_years(profile)
    reported_years = _reported_data_years(profile)
    experience_review = bool(required_years and max(required_years) >= 5 and max(required_years) > demonstrated_years)
    experience_gap = max(0.0, max(required_years) - demonstrated_years) if required_years else 0.0
    preferences = profile.get("preferences") or {}
    unknown_employment_allowed = bool(
        employment_type == "unknown"
        and preferences.get("allow_unknown_employment_type") is True
    )
    raw_stretch_limit = preferences.get("max_experience_stretch_years", 0)
    try:
        stretch_limit = float(raw_stretch_limit) if not isinstance(raw_stretch_limit, bool) else 0.0
    except (TypeError, ValueError, OverflowError):
        stretch_limit = 0.0
    if not math.isfinite(stretch_limit) or stretch_limit < 0:
        stretch_limit = 0.0
    experience_stretch_allowed = bool(
        experience_review and reported_years is not None
        and preferences.get("allow_experience_stretch") is True
        and 0 < experience_gap <= stretch_limit
    )
    reasons.append(sponsor_reason)

    if employment_type not in {"full-time", "unknown"}:
        exclusions.append(f"Employment type is {employment_type}; seeking full-time employee roles.")
    elif employment_type == "unknown":
        if unknown_employment_allowed:
            reasons.append("Posting does not specify employment type; retained under the configured preference allowing unspecified employment type.")
        else:
            exclusions.append("Full-time employment has not been confirmed; needs review.")
    else:
        reasons.append("Full-time employment is stated.")
        salary_evidence = _ROLE_FULLTIME_SALARY.search(text)
        if salary_evidence:
            reasons.append("Role-specific full-time evidence: " + salary_evidence.group(0) + ".")
    if location_status == "non-us":
        exclusions.append("Posting location is outside the United States.")
    elif location_status == "unknown":
        reasons.append("US work location is not confirmed; needs review.")
    else:
        reasons.append("US work location is stated or recognizable.")
        if job.get("office_locations") and _location({"location": job.get("location"), "country": job.get("country")}) == "unknown":
            reasons.append("US location evidence from official job-board offices: " + clean_text(job["office_locations"]) + ".")
    if sponsorship == "restricted":
        exclusions.append(sponsor_reason)

    score = 8
    if re.search(r"\b(?:data|analytics)\s+engineer\b|\bengineer(?:\s+ii|\s+iii)?\s*[,\-:]?\s+data\b", title):
        score += 30
        reasons.append("Title matches data/analytics engineering.")
    elif re.search(r"\b(?:data platform|data infrastructure|cloud data|etl|big data)\b", title):
        score += 27
        reasons.append("Title matches data platform or pipeline engineering.")
    elif re.search(r"\b(?:platform|cloud|backend|back-end|software)\s+engineer\b", title):
        score += 15
        reasons.append("Title overlaps cloud/application engineering experience.")
    elif re.search(r"\b(?:data scientist|machine learning engineer|research engineer)\b", title):
        score += 10
        reasons.append("Title overlaps healthcare/research experience.")

    evidence = _profile_text(profile)
    matched = []
    skill_points = 0
    for label, pattern, points in _SKILL_GROUPS:
        if re.search(pattern, text) and re.search(pattern, evidence):
            matched.append(label)
            skill_points += points
    score += min(skill_points, 55)
    if matched:
        reasons.append("Resume overlap: " + ", ".join(matched) + ".")
    if re.search(r"\b(?:principal|staff|director|head of|vice president|vp|distinguished)\b", title):
        score -= 20
        reasons.append("Seniority may exceed the demonstrated industry experience; review requirements.")
    elif re.search(r"\b(?:senior|sr\.?)\b", title):
        if demonstrated_years >= 5 and re.search(r"\b(?:data|analytics)\s+engineer(?:ing)?\b|\b(?:data platform|data infrastructure|cloud data|etl|big data)\b", title):
            reasons.append("Senior data-engineering title is consistent with the supplied experience floor; verify role-specific requirements.")
        else:
            score -= 6
            reasons.append("Senior title: verify the required industry experience.")
    if re.search(r"\b(?:manager|management)\b", title):
        score -= 15
        reasons.append("Management experience is not established in the supplied resume.")
    years = [int(x) for x in re.findall(r"\b(\d{1,2})\s*\+?\s+years?(?:\s+of)?\s+(?:professional|industry|commercial|production)\s+experience", description)]
    if years and min(years) >= 5 and min(years) > demonstrated_years:
        score -= min(18, (min(years) - 3) * 3)
        reasons.append(f"Posting asks for {min(years)}+ years of industry experience, exceeding the supplied experience floor; review equivalency.")

    if experience_review:
        evidence_description = (f"the applicant confirms at least {demonstrated_years:.1f} years of data engineering experience"
                                if reported_years is not None else
                                f"supplied dated industry work demonstrates approximately {demonstrated_years:.1f} years")
        reasons.append(f"Experience needs review: posting asks for {max(required_years)}+ years of data/software engineering or industry experience; {evidence_description}. Verify whether the employer accepts transferable experience or education equivalency.")
        if experience_stretch_allowed:
            reasons.append(f"Configured experience stretch: the {experience_gap:.1f}-year gap is within the configured {stretch_limit:.1f}-year limit. Apply using the confirmed {demonstrated_years:.1f}+ years; this does not establish that the advertised experience requirement is met.")
    elif optional_years and max(optional_years) >= 5:
        reasons.append(f"Posting prefers {max(optional_years)}+ years of relevant experience; preference is not an exclusion.")
    requires_review = ((employment_type == "unknown" and not unknown_employment_allowed)
                       or location_status == "unknown"
                       or (experience_review and not experience_stretch_allowed))
    return {
        "eligible": not exclusions,
        "score": max(0, min(100, score)),
        "reasons": reasons,
        "exclusions": exclusions,
        "sponsorship": sponsorship,
        "employment_type": employment_type,
        "unknown_employment_allowed": unknown_employment_allowed,
        "location_status": location_status,
        "requires_review": requires_review,
        "experience_requires_review": experience_review,
        "experience_stretch_allowed": experience_stretch_allowed,
        "experience_gap_years": experience_gap,
        "data_engineering_years_used": demonstrated_years,
        "experience_years_source": "applicant-confirmed" if reported_years is not None else "dated-industry",
    }
