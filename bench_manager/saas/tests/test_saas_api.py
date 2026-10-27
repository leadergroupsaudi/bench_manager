# -*- coding: utf-8 -*-
# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import copy

import frappe
from frappe.tests import IntegrationTestCase

from bench_manager.saas import api
from bench_manager.saas.utils import (
	SaaSAPIError,
	check_prefix_availability,
	get_default_fiscal_year,
	validate_prefix,
)

BASE_PAYLOAD = {
	"marketing_order_id": "TEST-ORD-1",
	"prefix": "acmetest",
	"plan_name": "Standard",
	"customer": {"name": "Acme Owner", "email": "owner@acme.example"},
	"company": {"name": "Acme Trading", "abbr": "ACM", "chart_of_accounts": "Standard"},
	"setup": {
		"language": "English",
		"country": "Saudi Arabia",
		"timezone": "Asia/Riyadh",
		"currency": "SAR",
		"fy_start_date": "2026-01-01",
	},
	"first_user": {"full_name": "Acme Owner", "email": "owner@acme.example"},
	"limits": {"max_users": 10},
}


def payload(**overrides):
	p = copy.deepcopy(BASE_PAYLOAD)
	p.update(overrides)
	return p


class TestSaaSAPI(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("SaaS Settings", "base_domain", "test.example.com")
		# Tests must never start a real site build.
		frappe.db.set_single_value("SaaS Settings", "auto_provision_on_save", 0)
		frappe.db.set_single_value("SaaS Settings", "max_sites_per_hour", 50)
		frappe.local.response = frappe._dict()

	# ------------------------------------------------------------- prefix rules

	def test_prefix_rules(self):
		self.assertEqual(validate_prefix("Acme-1"), "acme-1")
		for bad in ("", "ab", "-acme", "acme-", "ac me", "acme_x", "a--b", "x" * 31, "admin", "www"):
			with self.assertRaises(SaaSAPIError, msg=bad):
				validate_prefix(bad)

	def test_check_subdomain(self):
		self.assertTrue(api.check_subdomain(prefix="freeprefix")["available"])
		res = api.check_subdomain(prefix="admin")
		self.assertFalse(res["available"])
		self.assertEqual(res["reason"], "prefix_reserved")

	# ------------------------------------------------------------- create + idempotency

	def test_create_and_replay(self):
		first = api.create_site_request(**payload())
		self.assertTrue(first["ok"], first)
		self.assertEqual(first["site_name"], "acmetest.test.example.com")
		self.assertEqual(first["status"], "Queued")
		self.assertFalse(first["idempotent_replay"])
		self.assertIsNone(first["site_url"])

		again = api.create_site_request(**payload())
		self.assertTrue(again["idempotent_replay"])
		self.assertEqual(frappe.db.count("Customer Site", {"marketing_order_id": "TEST-ORD-1"}), 1)

	def test_same_order_different_payload_conflicts(self):
		api.create_site_request(**payload())
		res = api.create_site_request(**payload(plan_name="Other"))
		self.assertFalse(res["ok"])
		self.assertEqual(res["error"]["code"], "idempotency_conflict")
		self.assertEqual(frappe.local.response["http_status_code"], 409)

	def test_duplicate_prefix_other_order(self):
		api.create_site_request(**payload())
		res = api.create_site_request(**payload(marketing_order_id="TEST-ORD-2"))
		self.assertEqual(res["error"]["code"], "prefix_taken")

	# ------------------------------------------------------------- validation

	def test_password_fields_rejected(self):
		p = payload()
		p["first_user"]["password"] = "x"
		res = api.create_site_request(**p)
		self.assertEqual(res["error"]["code"], "password_not_accepted")
		self.assertFalse(frappe.db.exists("Customer Site", "acmetest.test.example.com"))

	def test_invalid_inputs(self):
		cases = {
			"missing company": payload(company={}),
			"bad email": payload(customer={"name": "A", "email": "nope"}),
			"bad country": payload(setup={**BASE_PAYLOAD["setup"], "country": "Atlantis"}),
			"bad tz": payload(setup={**BASE_PAYLOAD["setup"], "timezone": "Mars/Base"}),
			"bad date": payload(setup={**BASE_PAYLOAD["setup"], "fy_start_date": "not-a-date"}),
			"bad chart": payload(company={**BASE_PAYLOAD["company"], "chart_of_accounts": "Nope"}),
			"bad abbr": payload(company={**BASE_PAYLOAD["company"], "abbr": "TOO-LONG-ABBR!"}),
			"bad limit": payload(limits={"max_users": 0}),
		}
		for name, p in cases.items():
			res = api.create_site_request(**p)
			self.assertFalse(res["ok"], name)
			self.assertEqual(frappe.local.response["http_status_code"], 422, name)

	def test_timezone_defaults_from_country_and_language_by_code(self):
		p = payload()
		del p["setup"]["timezone"]
		p["setup"]["language"] = "en"
		res = api.create_site_request(**p)
		self.assertTrue(res["ok"], res)
		doc = frappe.get_doc("Customer Site", res["site_name"])
		self.assertEqual(doc.timezone, "Asia/Riyadh")
		self.assertEqual(doc.language, "en")

	def test_fiscal_year_defaults_follow_the_wizard_table(self):
		india = get_default_fiscal_year("India")
		self.assertTrue(india["fy_start_date"].endswith("-04-01"))
		self.assertTrue(india["fy_end_date"].endswith("-03-31"))
		australia = get_default_fiscal_year("Australia")
		self.assertTrue(australia["fy_start_date"].endswith("-07-01"))
		saudi = get_default_fiscal_year("Saudi Arabia")
		self.assertTrue(saudi["fy_start_date"].endswith("-01-01"))
		self.assertTrue(saudi["fy_end_date"].endswith("-12-31"))

	def test_fy_start_date_optional_in_api(self):
		p = payload()
		del p["setup"]["fy_start_date"]
		res = api.create_site_request(**p)
		self.assertTrue(res["ok"], res)
		doc = frappe.get_doc("Customer Site", res["site_name"])
		self.assertTrue(str(doc.fy_start_date).endswith("-01-01"))

	def test_hourly_cap(self):
		frappe.db.set_single_value("SaaS Settings", "max_sites_per_hour", 1)
		self.assertTrue(api.create_site_request(**payload())["ok"])
		res = api.create_site_request(**payload(marketing_order_id="TEST-ORD-3", prefix="othertest"))
		self.assertEqual(res["error"]["code"], "hourly_cap_reached")

	# ------------------------------------------------------------- authorisation

	def test_requires_integration_role(self):
		user = "saas.noaccess@example.com"
		if not frappe.db.exists("User", user):
			frappe.get_doc(
				{"doctype": "User", "email": user, "first_name": "No", "send_welcome_email": 0}
			).insert(ignore_permissions=True)
		frappe.set_user(user)
		res = api.check_subdomain(prefix="freeprefix")
		self.assertFalse(res["ok"])
		self.assertEqual(frappe.local.response["http_status_code"], 403)
		frappe.set_user("Administrator")

	# ------------------------------------------------------------- status endpoint

	def test_status_hides_url_until_published(self):
		api.create_site_request(**payload())
		status = api.get_site_status(marketing_order_id="TEST-ORD-1")
		self.assertEqual(status["state"], "queued")
		self.assertIsNone(status["site_url"])

		missing = api.get_site_status(marketing_order_id="NOPE")
		self.assertEqual(missing["error"]["code"], "not_found")
