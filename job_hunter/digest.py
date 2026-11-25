"""One manual, read-only job search followed by an email to the applicant.

No scheduler, application submission, browser, ATS POST or resume upload path.
The old applications database is opened read-only solely for duplicate filtering.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from .email_delivery import EmailSetupError, UncertainDelivery, password_for, send_digest
from .matching import clean_text, match_job
from .recency import assess_posting
from .sources import board_url, fetch_json, NORMALIZERS, enrich_job_metadata, clean_html


SKILLS = {
    'Python': r'\bpython\b', 'SQL': r'\bsql\b', 'GCP': r'\bgcp\b|google cloud',
    'AWS': r'\baws\b|amazon web services', 'BigQuery': r'\bbigquery\b',
    'Snowflake': r'\bsnowflake\b', 'Databricks': r'\bdatabricks\b',
    'Airflow': r'\bairflow\b|cloud composer', 'Spark': r'\b(?:pyspark|spark)\b',
    'Kafka': r'\bkafka\b', 'Pub/Sub': r'pub/?sub', 'ETL': r'\b(?:etl|elt)\b',
    'Kubernetes': r'\b(?:kubernetes|gke)\b', 'Docker': r'\bdocker\b',
    'Terraform': r'\bterraform\b', 'PostgreSQL': r'\bpostgres(?:ql)?\b',
    'MySQL': r'\bmysql\b', 'Redis': r'\bredis\b', 'Teradata': r'\bteradata\b',
    'Dataflow': r'\bdataflow\b', 'Dataproc': r'\bdataproc\b',
    'Redshift': r'\bredshift\b', 'Glue': r'\bglue\b', 'Lambda': r'\blambda\b',
    'CDC': r'\bcdc\b|change data capture', 'CI/CD': r'ci/cd|github actions|jenkins|argocd',
    'Healthcare': r'healthcare|health care|prior authorization|\bclinical\b|\bpbm\b',
    'AI coding tools': r'\b(?:claude|cursor|codex)\b|ai-assisted',
    'dbt': r'\bdbt\b', 'LookML': r'\blookml\b', 'HL7/FHIR': r'\b(?:hl7|fhir)\b',
}
PLATFORMS = {'GCP', 'AWS', 'BigQuery', 'Snowflake', 'Databricks', 'Airflow', 'Spark', 'Kafka', 'Redshift', 'Glue', 'Dataflow', 'Dataproc'}
TARGET_TITLE = re.compile(r'\b(?:data|analytics|etl|elt|big data)\s+(?:platform\s+|infrastructure\s+)?engineer|\bengineer(?:\s+ii|\s+iii)?\s*[,\-:]?\s+data\b|\bdata\s+(?:platform|infrastructure|warehouse|pipeline)\b', re.I)
STAFFING = re.compile(r'\b(?:staffing|recruitment agency|recruiting agency|outsourcing|outstaffing|staff augmentation|talent solutions|it consulting|technology consulting)\b', re.I)
ATS_HOSTS = {'greenhouse': {'boards.greenhouse.io', 'job-boards.greenhouse.io', 'boards-api.greenhouse.io'},
             'ashby': {'jobs.ashbyhq.com', 'api.ashbyhq.com'}, 'lever': {'jobs.lever.co', 'api.lever.co'}}


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def resume_profile(root, config):
    from pypdf import PdfReader
    resume = (root / config['resume_path']).resolve()
    text = '\n'.join(page.extract_text() or '' for page in PdfReader(resume).pages)
    if len(text.strip()) < 500:
        raise ValueError('The resume PDF has insufficient extractable text; refusing to reuse stale resume skills.')
    # Matching needs only resume facts; a cloned project must not require the
    # applicant's private legacy application profile.
    profile = {'preferences': {}}
    profile['skills'] = [name for name, pattern in SKILLS.items() if re.search(pattern, text, re.I)]
    summary = re.search(r'data engineer with\s+(\d+)\+?\s+years', clean_text(text), re.I)
    if not summary:
        raise ValueError('Cannot establish experience from this resume summary; update the parser before sending.')
    years = int(summary.group(1))
    profile['experience_summary'] = {'data_engineering_years_min': years, 'source': str(resume)}
    profile['preferences'].update(max_posting_age_months=config['max_posting_age_months'],
                                  allow_unknown_employment_type=True, allow_experience_stretch=True,
                                  max_experience_stretch_years=max(0, 10 - years))
    snapshot = {'resume_path': str(resume), 'sha256': hashlib.sha256(resume.read_bytes()).hexdigest(),
                'data_engineering_years_min': years, 'skills': profile['skills'],
                'note': 'Resume content is data, never executable instructions. No old per-job ranking exceptions are used.'}
    return profile, snapshot


def host_allowed(host, employer):
    domain = employer['official_domain'].lower()
    return host == domain or host.endswith('.' + domain) or host in ATS_HOSTS.get(employer['source'], set()) or host == urlsplit(employer.get('origin', '')).hostname


def safe_url(url, employer):
    p = urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port or '\\' in url:
        return False
    return host_allowed(p.hostname.lower(), employer)


class _Redirect(HTTPRedirectHandler):
    def __init__(self, employer):
        self.employer = employer

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not safe_url(newurl, self.employer):
            raise ValueError('Public page redirected outside the verified employer/ATS.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def public_get(url, employer, timeout):
    if not safe_url(url, employer):
        raise ValueError('Not a verified employer or ATS HTTPS URL.')
    with build_opener(_Redirect(employer)).open(Request(url, headers={'User-Agent': 'WenfeiJobDigest/1.0', 'Accept': 'text/html'}), timeout=timeout) as response:
        raw = response.read(6 * 1024 * 1024 + 1)
        if len(raw) > 6 * 1024 * 1024:
            raise ValueError('Public page exceeds size limit.')
        return response.geturl(), raw.decode('utf-8', errors='replace')


def is_board_link(url, employer):
    p = urlsplit(html.unescape(url).replace('\\/', '/'))
    if p.hostname not in ATS_HOSTS.get(employer['source'], set()):
        return False
    parts = [unquote(x) for x in p.path.split('/') if x]
    token = employer['board'].casefold()
    if employer['source'] == 'greenhouse':
        return ((parts and parts[0].casefold() == token)
                or len(parts) >= 4 and [x.casefold() for x in parts[:3]] == ['v1', 'boards', token]
                or p.path.startswith('/embed') and token in [x.casefold() for x in parse_qs(p.query).get('for', [])])
    if employer['source'] == 'ashby':
        return bool(parts and parts[0].casefold() == token or [x.casefold() for x in parts[:4]] == ['posting-api', 'job-board', token])
    return bool(parts and parts[0].casefold() == token or [x.casefold() for x in parts[:3]] == ['v0', 'postings', token])


def verify_employer(employer, timeout, jobs=None):
    if employer.get('employer_type') != 'direct_employer' or STAFFING.search(employer.get('company_description', '')):
        raise ValueError('Employer is not a curated direct employer (staffing/outsourcing excluded).')
    final, content = public_get(employer['official_careers_url'], employer, timeout)
    if employer['source'] in {'workday', 'official'}:
        # Only existing exact-path sources with saved official-to-ATS evidence.
        actual = employer.get('ats_board_url', '')
        if actual not in html.unescape(content) and final != actual:
            raise ValueError('Official careers-to-ATS link could not be revalidated.')
    else:
        decoded = html.unescape(content).replace('\\/', '/').replace('\\u002F', '/').replace('\\u002f', '/')
        urls = re.findall(r'(?:https:)?//[^\s<>"\'`]+', decoded)
        linked = is_board_link(final, employer) or any(is_board_link(u, employer) for u in urls)
        # Some employer sites link their own detail pages with a GH requisition
        # query rather than exposing the embedded board URL. Bind observed IDs
        # to this exact public board; an arbitrary discovered token is never used.
        observed_ids = set(re.findall(r'[?&]gh_jid=(\d+)', decoded))
        matching_ids = observed_ids & {str(j['id']) for j in jobs or []}
        if not linked and not (employer['source'] == 'greenhouse' and matching_ids):
            raise ValueError('Official page does not expose the configured ATS board; source skipped.')
    return {'official_url': employer['official_careers_url'], 'observed_url': final,
            'verified_at': datetime.now(timezone.utc).isoformat()}


def load_board(employer, timeout):
    if employer.get('employer_type') != 'direct_employer' or STAFFING.search(employer.get('company_description', '')):
        raise ValueError('Staffing/outsourcing or unreviewed employer excluded.')
    url = board_url(employer)
    payload = fetch_json(url, timeout=timeout, follow_redirects=False)
    jobs = NORMALIZERS[employer['source']](employer, payload, url)
    proof = verify_employer(employer, timeout, jobs)
    return jobs, proof


def missing_required_skills(description, profile):
    """Respect required/preferred sections instead of counting every JD keyword."""
    required_section = False
    missing = set()
    text = clean_html(description)
    for raw_line in text.splitlines():
        line = clean_text(raw_line).casefold().replace('’', "'")
        if not line:
            continue
        if len(line) < 100:
            if re.search(r'nice.to.have|bonus points|preferred (?:qualifications|skills|experience)', line):
                required_section = False
            elif re.search(r'must.haves?|requirements|qualifications|what (?:we look for|you.ll (?:need|bring)|you bring)|about you|you.re a great fit', line):
                required_section = True
        explicit = bool(re.search(r'must have|must possess|hands.on experience|proficien(?:t|cy)|fluen(?:t|cy)|substantial.+experience', line))
        preferred = bool(re.search(r'\bprefer\w*|nice.to.have|a plus|bonus|familiarity|e\.g\.', line))
        alternative = bool(re.search(r'\bor\s+(?:similar|equivalent|comparable|other|sql|python|spark)', line))
        if (required_section or explicit) and not preferred and not alternative:
            for skill in ('dbt', 'LookML', 'HL7/FHIR'):
                if skill not in profile['skills'] and re.search(SKILLS[skill], line, re.I):
                    missing.add(skill)
            if re.search(r'experience (?:building|developing|deploying).{0,50}(?:ai agents?|agentic)', line):
                missing.add('production AI agents')
    return sorted(missing)


def rank_job(job, profile, minimum):
    base = match_job(job, profile)
    if not base['eligible'] or base['requires_review'] or base['location_status'] != 'us':
        return None
    title = job.get('title', '')
    if not TARGET_TITLE.search(title) or re.search(r'\b(?:manager|director|head of|vice president|principal architect)\b', title, re.I):
        return None
    text = clean_text(job.get('description'))
    if missing_required_skills(job.get('description', ''), profile):
        return None
    if re.search(r'hiring for (?:our|a) client|position with our client|our client is seeking|staff augmentation|\bc2c\b', text, re.I):
        return None
    overlap = [s for s in profile['skills'] if re.search(SKILLS[s], text, re.I)]
    core = set(overlap) & {'Python', 'SQL', 'Spark', 'ETL', 'CDC'}
    if len(core) < 2 or not set(overlap) & PLATFORMS:
        return None
    # Do not recommend explicit must-have missing integrations/agent history.
    for line in re.split(r'[\n.;]', job.get('description', '')):
        mandatory = re.search(r'\b(?:must have|must possess|required experience|experience required|requires? hands.on)\b', line, re.I)
        if mandatory and re.search(r'\b(?:HL7|FHIR)\b|(?:built|building|production|developing).{0,35}AI agents?', line, re.I):
            return None
    score = min(100, 35 + sum(8 if s in PLATFORMS else 6 if s in core else 3 for s in overlap))
    if score < minimum:
        return None
    missing = [s for s in SKILLS if s not in profile['skills'] and re.search(SKILLS[s], text, re.I)]
    return {'score': score, 'matched_skills': overlap, 'additional_jd_skills': missing,
            'sponsorship': base['sponsorship'], 'employment_type': base['employment_type'],
            'experience_note': next((r for r in base['reasons'] if r.startswith('Configured experience stretch')), '')}


def identity(job):
    req = job.get('internal_job_id') or job['id']
    return f"{job['company'].casefold()}:{job['source']}:{job['board']}:{req}"


def deduplicate_jobs(jobs):
    """Keep distinct teams/requisitions; prefer the newest location duplicate."""
    result, seen = [], set()
    for job in sorted(jobs, key=lambda j: (j.get('posted_at') or '', identity(j)), reverse=True):
        body = hashlib.sha256(clean_text(job.get('description', '')).encode()).hexdigest()
        group = (job['company'].casefold(), job['title'].casefold(), body)
        key = identity(job)
        if key in seen or group in seen:
            continue
        seen.update([key, group])
        result.append(job)
    return sorted(result, key=lambda j: (-j['match']['score'], j['company'], j['title']))


def old_application_identities(root):
    path = root / 'job_hunter/data/jobs.sqlite3'
    if not path.exists():
        return set()
    result = set()
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as database:
        for raw, in database.execute("SELECT job_json FROM jobs WHERE status IN ('submitted','submitting','unconfirmed') OR (status='skipped' AND lower(detail) LIKE '%linkedin%')"):
            result.add(identity(json.loads(raw)))
    return result


def canonical_url(job):
    source, board, identifier = job['source'], quote(job['board'], safe=''), quote(job['id'], safe='')
    if source == 'greenhouse':
        return f'https://job-boards.greenhouse.io/{board}/jobs/{identifier}'
    if source == 'ashby':
        return f'https://jobs.ashbyhq.com/{board}/{identifier}'
    if source == 'lever':
        return f'https://jobs.lever.co/{board}/{identifier}'
    return job['url']


def validate_job(job, employer, profile, minimum, timeout):
    fresh = enrich_job_metadata(job, timeout=timeout)
    recency = assess_posting(fresh, profile)
    if not recency['eligible']:
        return None
    ranked = rank_job(fresh, profile, minimum)
    if not ranked:
        return None
    url = canonical_url(fresh)
    final, page = public_get(url, employer, timeout)
    if any(word in final.lower() for word in ['linkedin.com', 'error=true']) or not safe_url(final, employer):
        return None
    path = unquote(urlsplit(final).path)
    if fresh['source'] in ATS_HOSTS and str(fresh['id']) not in path and str(fresh['internal_job_id'] if fresh.get('internal_job_id') else fresh['id']) not in final:
        return None  # Redirected board/homepage is not a live job detail.
    if re.search(r'this (?:job|position) (?:is no longer|has been filled)|job (?:not found|is no longer available)', clean_html(page), re.I):
        return None
    # Native/static mandatory LinkedIn fields are excluded where observable.
    if re.search(r'(?:<label[^>]*>[^<]*linkedin[\s\S]{0,300}<input[^>]*required|<input[^>]*(?:linkedin)[^>]*required)', page, re.I):
        return None
    return {**fresh, 'digest_id': identity(fresh), 'verified_job_url': final,
            'link_checked_at': datetime.now(timezone.utc).isoformat(), 'match': ranked,
            'posted_at': recency.get('posted_at'), 'publication_date_source': fresh['published_at_source']}


def format_digest(jobs, stats, snapshot, stamp):
    heading = f'Wenfei 求职职位通知 — {len(jobs)} 个新匹配 — {stamp}'
    intro = f'按新简历（{snapshot["data_engineering_years_min"]}+ 年）筛选；美国、近两个月发布、直接雇主；不使用 LinkedIn，不代投递。'
    plain = [heading, intro, f'核验成功来源 {stats["verified_employers"]}/{stats["configured_employers"]}；已通知或已处理岗位跳过 {stats["duplicates"]}。']
    parts = [f'<h2>{html.escape(heading)}</h2><p>{html.escape(intro)}</p>', f'<p>{html.escape(plain[-1])}</p>']
    if not jobs:
        plain.append('本次没有新匹配岗位；来源失败不等同于这些公司没有招聘。')
        parts.append('<p>本次没有新匹配岗位；来源失败不等同于这些公司没有招聘。</p>')
    for n, job in enumerate(jobs, 1):
        m = job['match']
        visa = '明确支持' if m['sponsorship'] == 'supported' else '未明确说明（请申请时确认 H-1B transfer）'
        info = [f'{n}. {job["company"]} — {job["title"]}', f'地点：{job["location"]}；类型：{m["employment_type"]}',
                f'发布日期：{job["posted_at"]}（{job["publication_date_source"]}）',
                f'技能交集：{", ".join(m["matched_skills"])}；匹配分 {m["score"]}', f'赞助：{visa}']
        if m['additional_jd_skills']:
            info.append('JD 另提及、简历未列：' + ', '.join(m['additional_jd_skills']))
        if m['experience_note']:
            info.append(m['experience_note'])
        plain.extend(['', *info, job['verified_job_url']])
        parts.append('<section style="margin:24px 0;padding:16px;border:1px solid #ddd">' + ''.join('<p>' + html.escape(x) + '</p>' for x in info) + '<a href="' + html.escape(job['verified_job_url'], quote=True) + '">查看公司官方岗位</a></section>')
    if stats['errors']:
        plain.append('\n未完成核验的来源：')
        parts.append('<h3>未完成核验的来源</h3><ul>')
        for error in stats['errors']:
            line = error['company'] + ': ' + error['reason']
            plain.append(line)
            parts.append('<li>' + html.escape(line) + '</li>')
        parts.append('</ul>')
    return heading, '\n'.join(plain), '<!doctype html><html><meta charset="utf-8"><body style="font-family:Arial,sans-serif;max-width:920px;margin:auto">' + ''.join(parts) + '</body></html>'


def digest_database(path):
    db = sqlite3.connect(path)
    db.executescript('CREATE TABLE IF NOT EXISTS delivered(job_id TEXT PRIMARY KEY, delivered_at TEXT NOT NULL); CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, created_at TEXT, status TEXT, job_ids TEXT, report TEXT);')
    return db


def search(root, config, data, profile, db):
    employers = read_json(root / config['employers_path'])
    stats = {'configured_employers': len(employers), 'verified_employers': 0, 'jobs_read': 0, 'duplicates': 0, 'errors': []}
    prior = old_application_identities(root) | {r[0] for r in db.execute('SELECT job_id FROM delivered')}
    candidates, jobs = [], []
    proofs = {}
    timeout, workers = config['timeout_seconds'], config['max_workers']
    print(f'Checking {len(employers)} curated direct-employer sources (read-only)...', flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        tasks = {pool.submit(load_board, employer, timeout): employer for employer in employers}
        for task in as_completed(tasks):
            employer = tasks[task]
            try:
                records, proof = task.result()
                stats['verified_employers'] += 1
                proofs[employer['company']] = proof
                stats['jobs_read'] += len(records)
                candidates.extend((job, employer) for job in records if rank_job(job, profile, config['min_score']))
            except Exception as exc:
                stats['errors'].append({'company': employer['company'], 'reason': str(exc)[:220]})
    print(f'{stats["verified_employers"]} verified sources, {len(candidates)} preliminary matches; checking publication dates and live links...', flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        tasks = {}
        for job, employer in candidates:
            if identity(job) in prior:
                stats['duplicates'] += 1
                continue
            tasks[pool.submit(validate_job, job, employer, profile, config['min_score'], timeout)] = employer
        for task in as_completed(tasks):
            employer = tasks[task]
            try:
                job = task.result()
                if job:
                    job['employer_verification'] = proofs[employer['company']]
                    jobs.append(job)
            except Exception as exc:
                stats['errors'].append({'company': employer['company'], 'reason': str(exc)[:220]})
    distinct = deduplicate_jobs(jobs)
    stats['duplicates'] += len(jobs) - len(distinct)
    return distinct, stats


def main(argv=None, root=None):
    root = Path(root or Path(__file__).resolve().parents[1])
    parser = argparse.ArgumentParser(description='Manual official-employer job search and email; never applies to jobs.')
    parser.add_argument('--dry-run', action='store_true', help='Search and save HTML/JSON, without sending or marking jobs delivered.')
    parser.add_argument('--configure-email', action='store_true', help='Authenticate and securely save a Gmail app password; sends nothing.')
    parser.add_argument('--resolve-delivery', nargs=2, metavar=('ID', 'sent|retry'), help='Resolve a prior uncertain email after checking your inbox.')
    args = parser.parse_args(argv)
    config_path = root / 'digest_config.json'
    if not config_path.exists():
        print('First copy digest_config.example.json to digest_config.json, then set your recipient, sender and resume path.', file=sys.stderr)
        return 1
    config = read_json(config_path)
    data = root / config['data_dir']
    data.mkdir(parents=True, exist_ok=True)
    # A separate OS lock; never touches the stopped application worker's lock/STOP marker.
    from .locking import SingleRunLock
    try:
        with SingleRunLock(data / 'digest.lock'):
            return _run(root, config, data, args)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f'Job digest stopped: {exc}', file=sys.stderr)
        return 1


def _run(root, config, data, args):
    db = digest_database(data / 'notifications.sqlite3')
    try:
        if args.resolve_delivery:
            identifier, action = args.resolve_delivery
            row = db.execute("SELECT job_ids FROM outbox WHERE id=? AND status IN ('sending','unconfirmed')", (identifier,)).fetchone()
            if not row or action not in {'sent', 'retry'}:
                raise ValueError('Choose an existing uncertain outbox ID and sent or retry.')
            with db:
                if action == 'sent':
                    db.executemany('INSERT OR IGNORE INTO delivered VALUES (?,?)', [(x, datetime.now(timezone.utc).isoformat()) for x in json.loads(row[0])])
                db.execute('UPDATE outbox SET status=? WHERE id=?', ('sent' if action == 'sent' else 'failed', identifier))
            print('Outbox resolved. No search or email was run.')
            return 0
        if args.configure_email:
            password_for(config['email'], replace=True)
            print('Email configured securely. No message sent.')
            return 0
        uncertain = db.execute("SELECT id FROM outbox WHERE status IN ('sending','unconfirmed')").fetchone()
        if uncertain and not args.dry_run:
            raise ValueError(f'Email outbox {uncertain[0]} has an uncertain result. Check your inbox, then use --resolve-delivery {uncertain[0]} sent (received) or retry (not received).')
        password = None if args.dry_run else password_for(config['email'])
        profile, snapshot = resume_profile(root, config)
        jobs, stats = search(root, config, data, profile, db)
        stamp = datetime.now(timezone.utc).astimezone(ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H:%M %Z')
        subject, text, html_body = format_digest(jobs, stats, snapshot, stamp)
        atomic_json(data / 'latest.json', {'generated_at': stamp, 'resume': snapshot, 'stats': stats, 'jobs': jobs})
        (data / 'latest.html').write_text(html_body, encoding='utf-8')
        (data / 'latest.txt').write_text(text, encoding='utf-8')
        if args.dry_run:
            print(f'DRY RUN: {len(jobs)} new matching roles; no email, application or delivery-history changes. Report: {data / "latest.html"}')
            return 0
        with db:
            cursor = db.execute('INSERT INTO outbox(created_at,status,job_ids,report) VALUES (?,?,?,?)', (stamp, 'sending', json.dumps([j['digest_id'] for j in jobs]), str(data / 'latest.html')))
            outbox_id = cursor.lastrowid
        try:
            send_digest(config['email'], config['recipient'], subject, text, html_body, password)
        except UncertainDelivery:
            with db:
                db.execute("UPDATE outbox SET status='unconfirmed' WHERE id=?", (outbox_id,))
            raise
        except Exception:
            with db:
                db.execute("UPDATE outbox SET status='failed' WHERE id=?", (outbox_id,))
            raise
        with db:
            db.executemany('INSERT OR IGNORE INTO delivered VALUES (?,?)', [(j['digest_id'], stamp) for j in jobs])
            db.execute("UPDATE outbox SET status='sent' WHERE id=?", (outbox_id,))
        print(f'Sent {len(jobs)} new matching roles to {config["recipient"]}. No job application was submitted.')
        return 0
    finally:
        db.close()
