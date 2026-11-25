import unittest

from job_hunter.matching import clean_text, match_job


class MatchingTests(unittest.TestCase):
    def job(self, **kwargs):
        job = {
            "title": "Data Engineer II",
            "description": "Build Python SQL data pipelines using GCP, BigQuery and Airflow.",
            "employment_type": "FullTime",
            "location": "San Francisco, CA",
        }
        job.update(kwargs)
        return match_job(job, {})

    def test_clean_html_entities(self):
        self.assertEqual(clean_text("<p>GCP &amp; SQL</p><script>tracking()</script><p>data&nbsp;work</p>"), "GCP & SQL data work")

    def test_basic_match(self):
        result = self.job()
        self.assertTrue(result["eligible"])
        self.assertGreaterEqual(result["score"], 70)
        self.assertEqual(result["sponsorship"], "unclear")
        self.assertEqual(result["employment_type"], "full-time")
        self.assertFalse(result["requires_review"])

    def test_unknown_sponsorship_retained(self):
        self.assertTrue(self.job(description="Python and SQL; must be legally authorized to work in the US.")["eligible"])

    def test_no_c2c_is_not_no_sponsorship(self):
        result = self.job(description="Full-time W2 employee only. No C2C or third-party agencies.")
        self.assertTrue(result["eligible"])
        self.assertEqual(result["sponsorship"], "unclear")

    def test_explicit_no_sponsorship(self):
        examples = [
            "We cannot sponsor visas.",
            "We are unable to provide visa sponsorship at this time.",
            "Visa sponsorship is not available.",
            "No immigration sponsorship.",
            "Applicants must be authorized to work in the United States without employer sponsorship.",
            "Candidates must not require sponsorship now or in the future.",
            "We do not sponsor new H-1B visas.",
            "H1B transfers are not accepted.",
            "We cannot accept H-1B transfers.",
            "This position is not eligible for visa sponsorship.",
            "Candidates requiring sponsorship will not be considered.",
            "We will not be able to sponsor visas.",
            "Authorization to work in the United States without current or future visa sponsorship.",
        ]
        for text in examples:
            with self.subTest(text=text):
                result = self.job(description=text)
                self.assertFalse(result["eligible"])
                self.assertEqual(result["sponsorship"], "restricted")

    def test_transfer_exception(self):
        examples = [
            "We do not sponsor new H-1B visas. Existing H-1B transfers are accepted.",
            "No visa sponsorship for new petitions; we support H1B transfers.",
            "Candidates requiring an H-1B transfer may apply.",
        ]
        for text in examples:
            with self.subTest(text=text):
                result = self.job(description=text)
                self.assertTrue(result["eligible"])
                self.assertEqual(result["sponsorship"], "supported")

    def test_citizen_restrictions(self):
        examples = [
            "U.S. citizens only.",
            "Must be a US citizen.",
            "US citizenship required.",
            "U.S. citizenship is required.",
            "Only green card holders.",
            "Green-card holders only.",
            "Must be a permanent resident.",
            "US citizen or permanent resident.",
            "Citizenship: US Citizen.",
            "USC/GC only.",
            "Must be a citizen of the United States.",
            "U.S. citizen or GC.",
            "U.S. citizenship and eligibility for a U.S. government secret clearance are required to access classified information.",
        ]
        for text in examples:
            with self.subTest(text=text):
                result = self.job(description=text)
                self.assertFalse(result["eligible"])
                self.assertEqual(result["sponsorship"], "restricted")

    def test_clearance_is_not_inferred_citizenship(self):
        self.assertTrue(self.job(description="Must be able to obtain a security clearance.")["eligible"])

    def test_export_license_sponsorship_is_not_visa_sponsorship(self):
        result = self.job(description="This position may require access to information protected under U.S. export control laws. Any offer of employment may be conditioned on your authorization to receive software or technology controlled under these U.S. export laws without sponsorship for an export license.")
        self.assertTrue(result["eligible"])
        self.assertEqual(result["sponsorship"], "unclear")

    def test_eeo_not_citizenship_restriction(self):
        result = self.job(description="We hire without regard to citizenship, age, disability, race or national origin. We are an equal opportunity employer.")
        self.assertTrue(result["eligible"])
        self.assertEqual(result["sponsorship"], "unclear")

    def test_citizenship_not_required(self):
        self.assertTrue(self.job(description="U.S. citizenship is not required.")["eligible"])

    def test_inclusive_work_authorization_alternative(self):
        self.assertTrue(self.job(description="Must be a US citizen or have valid work authorization.")["eligible"])
        self.assertTrue(self.job(description="US citizens or permanent residents or visa holders may apply.")["eligible"])

    def test_citizen_requirement_beats_transfer_statement(self):
        result = self.job(description="US citizens only. H-1B transfers are accepted.")
        self.assertFalse(result["eligible"])

    def test_sponsorship_support(self):
        for text in ["Visa sponsorship is available.", "We can sponsor work visas.", "We provide visa sponsorship."]:
            with self.subTest(text=text):
                self.assertEqual(self.job(description=text)["sponsorship"], "supported")

    def test_sponsorship_questions_not_policy(self):
        result = self.job(description="Will you now or in the future require sponsorship for employment visa status?")
        self.assertTrue(result["eligible"])
        self.assertEqual(result["sponsorship"], "unclear")

    def test_contract_employment(self):
        self.assertFalse(self.job(employment_type="Contract")["eligible"])

    def test_contract_title_overrides_fulltime_metadata(self):
        self.assertFalse(self.job(title="Data Engineer (Contract)")["eligible"])

    def test_data_contracts_not_contract_employment(self):
        self.assertTrue(self.job(description="Design data contracts, full-time employee benefits, and Python pipelines.")["eligible"])

    def test_role_specific_description_overrides_fulltime_api(self):
        examples = [
            ("This is a contract position.", "contract"),
            ("This is a 6-month contract role.", "contract"),
            ("This role is a full-time contract position.", "contract"),
            ("This position will be temporary.", "temporary"),
            ("This is a part-time opportunity.", "part-time"),
            ("This part-time role builds our pipelines.", "part-time"),
            ("Employment type: contract.", "contract"),
        ]
        for description, expected in examples:
            with self.subTest(description=description):
                result = self.job(description=description)
                self.assertEqual(result["employment_type"], expected)
                self.assertFalse(result["eligible"])

    def test_negated_contract_statement_not_restricted(self):
        result = self.job(description="This is not a contract position. Build Python pipelines and design data contracts.")
        self.assertEqual(result["employment_type"], "full-time")
        self.assertTrue(result["eligible"])

    def test_customer_contract_role_not_employment_type(self):
        result = self.job(description="Build Python pipelines for insurance contracts. Help a contract role classification team analyze data.")
        self.assertEqual(result["employment_type"], "full-time")
        self.assertTrue(result["eligible"])

    def test_internship_excluded(self):
        self.assertFalse(self.job(title="Data Engineering Intern")["eligible"])

    def test_parttime_excluded(self):
        self.assertFalse(self.job(employment_type="PartTime")["eligible"])

    def test_unknown_type_flagged(self):
        result = self.job(employment_type="")
        self.assertEqual(result["employment_type"], "unknown")
        self.assertTrue(result["requires_review"])
        self.assertFalse(result["eligible"])

    def test_explicitly_allowed_unknown_employment_retains_type_without_hold(self):
        profile = {"preferences":{"allow_unknown_employment_type":True}}
        job = {"title":"Data Engineer", "description":"Build Python SQL data pipelines.", "employment_type":"unknown", "location":"US"}
        result = match_job(job, profile)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["employment_type"], "unknown")
        self.assertTrue(result["unknown_employment_allowed"])
        self.assertFalse(result["requires_review"])
        self.assertTrue(any("does not specify employment type" in reason for reason in result["reasons"]))

    def test_unknown_employment_opt_in_needs_explicit_boolean(self):
        job = {"title":"Data Engineer", "description":"Build data pipelines.", "employment_type":"unknown", "location":"US"}
        for value in (False, None, "true", 1):
            with self.subTest(value=value):
                result = match_job(job, {"preferences":{"allow_unknown_employment_type":value}})
                self.assertFalse(result["eligible"])
                self.assertFalse(result["unknown_employment_allowed"])
                self.assertTrue(result["requires_review"])

    def test_unknown_employment_opt_in_keeps_explicit_nonfulltime_exclusions(self):
        profile = {"preferences":{"allow_unknown_employment_type":True}}
        for kind in ("Contract", "PartTime", "Internship", "Temporary"):
            with self.subTest(kind=kind):
                result = match_job({"title":"Data Engineer", "description":"Build data pipelines.", "employment_type":kind, "location":"US"}, profile)
                self.assertFalse(result["eligible"])
                self.assertFalse(result["unknown_employment_allowed"])
        description_contract = match_job({"title":"Data Engineer", "description":"This is a contract position.", "employment_type":"unknown", "location":"US"}, profile)
        self.assertFalse(description_contract["eligible"])
        self.assertEqual(description_contract["employment_type"], "contract")

    def test_unknown_employment_opt_in_does_not_override_nonus_location(self):
        result = match_job({"title":"Data Engineer", "description":"Build data pipelines.", "employment_type":"unknown", "location":"Toronto, Canada"}, {"preferences":{"allow_unknown_employment_type":True}})
        self.assertFalse(result["eligible"])
        self.assertEqual(result["location_status"], "non-us")

    def test_description_fulltime(self):
        result = self.job(employment_type="", description="Employment Type: Full-Time. Python SQL data pipelines.")
        self.assertEqual(result["employment_type"], "full-time")
        self.assertTrue(result["eligible"])

    def test_description_this_fulltime_role(self):
        self.assertTrue(self.job(employment_type="", description="This full-time role builds data pipelines.")["eligible"])

    def test_role_specific_fulltime_salary_statement_confirms_type(self):
        result = self.job(employment_type="unknown", description="The range of annual base salary for full-time employees for this position is below. $120,000-$150,000.")
        self.assertEqual(result["employment_type"], "full-time")
        self.assertTrue(result["eligible"])
        self.assertTrue(any("Role-specific full-time evidence" in reason for reason in result["reasons"]))

    def test_generic_fulltime_benefits_do_not_confirm_role(self):
        result = self.job(employment_type="unknown", description="Full-time employees enjoy benefits and an annual base salary. We value Python and SQL.")
        self.assertEqual(result["employment_type"], "unknown")
        self.assertFalse(result["eligible"])

    def test_explicit_contract_overrides_fulltime_salary_boilerplate(self):
        result = self.job(employment_type="unknown", description="This is a contract position. The range of annual base salary for full-time employees for this position is below.")
        self.assertEqual(result["employment_type"], "contract")
        self.assertFalse(result["eligible"])
        self.assertFalse(any("Role-specific full-time evidence" in reason for reason in result["reasons"]))

    def test_familiar_us_city(self):
        self.assertEqual(self.job(location="Seattle")["location_status"], "us")

    def test_remote_us(self):
        self.assertEqual(self.job(location="Remote - United States")["location_status"], "us")

    def test_non_us(self):
        for location in ["Toronto, Canada", "London, United Kingdom", "Berlin, Germany", "Bengaluru, India", "Singapore"]:
            with self.subTest(location=location):
                result = self.job(location=location)
                self.assertEqual(result["location_status"], "non-us")
                self.assertFalse(result["eligible"])

    def test_country_overrides_ambiguous_city(self):
        result = self.job(location="London", country="Canada")
        self.assertFalse(result["eligible"])

    def test_us_location_alternative_retained(self):
        result = self.job(location="New York, NY / Toronto, Canada")
        self.assertTrue(result["eligible"])

    def test_unknown_remote_requires_review(self):
        result = self.job(location="Remote")
        self.assertTrue(result["eligible"])
        self.assertTrue(result["requires_review"])
        self.assertEqual(result["location_status"], "unknown")

    def test_us_office_evidence_resolves_ambiguous_remote_location(self):
        for location in ("Remote - CA", "Remote - MA"):
            with self.subTest(location=location):
                result = self.job(location=location, office_locations=["Remote - SF Bay Area"])
                self.assertEqual(result["location_status"], "us")
                self.assertFalse(result["requires_review"])
                self.assertTrue(any("official job-board offices" in reason for reason in result["reasons"]))

    def test_us_office_never_overrides_explicit_non_us_primary(self):
        for fields in ({"location":"Remote - Canada"}, {"location":"Remote - CA", "country":"CA"}):
            with self.subTest(fields=fields):
                result = self.job(**fields, office_locations=["Remote - SF Bay Area"])
                self.assertEqual(result["location_status"], "non-us")
                self.assertFalse(result["eligible"])

    def test_mixed_or_unknown_offices_do_not_establish_us_location(self):
        for offices in (["Remote - SF Bay Area", "London"], ["Remote - SF Bay Area", "Remote"]):
            with self.subTest(offices=offices):
                result = self.job(location="Remote", office_locations=offices)
                self.assertEqual(result["location_status"], "unknown")
                self.assertTrue(result["requires_review"])

    def test_office_fallback_does_not_rewrite_an_unrecognized_primary_place(self):
        result = self.job(location="Remote - EMEA", office_locations=["USA"])
        self.assertEqual(result["location_status"], "unknown")
        self.assertTrue(result["requires_review"])

    def test_senior_not_excluded(self):
        result = self.job(title="Senior Data Engineer")
        self.assertTrue(result["eligible"])
        self.assertLess(result["score"], self.job()["score"])

    def test_director_scores_lower(self):
        self.assertLess(self.job(title="Director, Data Engineering")["score"], self.job()["score"])

    def test_title_fit_matters(self):
        self.assertGreater(self.job()["score"], self.job(title="Financial Analyst")["score"])

    def test_profile_skills_control_overlap(self):
        job = {"title": "Data Engineer", "description": "Python SQL BigQuery GCP Airflow", "employment_type": "full-time", "location": "US"}
        self.assertLess(match_job(job, {"skills": ["Python"]})["score"], match_job(job, {})["score"])

    def test_notion_five_year_data_engineer_requirement_needs_review(self):
        result = self.job(description="5+ years of experience as a data engineer building core datasets and supporting business functions, with a passion for analytics.")
        self.assertTrue(result["eligible"])
        self.assertTrue(result["requires_review"])
        self.assertTrue(any("5+ years" in reason for reason in result["reasons"]))

    def test_headway_six_year_requirement_needs_review(self):
        result = self.job(title="Senior Data Engineer", description="6+ years of experience as a data engineer, with a track record of building scalable systems and solving complex data problems in a fast-paced tech environment.")
        self.assertTrue(result["eligible"])
        self.assertTrue(result["requires_review"])

    def test_openai_software_experience_not_lower_data_alternative(self):
        result = self.job(description="Have 3+ years of experience as a data engineer and 8+ years of any software engineering experience(including data engineering).")
        self.assertTrue(result["requires_review"])
        self.assertTrue(any("8+ years" in reason for reason in result["reasons"]))

    def test_research_not_assumed_fulltime_industry(self):
        job = {"title":"Data Engineer", "description":"5+ years of experience as a data engineer.", "location":"US", "employment_type":"FullTime"}
        profile = {"experience":[{"title":"Graduate Research Associate", "start":"2018-05", "end":"2024-05"}, {"title":"Data Engineer", "start":"2025-09", "end":"2026-11"}]}
        result = match_job(job, profile)
        self.assertTrue(result["requires_review"])
        self.assertTrue(any("1.2 years" in reason for reason in result["reasons"]))

    def test_sufficient_dated_industry_work_no_experience_hold(self):
        job = {"title":"Data Engineer", "description":"5+ years of experience as a data engineer.", "location":"US", "employment_type":"FullTime"}
        result = match_job(job, {"experience":[{"title":"Data Engineer", "start":"2018-05", "end":"2024-05"}]})
        self.assertFalse(result["requires_review"])

    def test_three_year_or_seven_alternative_not_seven_year_minimum(self):
        result = self.job(description="3+ years of experience implementing enterprise-grade data pipelines, or 7+ years of related professional experience (e.g., software, data, or integration engineering) that includes building and operating production data pipelines.")
        self.assertFalse(result["requires_review"])

    def test_preferred_experience_not_a_hold(self):
        result = self.job(description="Preferred: 5+ years of experience as a data engineer.")
        self.assertFalse(result["requires_review"])

    def test_preferred_section_not_a_hold(self):
        result = self.job(description="Nice to haves\n5+ years of experience as a data engineer.")
        self.assertFalse(result["requires_review"])

    def test_company_age_not_experience(self):
        result = self.job(description="Our company has built software for 15 years. We build data engineering systems. We offer 5 years of data retention.")
        self.assertFalse(result["requires_review"])

    def test_confirmed_seven_year_floor_satisfies_five_and_seven_year_jobs(self):
        profile = {
            "experience_summary":{"data_engineering_years_min":7.0},
            "experience":[{"title":"Data Engineer", "start":"2025-09", "end":"2026-11"}],
        }
        for required in (5, 7):
            with self.subTest(required=required):
                job = {"title":"Senior Data Engineer", "description":f"{required}+ years of data engineering experience. Python SQL GCP BigQuery Airflow.", "location":"US", "employment_type":"FullTime"}
                result = match_job(job, profile)
                self.assertTrue(result["eligible"])
                self.assertFalse(result["experience_requires_review"])
                self.assertFalse(result["requires_review"])
                self.assertEqual(result["data_engineering_years_used"], 7.0)
                self.assertEqual(result["experience_years_source"], "applicant-confirmed")
        self.assertEqual(profile["experience"][0]["start"], "2025-09")

    def test_confirmed_seven_year_floor_does_not_infer_eight(self):
        profile = {"experience_summary":{"data_engineering_years_min":7}}
        job = {"title":"Data Engineer", "description":"Have 3+ years of experience as a data engineer and 8+ years of any software engineering experience(including data engineering).", "location":"US", "employment_type":"FullTime"}
        result = match_job(job, profile)
        self.assertTrue(result["eligible"])
        self.assertTrue(result["experience_requires_review"])
        self.assertTrue(result["requires_review"])
        self.assertTrue(any("confirms at least 7.0" in reason for reason in result["reasons"]))

    def test_opted_in_one_year_experience_stretch_is_advisory(self):
        profile = {"experience_summary":{"data_engineering_years_min":7}, "preferences":{"allow_experience_stretch":True, "max_experience_stretch_years":1}}
        job = {"title":"Senior Data Engineer", "description":"Minimum requirements: 8+ years of data engineering experience.", "location":"US", "employment_type":"FullTime"}
        result = match_job(job, profile)
        self.assertTrue(result["eligible"])
        self.assertTrue(result["experience_requires_review"])
        self.assertTrue(result["experience_stretch_allowed"])
        self.assertFalse(result["requires_review"])
        self.assertEqual(result["experience_gap_years"], 1.0)
        self.assertEqual(result["data_engineering_years_used"], 7.0)
        self.assertTrue(any("does not establish" in reason for reason in result["reasons"]))

    def test_stretch_does_not_allow_larger_gap_or_unknown_location(self):
        profile = {"experience_summary":{"data_engineering_years_min":7}, "preferences":{"allow_experience_stretch":True, "max_experience_stretch_years":1}}
        job = {"title":"Senior Data Engineer", "description":"Minimum requirements: 9+ years of data engineering experience.", "location":"US", "employment_type":"FullTime"}
        result = match_job(job, profile)
        self.assertTrue(result["requires_review"])
        self.assertFalse(result["experience_stretch_allowed"])
        unresolved = match_job({**job, "description":"8+ years of data engineering experience.", "location":"Remote"}, profile)
        self.assertTrue(unresolved["experience_stretch_allowed"])
        self.assertTrue(unresolved["requires_review"])

    def test_stretch_needs_explicit_boolean_and_confirmed_experience(self):
        job = {"title":"Data Engineer", "description":"8+ years of data engineering experience.", "location":"US", "employment_type":"FullTime"}
        for opt_in in (False, "true", 1, None):
            with self.subTest(opt_in=opt_in):
                profile = {"experience_summary":{"data_engineering_years_min":7}, "preferences":{"allow_experience_stretch":opt_in, "max_experience_stretch_years":1}}
                result = match_job(job, profile)
                self.assertTrue(result["requires_review"])
                self.assertFalse(result["experience_stretch_allowed"])
        fallback = match_job(job, {"experience":[{"title":"Data Engineer", "start":"2019-09", "end":"2026-09"}], "preferences":{"allow_experience_stretch":True, "max_experience_stretch_years":1}})
        self.assertTrue(fallback["requires_review"])
        self.assertFalse(fallback["experience_stretch_allowed"])

    def test_invalid_stretch_limit_never_removes_experience_hold(self):
        job = {"title":"Data Engineer", "description":"8+ years of data engineering experience.", "location":"US", "employment_type":"FullTime"}
        for limit in (True, -1, None, float("inf"), float("nan"), "unlimited"):
            with self.subTest(limit=limit):
                profile = {"experience_summary":{"data_engineering_years_min":7}, "preferences":{"allow_experience_stretch":True, "max_experience_stretch_years":limit}}
                result = match_job(job, profile)
                self.assertTrue(result["requires_review"])
                self.assertFalse(result["experience_stretch_allowed"])

    def test_seven_to_ten_year_stretch_can_be_allowed_but_eleven_stays_held(self):
        profile = {"experience_summary":{"data_engineering_years_min":7}, "preferences":{"allow_experience_stretch":True, "max_experience_stretch_years":3, "allow_unknown_employment_type":True}}
        base = {"title":"Senior Data Engineer", "location":"US", "employment_type":"unknown"}
        ten = match_job({**base, "description":"10+ years of data engineering experience. Python SQL GCP BigQuery Airflow."}, profile)
        self.assertTrue(ten["eligible"])
        self.assertTrue(ten["experience_stretch_allowed"])
        self.assertTrue(ten["unknown_employment_allowed"])
        self.assertFalse(ten["requires_review"])
        self.assertEqual(ten["experience_gap_years"], 3.0)
        self.assertEqual(ten["data_engineering_years_used"], 7.0)
        eleven = match_job({**base, "description":"11+ years of data engineering experience. Python SQL GCP BigQuery Airflow."}, profile)
        self.assertFalse(eleven["experience_stretch_allowed"])
        self.assertTrue(eleven["requires_review"])

    def test_seven_year_floor_removes_satisfied_industry_and_senior_penalties(self):
        profile = {"experience_summary":{"data_engineering_years_min":7}}
        base = {"title":"Data Engineer", "description":"Python SQL GCP BigQuery Airflow.", "location":"US", "employment_type":"FullTime"}
        baseline = match_job(base, profile)
        senior = match_job({**base, "title":"Senior Data Engineer", "description":base["description"]+" 5+ years of professional experience."}, profile)
        self.assertEqual(senior["score"], baseline["score"])
        self.assertFalse(senior["experience_requires_review"])

    def test_confirmed_de_years_do_not_add_technology_years_or_job_tenure(self):
        profile = {"experience_summary":{"data_engineering_years_min":7}, "skills":["GCP", "SQL"], "experience":[{"title":"Data Engineer", "company":"CVS Health", "start":"2025-09", "end":"2026-11"}]}
        job = {"title":"Data Engineer", "description":"Use GCP and SQL.", "location":"US", "employment_type":"FullTime"}
        result = match_job(job, profile)
        self.assertEqual(result["data_engineering_years_used"], 7.0)
        self.assertNotIn("gcp_years", result)
        self.assertEqual(profile["experience"][0]["end"], "2026-11")

    def test_invalid_experience_floor_falls_back_to_dated_work(self):
        job = {"title":"Data Engineer", "description":"5+ years of data engineering experience.", "location":"US", "employment_type":"FullTime"}
        for value in (True, -7, "not known", float("inf"), float("nan")):
            with self.subTest(value=value):
                result = match_job(job, {"experience_summary":{"data_engineering_years_min":value}, "experience":[{"title":"Data Engineer", "start":"2025-09", "end":"2026-11"}]})
                self.assertTrue(result["experience_requires_review"])
                self.assertEqual(result["experience_years_source"], "dated-industry")


if __name__ == "__main__":
    unittest.main()
