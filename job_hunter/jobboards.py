"""Public job-board discovery. Search snippets never establish a job/date.

Only live detail pages with explicit JobPosting data and a reviewed direct
employer are accepted. No login, application, CAPTCHA bypass or scraping proxy.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
from urllib.parse import urlsplit, parse_qs, quote
from urllib.request import Request, urlopen, HTTPRedirectHandler, build_opener
import xml.etree.ElementTree as ET

from .discovery import official_job
from .sources import _JSONLDScripts, _job_postings

PROVIDERS = {
    'Indeed': ('indeed.com', 'site:indeed.com/viewjob "data engineer" "United States"'),
    'Dice': ('dice.com', 'site:dice.com/job-detail "data engineer"'),
    'Built In': ('builtin.com', 'site:builtin.com/job "data engineer"'),
}


def provider_for(url):
    try:
        p = urlsplit(url)
        if p.scheme != 'https' or p.username or p.password or p.port:
            return None
        host = p.hostname or ''
        if host in {'www.indeed.com', 'indeed.com'} and p.path == '/viewjob' and parse_qs(p.query).get('jk'):
            return 'Indeed'
        if host in {'www.dice.com', 'dice.com'} and re.fullmatch(r'/job-detail/[a-zA-Z0-9-]+/?', p.path):
            return 'Dice'
        if host in {'www.builtin.com', 'builtin.com'} and p.path.startswith('/job/'):
            return 'Built In'
    except ValueError:
        pass
    return None


def get_page(url, timeout):
    source = provider_for(url)
    if not source:
        raise ValueError('Not an allowed job-board detail URL')
    class DetailRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if provider_for(newurl) != source:
                raise ValueError('Job board redirected away from its detail page')
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    with build_opener(DetailRedirect()).open(Request(url, headers={'User-Agent': 'ManualJobDigest/1.0', 'Accept': 'text/html'}), timeout=timeout) as response:
        final = response.geturl()
        if provider_for(final) != source:
            raise ValueError('Job board redirected away from its detail page')
        raw = response.read(6 * 1024 * 1024 + 1)
        if len(raw) > 6 * 1024 * 1024:
            raise ValueError('Job detail exceeds size limit')
        return final, raw.decode('utf-8', errors='replace')


def normalize_name(name):
    return re.sub(r'[^a-z0-9]', '', name.casefold())


def parse_detail(page, url, employers):
    source = provider_for(url)
    if not source:
        return None
    parser = _JSONLDScripts(); parser.feed(page)
    postings = []
    for raw in parser.scripts:
        try:
            postings.extend(_job_postings(json.loads(raw)))
        except (ValueError, TypeError, RecursionError):
            continue
    if len(postings) != 1:
        return None
    posting = postings[0]
    organization = posting.get('hiringOrganization') or {}
    if not isinstance(organization, dict) or not organization.get('name'):
        return None
    name = normalize_name(organization['name'])
    employer = next((e for e in employers if name in {normalize_name(n) for n in [e['company'], *e.get('job_board_aliases', [])]}
                     and e.get('employer_type') == 'direct_employer'
                     and e.get('company_priority') in {'established', 'industry_known'}), None)
    if not employer:
        return None  # Never treat the client's brand mentioned in an agency JD as the employer.
    # A source detail page can legitimately identify the company URL in schema.
    # Parse the exact schema, but retain the discovered source link for the user.
    schema = dict(posting); schema['url'] = url
    record = official_job('<script type="application/ld+json">' + json.dumps(schema).replace('</', '<\\/') + '</script>', url, employer)
    if not record or not record.get('published_at'):
        return None
    record.update(source='job_board', board=PROVIDERS[source][0], discovery_source=source,
                  source_listing_url=url, published_at_source=source + ' JobPosting.datePosted',
                  employer_listing_name=organization['name'])
    return record, employer


def rss_results(query, timeout):
    url = 'https://www.bing.com/search?format=rss&q=' + quote(query + ' -site:linkedin.com')
    with urlopen(Request(url, headers={'User-Agent': 'ManualJobDigest/1.0'}), timeout=timeout) as response:
        if urlsplit(response.geturl()).hostname not in {'bing.com', 'www.bing.com'}:
            raise ValueError('Unexpected search redirect')
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError('Search response exceeds size limit')
    return [x.findtext('link', '') for x in ET.fromstring(raw).findall('.//item')]


def search_results(query, timeout):
    key = os.environ.get('BRAVE_SEARCH_API_KEY')
    if not key:
        return rss_results(query, timeout)
    # The user supplies the optional search-service credential locally. No
    # private resume text/contact data are included in these generic queries.
    url = 'https://api.search.brave.com/res/v1/web/search?q=' + quote(query + ' -site:linkedin.com') + '&country=US&count=20&freshness=pm'
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise ValueError('Search API redirect refused')
    with build_opener(NoRedirect()).open(Request(url, headers={'X-Subscription-Token': key, 'Accept': 'application/json'}), timeout=timeout) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError('Search response exceeds size limit')
    return [row.get('url', '') for row in json.loads(raw).get('web', {}).get('results', [])]


def discover_jobboards(employers, timeout, workers, settings):
    if not settings.get('enabled', True):
        return [], [], {}
    urls, errors, counts = set(), [], {}
    for source, (_, query) in PROVIDERS.items():
        counts[source] = {'leads': 0, 'read': 0, 'verified_details': 0}
        try:
            for url in search_results(query, timeout):
                if provider_for(url) == source:
                    urls.add(url); counts[source]['leads'] += 1
        except Exception as exc:
            errors.append({'company': source, 'reason': 'Search unavailable: ' + str(exc)[:180]})
    jobs = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        tasks = {pool.submit(get_page, url, timeout): url for url in urls}
        for task in as_completed(tasks):
            url = tasks[task]; source = provider_for(url)
            try:
                final, page = task.result(); counts[source]['read'] += 1
                result = parse_detail(page, final, employers)
                if result:
                    jobs.append(result); counts[source]['verified_details'] += 1
            except Exception as exc:
                errors.append({'company': source, 'reason': 'Detail unavailable (not bypassed): ' + str(exc)[:180]})
    for source, count in counts.items():
        if not count['verified_details']:
            errors.append({'company': source, 'reason': 'No verifiable detail returned this run; search/index/access coverage is incomplete, not evidence of no openings.'})
    if not urls and not os.environ.get('BRAVE_SEARCH_API_KEY'):
        errors.append({'company': 'Job-board search', 'reason': 'Public RSS returned no supported detail links. Optional BRAVE_SEARCH_API_KEY enables the documented search API; even then site access and actual publication dates still require validation.'})
    return jobs, errors, counts
