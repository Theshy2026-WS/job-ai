"""Discover public job sources from a broader, reviewed employer directory.

Search engines supply leads only. Every accepted board must be linked by the
real employer website; every direct job must publish official JobPosting data.
No LinkedIn, guessed board tokens, credentials or applicant data are used.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from html import unescape
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from .sources import _JSONLDScripts, _job_postings, _record


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.current = [], None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a':
            self.current = {'url': attrs.get('href', ''), 'text': ''}
        elif tag in {'iframe', 'script'} and attrs.get('src'):
            self.links.append({'url': attrs['src'], 'text': ''})

    def handle_data(self, text):
        if self.current:
            self.current['text'] += text

    def handle_endtag(self, tag):
        if tag == 'a' and self.current:
            self.links.append(self.current); self.current = None


def extract_links(content, base):
    parser = Links(); parser.feed(content)
    links = [{**x, 'url': urljoin(base, x['url'])} for x in parser.links if x['url']]
    decoded = unescape(content).replace('\\/', '/').replace('\\u002F', '/').replace('\\u002f', '/')
    links += [{'url': urljoin(base, x), 'text': ''} for x in re.findall(r'(?:https:)?//[^\s<>"\'`]+', decoded)]
    return links


def board_from_link(url, employer):
    """Extract a token only from a link actually observed on an official page."""
    try:
        p = urlsplit(unescape(url))
    except ValueError:
        return None
    parts = [unquote(x) for x in p.path.split('/') if x]
    host = (p.hostname or '').lower(); source = token = None
    if host in {'boards.greenhouse.io', 'job-boards.greenhouse.io', 'boards-api.greenhouse.io'}:
        source = 'greenhouse'
        if p.path.startswith('/embed'):
            token = (parse_qs(p.query).get('for') or [None])[0]
        elif parts[:2] == ['v1', 'boards'] and len(parts) >= 3:
            token = parts[2]
        elif parts:
            token = parts[0]
    elif host in {'jobs.ashbyhq.com', 'api.ashbyhq.com'}:
        source = 'ashby'
        if parts[:2] == ['posting-api', 'job-board'] and len(parts) >= 3:
            token = parts[2]
        elif parts:
            token = parts[0]
    elif host in {'jobs.lever.co', 'api.lever.co'}:
        source = 'lever'
        if parts[:2] == ['v0', 'postings'] and len(parts) >= 3:
            token = parts[2]
        elif parts:
            token = parts[0]
    if not source or not token or not re.fullmatch(r'[A-Za-z0-9_.-]+', token):
        return None
    return {**employer, 'source': source, 'board': token, 'discovered_board_link': url}


def official_host(url, employer):
    try:
        p = urlsplit(url)
    except ValueError:
        return False
    host = (p.hostname or '').lower(); domain = employer['official_domain'].lower()
    return p.scheme == 'https' and (host == domain or host.endswith('.' + domain))


def search_leads(queries, employers, timeout):
    """Bing RSS discovery only; an indexed listing is never publication proof."""
    leads, errors = [], []
    for query in queries:
        url = 'https://www.bing.com/search?format=rss&q=' + quote(query + ' -site:linkedin.com')
        try:
            with urlopen(Request(url, headers={'User-Agent': 'ManualJobDigest/1.0'}), timeout=timeout) as r:
                if urlsplit(r.geturl()).hostname not in {'www.bing.com', 'bing.com'}:
                    raise ValueError('Search engine redirected unexpectedly')
                raw = r.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError('Search response exceeds size limit')
            for item in ET.fromstring(raw).findall('.//item'):
                link = item.findtext('link', '')
                if 'linkedin.com' in link.lower():
                    continue
                for employer in employers:
                    if official_host(link, employer):
                        leads.append((link, employer)); break
        except Exception as exc:
            errors.append({'company': 'Web discovery', 'reason': str(exc)[:220]})
    return leads, errors


def official_job(content, url, employer):
    """Read JobPosting on the actual employer domain, never third-party snippets."""
    parser = _JSONLDScripts(); parser.feed(content); postings = []
    for raw in parser.scripts:
        try:
            postings.extend(_job_postings(json.loads(raw)))
        except (ValueError, TypeError, RecursionError):
            pass
    if len(postings) != 1:
        return None
    posting = postings[0]
    declared = posting.get('url')
    if declared and urlsplit(declared).path.rstrip('/') != urlsplit(url).path.rstrip('/'):
        return None
    address = posting.get('jobLocation') or []
    address = address if isinstance(address, list) else [address]
    labels, countries = [], []
    for place in address:
        a = place.get('address') or {} if isinstance(place, dict) else {}
        country = a.get('addressCountry', '')
        country = country.get('name', '') if isinstance(country, dict) else country
        country = 'US' if str(country).upper() in {'US', 'USA', 'UNITED STATES', 'UNITED STATES OF AMERICA'} else str(country)
        countries.append(country)
        labels.append(', '.join(str(a[k]) for k in ['addressLocality', 'addressRegion'] if a.get(k)) + ', ' + country)
    requirements = posting.get('applicantLocationRequirements') or []
    for area in requirements if isinstance(requirements, list) else [requirements]:
        if isinstance(area, dict) and area.get('name') in {'US', 'USA', 'United States'}:
            countries.append('US'); labels.append('Remote, United States')
    identifier = posting.get('identifier')
    identifier = identifier.get('value') if isinstance(identifier, dict) else identifier
    identifier = str(identifier) if identifier else hashlib.sha256(url.encode()).hexdigest()[:20]
    kind = posting.get('employmentType', 'unknown')
    if isinstance(kind, list):
        kind = kind[0] if len(kind) == 1 else 'unknown'
    kind = {'FULL_TIME': 'FullTime', 'PART_TIME': 'Part-time', 'CONTRACTOR': 'Contract', 'TEMPORARY': 'Temporary', 'INTERN': 'Internship'}.get(kind, kind)
    descriptor = {**employer, 'source': 'employer_site', 'board': employer['official_domain']}
    if not posting.get('description') or not posting.get('title'):
        return None
    return _record(descriptor, posting, id=identifier, internal_job_id=identifier,
                   title=posting['title'], description=posting['description'], location='; '.join(labels),
                   country='US' if 'US' in countries else '', employment_type=kind,
                   url=url, apply_url=url, source_url=url, published_at=posting.get('datePosted', ''),
                   published_at_source='official JobPosting.datePosted' if posting.get('datePosted') else '',
                   published_at_evidence=url)


def discover_company(employer, timeout, public_get, target_title):
    initial = employer['official_careers_url']
    pending, seen = [initial], set()
    boards, details, direct, errors = {}, {}, [], []
    while pending and len(seen) < 3:
        url = pending.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            final, content = public_get(url, employer, timeout)
            if not official_host(final, employer):
                # An official career URL can redirect straight to its ATS.
                b = board_from_link(final, employer)
                if b:
                    b['official_careers_url'] = url
                    boards[(b['source'], b['board'])] = b
                continue
            for link in extract_links(content, final):
                b = board_from_link(link['url'], employer)
                if b:
                    b['official_careers_url'] = final
                    b['discovery_parent_careers_url'] = initial
                    boards[(b['source'], b['board'])] = b
                elif official_host(link['url'], employer):
                    if target_title.search(link['text']):
                        details[link['url']] = employer
                    elif len(pending) < 2 and re.search(r'view.{0,15}(?:jobs|roles|openings)|open (?:roles|jobs|positions)|see.{0,12}jobs|explore roles', link['text'], re.I):
                        pending.append(link['url'])
        except Exception as exc:
            errors.append({'company': employer['company'], 'reason': 'Discovery: ' + str(exc)[:200]})
    for url in details:
        try:
            final, content = public_get(url, employer, timeout)
            if official_host(final, employer):
                record = official_job(content, final, employer)
                if record:
                    direct.append((record, employer))
        except Exception as exc:
            errors.append({'company': employer['company'], 'reason': 'Official job detail: ' + str(exc)[:180]})
    if not boards and not direct and not errors:
        errors.append({'company': employer['company'], 'reason': 'No supported public ATS link or official JobPosting detail was observable; coverage incomplete.'})
    return list(boards.values()), direct, errors


def discover(root, config, existing, public_get, target_title):
    settings = config.get('discovery', {})
    if not settings.get('enabled', False):
        return existing, [], [], {'directory_companies': 0, 'discovered_boards': 0, 'official_jobs': 0}
    directory_path = root / settings.get('directory_path', 'employer_directory.json')
    directory = json.loads(directory_path.read_text(encoding='utf-8-sig')) if directory_path.exists() else []
    boards = {(e['source'], e['board']): e for e in existing}
    direct, errors = [], []
    with ThreadPoolExecutor(max_workers=config['max_workers']) as pool:
        tasks = {pool.submit(discover_company, e, config['timeout_seconds'], public_get, target_title): e for e in directory}
        for task in as_completed(tasks):
            try:
                found, jobs, failures = task.result()
                for e in found:
                    boards[(e['source'], e['board'])] = e
                direct.extend(jobs); errors.extend(failures)
            except Exception as exc:
                errors.append({'company': tasks[task]['company'], 'reason': str(exc)[:220]})
    leads, failures = search_leads(settings.get('search_queries', []), directory + existing, config['timeout_seconds'])
    errors.extend(failures)
    for url, employer in leads:
        try:
            final, content = public_get(url, employer, config['timeout_seconds'])
            if official_host(final, employer):
                record = official_job(content, final, employer)
                if record:
                    direct.append((record, employer))
        except Exception as exc:
            errors.append({'company': employer['company'], 'reason': 'Search lead: ' + str(exc)[:180]})
    return list(boards.values()), direct, errors, {'directory_companies': len(directory),
            'discovered_boards': len(set(boards) - {(e['source'], e['board']) for e in existing}),
            'official_jobs': len(direct)}
