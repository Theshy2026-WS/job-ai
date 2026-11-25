"""Public ATS schema regression tests; no network requests or applications."""
import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from job_hunter.sources import (
    SourceError, board_url, clean_html, fetch_json, fetch_jobs, enrich_employment, enrich_job_metadata, MAX_HTML_BYTES,
    normalize_greenhouse, normalize_lever, normalize_ashby,
)


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.gh = {"source": "greenhouse", "board": "example", "company": "Example"}
        self.lever = {**self.gh, "source": "lever"}
        self.ashby = {**self.gh, "source": "ashby"}

    def test_nested_greenhouse_html_is_readable(self):
        self.assertEqual(clean_html("&amp;lt;p&amp;gt;Need SQL&amp;lt;/p&amp;gt;&lt;script&gt;bad()&lt;/script&gt;<p>GCP</p>"), "Need SQL\nGCP")

    def test_unknown_employment_not_assumed_fulltime(self):
        payload = {"jobs": [{"id": 12, "title": "Data Engineer", "location": {"name": "New York"},
                             "content": "<p>BigQuery</p>", "internal_job_id": 2,
                             "offices": [{"name": "NY", "location": "New York, NY, United States"}],
                             "departments": [{"name": "Data"}]}]}
        job = normalize_greenhouse(self.gh, payload, "https://example.com")[0]
        self.assertEqual(job["employment_type"], "unknown")
        self.assertEqual(job["id"], "12")
        self.assertEqual(job["office_locations"], ["New York, NY, United States"])
        self.assertEqual(job["departments"], ["Data"])
        self.assertEqual(job["description"], "BigQuery")

    def test_greenhouse_explicit_metadata_and_prospect_exclusion(self):
        payload = {"jobs": [{"id": 12, "title": "Engineer", "metadata": [{"name": "Employment Type", "value": "Full-time"}]},
                            {"id": 13, "title": "Join our talent pool", "internal_job_id": None}]}
        jobs = normalize_greenhouse(self.gh, payload, "https://example.com")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["employment_type"], "Full-time")

    def test_greenhouse_publication_is_first_published_never_updated_or_created(self):
        raw = {'id':12, 'title':'Data Engineer', 'first_published':'2026-08-01T12:00:00Z',
               'updated_at':'2026-11-05T12:00:00Z', 'created_at':'2026-07-01T12:00:00Z'}
        job = normalize_greenhouse(self.gh, {'jobs':[raw]}, 'https://boards-api.greenhouse.io/example')[0]
        self.assertEqual(job['published_at'], raw['first_published'])
        self.assertEqual(job['published_at_source'], 'greenhouse.first_published')
        self.assertEqual(job['updated_at'], raw['updated_at'])
        del raw['first_published']
        job = normalize_greenhouse(self.gh, {'jobs':[raw]}, 'https://boards-api.greenhouse.io/example')[0]
        self.assertEqual(job['published_at'], '')
        self.assertEqual(job['published_at_source'], '')

    def test_greenhouse_explicit_time_type_metadata_confirms_type_without_benefit_inference(self):
        raw = {'id':12,'title':'Data Engineer','metadata':[{'name':'Time Type','value':'Full time'}]}
        url = 'https://boards-api.greenhouse.io/v1/boards/example/jobs?content=true'
        job = normalize_greenhouse(self.gh, {'jobs':[raw]}, url)[0]
        self.assertEqual(job['employment_type'], 'Full time')
        self.assertEqual(job['employment_type_source'], 'greenhouse.metadata.Time Type')
        self.assertEqual(job['employment_type_evidence'], url)
        raw['metadata'][0]['value'] = 'Part time'
        self.assertEqual(normalize_greenhouse(self.gh, {'jobs':[raw]}, url)[0]['employment_type'], 'Part time')

    def test_greenhouse_salary_and_company_full_time_benefits_do_not_confirm_type(self):
        raw = {'id':12,'title':'Data Engineer','metadata':[{'name':'WD - Pay Rate Type','value':'Salary'}],
               'content':'What we offer for our full-time, regular employees: comprehensive benefits.'}
        job = normalize_greenhouse(self.gh, {'jobs':[raw]}, 'https://boards-api.greenhouse.io/example')[0]
        self.assertEqual(job['employment_type'], 'unknown')
        self.assertEqual(job['employment_type_source'], '')

    def test_lever_creation_timestamp_is_not_a_publication_date(self):
        raw = {'id':'abc','text':'Data Engineer','createdAt':1793295251143,'updatedAt':1793899999999}
        job = normalize_lever(self.lever, [raw], 'https://api.lever.co/example')[0]
        self.assertEqual(job['created_at'], raw['createdAt'])
        self.assertEqual(job['updated_at'], raw['updatedAt'])
        self.assertEqual(job['published_at'], '')
        self.assertEqual(job['published_at_source'], '')

    def test_ashby_publication_source_explicitly_identifies_last_publication(self):
        raw = {'title':'Data Engineer','jobUrl':'https://jobs.ashbyhq.com/example/id',
               'publishedAt':'2026-10-20T15:30:00Z', 'updatedAt':'2026-11-05T12:00:00Z'}
        job = normalize_ashby(self.ashby, {'jobs':[raw]}, 'https://api.ashbyhq.com/example')[0]
        self.assertEqual(job['published_at'], raw['publishedAt'])
        self.assertEqual(job['last_published_at'], raw['publishedAt'])
        self.assertEqual(job['published_at_source'], 'ashby.publishedAt (last published)')
        self.assertEqual(job['updated_at'], raw['updatedAt'])

    def test_lever_joins_locations_and_all_description_sections(self):
        payload = [{"id": "abc", "text": "Data Engineer", "categories": {"commitment": "Full-time", "location": "NY", "allLocations": ["NY", "Boston"], "team": "Data"},
                    "descriptionPlain": "Data platform", "lists": [{"text": "Requirements", "content": "<li>SQL</li>"}],
                    "additionalPlain": "No sponsorship available", "workplaceType": "hybrid"}]
        job = normalize_lever(self.lever, payload, "https://example.com")[0]
        self.assertEqual(job["location"], "NY; Boston")
        self.assertEqual(job["employment_type"], "Full-time")
        self.assertIn("No sponsorship available", job["description"])
        self.assertIn("SQL", job["description"])
        self.assertEqual(job["apply_url"], "https://jobs.lever.co/example/abc/apply")

    def test_ashby_uses_job_url_id_and_secondary_us_location(self):
        payload = {"jobs": [{"title": "Data Engineer", "location": "Toronto", "employmentType": "FullTime", "workplaceType": "Hybrid",
                             "address": {"postalAddress": {"addressCountry": "CAN"}},
                             "secondaryLocations": [{"location": "New York", "address": {"addressCountry": "USA"}}],
                             "jobUrl": "https://jobs.ashbyhq.com/example/uuid", "applyUrl": "https://jobs.ashbyhq.com/example/uuid/application",
                             "descriptionPlain": "Python", "isListed": True},
                            {"title": "Private", "isListed": False}]}
        job = normalize_ashby(self.ashby, payload, "https://example.com")[0]
        self.assertEqual(job["id"], "uuid")
        self.assertEqual(job["country"], "US")
        self.assertEqual(job["countries"], ["CAN", "US"])
        self.assertEqual(job["location"], "Toronto; New York")
        self.assertEqual(job["workplace_type"], "Hybrid")

    def test_bad_schema_is_error_not_empty_board(self):
        with self.assertRaises(SourceError):
            normalize_greenhouse(self.gh, {"error": "not found"}, "https://example.com")
        with self.assertRaises(SourceError):
            normalize_lever(self.lever, {"jobs": []}, "https://example.com")
        with self.assertRaises(SourceError):
            normalize_ashby(self.ashby, None, "https://example.com")

    def test_token_cannot_replace_host_or_inject_query(self):
        with self.assertRaises(SourceError):
            board_url({"source": "greenhouse", "board": "../private?secret=1"})
        self.assertEqual(board_url(self.gh), "https://boards-api.greenhouse.io/v1/boards/example/jobs?content=true")

    @patch("job_hunter.sources.time.sleep")
    @patch("job_hunter.sources.urlopen")
    def test_transient_get_retries_once(self, opened, slept):
        error = HTTPError("https://example.com", 429, "limited", {"Retry-After": "100"}, None)
        opened.side_effect = [error, io.BytesIO(b'{"jobs": []}')]
        self.assertEqual(fetch_json("https://example.com"), {"jobs": []})
        self.assertEqual(opened.call_count, 2)
        slept.assert_called_once_with(2.0)

    @patch("job_hunter.sources.time.sleep")
    @patch("job_hunter.sources.urlopen")
    def test_not_found_not_retried(self, opened, slept):
        opened.side_effect = HTTPError("https://example.com", 404, "missing", {}, None)
        with self.assertRaisesRegex(SourceError, "HTTP 404"):
            fetch_json("https://example.com")
        self.assertEqual(opened.call_count, 1)
        slept.assert_not_called()

    @patch("job_hunter.sources.urlopen")
    def test_invalid_json_reports_failure(self, opened):
        opened.return_value = io.BytesIO(b"<html>blocked</html>")
        with self.assertRaisesRegex(SourceError, "JSONDecodeError"):
            fetch_json("https://example.com")

    @patch("job_hunter.sources.fetch_board")
    def test_failed_board_reported_and_duplicate_results_collapsed(self, fetched):
        record = {"source": "greenhouse", "board": "example", "id": "12", "company": "Example", "title": "Data Engineer"}
        fetched.side_effect = [[record, record], SourceError("HTTP 404")]
        jobs, errors = fetch_jobs([self.gh, self.lever], max_workers=1)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["source"], "lever")
        self.assertIn("404", errors[0]["error"])


class EmploymentEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.job = {"source": "greenhouse", "board": "example", "id": "123", "title": "Data Engineer",
                    "employment_type": "unknown", "apply_url": "https://job-boards.greenhouse.io/example/jobs/123"}

    @staticmethod
    def html(schema):
        return ('<html><body><script type="application/ld+json">' + json.dumps(schema) + '</script></body></html>').encode()

    @patch("job_hunter.sources.build_opener")
    def test_explicit_full_time_from_nested_graph_is_evidence_backed(self, opener):
        schema = {"@context": "https://schema.org", "@graph": [{"@type": "Organization", "employmentType": "PART_TIME"},
                    {"nested": [{"@type": ["Thing", "JobPosting"], "employmentType": "FULL_TIME"}]}]}
        opener.return_value.open.return_value = io.BytesIO(self.html(schema))
        result = enrich_employment(self.job)
        self.assertEqual(result["employment_type"], "Full-time")
        self.assertEqual(result["employment_type_evidence"], self.job["apply_url"])
        self.assertEqual(self.job["employment_type"], "unknown", "Input record must not be mutated")
        opener.return_value.open.assert_called_once()

    @patch("job_hunter.sources.build_opener")
    def test_existing_api_employment_type_is_not_fetched_or_overwritten(self, opener):
        result = enrich_employment({**self.job, "employment_type": "Contract"})
        self.assertEqual(result["employment_type"], "Contract")
        opener.assert_not_called()

    @patch("job_hunter.sources.build_opener")
    def test_non_ats_and_credential_urls_are_not_requested(self, opener):
        for url in ("https://example.com/jobs/123", "http://jobs.lever.co/example/id/apply", "https://jobs.lever.co.attacker.invalid/job", "https://:secret@jobs.lever.co/job"):
            with self.subTest(url=url):
                result = enrich_employment({**self.job, "apply_url": url})
                self.assertEqual(result["employment_type"], "unknown")
                self.assertIn("enrichment_error", result)
        opener.assert_not_called()

    @patch("job_hunter.sources.build_opener")
    def test_no_schema_never_defaults_to_full_time(self, opener):
        opener.return_value.open.return_value = io.BytesIO(b'<html><body>This is an excellent full-time opportunity</body></html>')
        result = enrich_employment(self.job)
        self.assertEqual(result["employment_type"], "unknown")
        self.assertNotIn("employment_type_evidence", result)

    @patch("job_hunter.sources.build_opener")
    def test_multiple_schema_employment_types_stay_unknown(self, opener):
        opener.return_value.open.return_value = io.BytesIO(self.html({"@type": "JobPosting", "employmentType": ["FULL_TIME", "CONTRACTOR"]}))
        result = enrich_employment(self.job)
        self.assertEqual(result["employment_type"], "unknown")

    @patch("job_hunter.sources.build_opener")
    def test_other_posting_cannot_supply_current_jobs_type(self, opener):
        schema = [{"@type": "JobPosting", "identifier": {"value": "456"}, "employmentType": "FULL_TIME"},
                  {"@type": "JobPosting", "identifier": {"value": "789"}, "employmentType": "FULL_TIME"}]
        opener.return_value.open.return_value = io.BytesIO(self.html(schema))
        self.assertEqual(enrich_employment(self.job)["employment_type"], "unknown")

    @patch("job_hunter.sources.build_opener")
    def test_multiple_postings_disambiguated_by_identifier(self, opener):
        schema = [{"@type": "JobPosting", "identifier": {"value": "123"}, "employmentType": "PART_TIME"},
                  {"@type": "JobPosting", "identifier": {"value": "456"}, "employmentType": "FULL_TIME"}]
        opener.return_value.open.return_value = io.BytesIO(self.html(schema))
        self.assertEqual(enrich_employment(self.job)["employment_type"], "Part-time")

    @patch("job_hunter.sources.build_opener")
    def test_network_failure_is_returned_without_retry(self, opener):
        opener.return_value.open.side_effect = URLError("unreachable")
        result = enrich_employment(self.job)
        self.assertEqual(result["employment_type"], "unknown")
        self.assertIn("URLError", result["enrichment_error"])
        opener.return_value.open.assert_called_once()

    @patch("job_hunter.sources.build_opener")
    def test_oversized_html_is_rejected(self, opener):
        opener.return_value.open.return_value = io.BytesIO(b' ' * (MAX_HTML_BYTES + 1))
        result = enrich_employment(self.job)
        self.assertEqual(result["employment_type"], "unknown")
        self.assertIn("3 MiB", result["enrichment_error"])

    @patch("job_hunter.sources.build_opener")
    def test_invalid_json_script_ignored_but_valid_job_script_parsed(self, opener):
        html = b'<script type="application/ld+json">not json</script>' + self.html({"@type": "https://schema.org/JobPosting", "employmentType": ["FULL_TIME"]})
        opener.return_value.open.return_value = io.BytesIO(html)
        self.assertEqual(enrich_employment(self.job)["employment_type"], "Full-time")


class PublicationEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.job = {'source':'lever','board':'example','id':'123','title':'Data Engineer',
                    'employment_type':'Full-time','published_at':'','published_at_source':'',
                    'url':'https://jobs.lever.co/example/123','apply_url':'https://jobs.lever.co/example/123/apply'}

    @staticmethod
    def html(schema):
        return ('<script type="application/ld+json">'+json.dumps(schema)+'</script>').encode()

    @patch('job_hunter.sources.build_opener')
    def test_known_employment_still_enriches_authoritative_date_posted(self, opener):
        opener.return_value.open.return_value = io.BytesIO(self.html({'@type':'JobPosting','datePosted':'2026-10-20'}))
        result = enrich_job_metadata(self.job)
        self.assertEqual(result['published_at'], '2026-10-20')
        self.assertEqual(result['published_at_source'], 'jsonld.datePosted')
        self.assertEqual(result['published_at_evidence'], self.job['url'])
        self.assertEqual(result['employment_type'], 'Full-time')
        opener.return_value.open.assert_called_once()

    @patch('job_hunter.sources.build_opener')
    def test_publication_does_not_depend_on_valid_employment_enum(self, opener):
        schema = {'@type':'JobPosting','datePosted':'2026-10-21T10:30:00-04:00','employmentType':['FULL_TIME','CONTRACTOR']}
        opener.return_value.open.return_value = io.BytesIO(self.html(schema))
        result = enrich_job_metadata({**self.job,'employment_type':'unknown'})
        self.assertEqual(result['published_at'], schema['datePosted'])
        self.assertEqual(result['employment_type'], 'unknown')

    @patch('job_hunter.sources.build_opener')
    def test_modified_created_and_retrieval_dates_are_never_used_as_date_posted(self, opener):
        schema = {'@type':'JobPosting','dateCreated':'2026-11-04','dateModified':'2026-11-05','validThrough':'2026-12-01'}
        opener.return_value.open.return_value = io.BytesIO(self.html(schema))
        result = enrich_job_metadata({**self.job,'updated_at':'2026-11-05','created_at':1793295251143,'first_seen':'2026-11-05'})
        self.assertEqual(result['published_at'], '')
        self.assertEqual(result['published_at_source'], '')

    @patch('job_hunter.sources.build_opener')
    def test_unprovenanced_legacy_created_at_value_cannot_remain_published(self, opener):
        opener.return_value.open.return_value = io.BytesIO(b'<html>No publication data</html>')
        result = enrich_job_metadata({**self.job,'published_at':1793295251143})
        self.assertEqual(result['published_at'], '')
        self.assertEqual(result['unverified_published_at'], 1793295251143)

    @patch('job_hunter.sources.build_opener')
    def test_multiple_posts_use_matching_post_not_newer_unrelated_date(self, opener):
        schema = [{'@type':'JobPosting','identifier':{'value':'456'},'datePosted':'2026-11-05'},
                  {'@type':'JobPosting','identifier':{'value':'123'},'datePosted':'2025-01-01'}]
        opener.return_value.open.return_value = io.BytesIO(self.html({'@graph':schema}))
        result = enrich_job_metadata(self.job)
        self.assertEqual(result['published_at'], '2025-01-01')

    @patch('job_hunter.sources.build_opener')
    def test_invalid_date_posted_stays_unknown(self, opener):
        for value in ('recent', '2026-02-30', 1793295251143):
            with self.subTest(value=value):
                opener.return_value.open.return_value = io.BytesIO(self.html({'@type':'JobPosting','datePosted':value}))
                self.assertEqual(enrich_job_metadata(self.job)['published_at'], '')

    @patch('job_hunter.sources.build_opener')
    @patch('job_hunter.sources.fetch_json')
    def test_greenhouse_detail_supplies_first_publication_when_list_omits_it(self, fetched, opener):
        fetched.return_value = {'id':123,'title':'Data Engineer','first_published':'2026-10-13T16:25:08-04:00',
                                'updated_at':'2026-11-04T16:15:15-05:00'}
        result = enrich_job_metadata({**self.job,'source':'greenhouse','apply_url':'https://job-boards.greenhouse.io/example/jobs/123'})
        self.assertEqual(result['published_at'], '2026-10-13T16:25:08-04:00')
        self.assertEqual(result['published_at_source'], 'greenhouse.first_published')
        self.assertEqual(result['published_at_evidence'], 'https://boards-api.greenhouse.io/v1/boards/example/jobs/123')
        self.assertEqual(result['updated_at'], '2026-11-04T16:15:15-05:00')
        fetched.assert_called_once_with('https://boards-api.greenhouse.io/v1/boards/example/jobs/123', timeout=15)
        opener.assert_not_called()

    @patch('job_hunter.sources.build_opener')
    @patch('job_hunter.sources.fetch_json')
    def test_greenhouse_mismatched_detail_identity_cannot_supply_date(self, fetched, opener):
        fetched.return_value = {'id':456,'title':'Other Job','first_published':'2026-11-05'}
        opener.return_value.open.return_value = io.BytesIO(b'<html>No publication data</html>')
        result = enrich_job_metadata({**self.job,'source':'greenhouse','apply_url':'https://job-boards.greenhouse.io/example/jobs/123'})
        self.assertEqual(result['published_at'], '')
        self.assertIn('metadata_error', result)

    @patch('job_hunter.sources.build_opener')
    @patch('job_hunter.sources.fetch_json')
    def test_greenhouse_detail_time_type_enriches_even_if_publication_is_already_known(self, fetched, opener):
        fetched.return_value = {'id':123,'title':'Data Engineer','first_published':'2026-10-13T16:25:08-04:00',
                                'metadata':[{'name':'Time Type','value':'Full time'}]}
        job = {**self.job,'source':'greenhouse','employment_type':'unknown',
               'published_at':'2026-10-13T16:25:08-04:00','published_at_source':'greenhouse.first_published',
               'apply_url':'https://job-boards.greenhouse.io/example/jobs/123'}
        result = enrich_job_metadata(job)
        self.assertEqual(result['employment_type'], 'Full time')
        self.assertEqual(result['employment_type_source'], 'greenhouse.metadata.Time Type')
        self.assertEqual(result['employment_type_evidence'], 'https://boards-api.greenhouse.io/v1/boards/example/jobs/123')
        fetched.assert_called_once()
        opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
