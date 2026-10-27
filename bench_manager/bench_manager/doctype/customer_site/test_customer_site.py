# -*- coding: utf-8 -*-
# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase


def make_site(prefix="ctrltest", order="CTRL-1", **kw):
	values = {
		"doctype": "Customer Site",
		"prefix": prefix,
		"marketing_order_id": order,
		"customer_name": "Owner",
		"customer_email": "owner@ctrl.example",
		"company_name": "Ctrl Co",
		"company_abbr": "CC",
		"chart_of_accounts": "Standard",
		"country": "Saudi Arabia",
		"currency": "SAR",
		"language": "en",
		"fy_start_date": "2026-01-01",
		"first_user_full_name": "Owner",
		"first_user_email": "owner@ctrl.example",
	}
	values.update(kw)
	doc = frappe.get_doc(values)
	doc.insert()
	return doc


def make_published(prefix, order):
	"""A Published Customer Site (the publish job itself is not started)."""
	doc = make_site(prefix=prefix, order=order)
	for status in ("Creating", "Configuring", "Awaiting Domain Mapping", "Ready"):
		doc.flags.allow_status_change = True
		doc.status = status
		doc.save()
	with mock.patch("bench_manager.bench_manager.doctype.customer_site.customer_site.enqueue_publish"):
		doc.publish_to_customer = 1
		doc.save()
	return doc


class TestCustomerSite(IntegrationTestCase):
	def setUp(self):
		patcher = mock.patch("bench_manager.bench_manager.doctype.customer_site.customer_site.enqueue_publish")
		self.enqueue_publish = patcher.start()
		self.addCleanup(patcher.stop)
		frappe.set_user("Administrator")
		frappe.db.set_single_value("SaaS Settings", "base_domain", "test.example.com")
		# Tests must never start a real site build.
		frappe.db.set_single_value("SaaS Settings", "auto_provision_on_save", 0)

	def advance(self, doc, *statuses):
		for status in statuses:
			doc.flags.allow_status_change = True
			doc.status = status
			doc.save()

	def test_status_cannot_be_edited_directly(self):
		doc = make_site(prefix="locked", order="LOCK-1")
		doc.status = "Creating"  # no sanctioned flag: this is a hand edit
		with self.assertRaises(frappe.ValidationError):
			doc.save()
		self.assertEqual(frappe.db.get_value("Customer Site", doc.name, "status"), "Queued")

	def test_override_needs_reason_and_valid_target(self):
		doc = make_site(prefix="override", order="LOCK-2")
		self.advance(doc, "Creating", "Configuring", "Awaiting Domain Mapping")
		doc.reload()
		# Ready belongs to Mark Ready, Stopped to Stop Site, Published to the checkbox
		self.assertEqual(doc.get_overridable_statuses(), ["Failed"])
		with self.assertRaises(frappe.ValidationError):
			doc.override_status("Failed", "")
		for status in ("Ready", "Stopped", "Published", "Creating"):
			with self.assertRaises(frappe.ValidationError, msg=status):
				doc.override_status(status, "trying a shortcut")

		self.assertEqual(doc.override_status("Failed", "Checked on local, marking failed"), "Failed")
		log = frappe.get_all("SaaS Event Log", filters={"customer_site": doc.name, "event": "Admin Action"}, pluck="detail")
		self.assertTrue(any("Awaiting Domain Mapping -> Failed" in d and "Checked on local" in d for d in log))

	def test_naming_and_defaults(self):
		doc = make_site(prefix="CtrlTest")
		self.assertEqual(doc.name, "ctrltest.test.example.com")
		self.assertEqual(doc.site_url, "https://ctrltest.test.example.com")
		self.assertEqual(doc.status, "Queued")
		self.assertEqual(str(doc.fy_end_date), "2026-12-31")

	def test_timezone_follows_country(self):
		doc = make_site()
		self.assertEqual(doc.timezone, "Asia/Riyadh")

	def test_invalid_language_and_chart_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			make_site(language="zz")
		with self.assertRaises(frappe.ValidationError):
			make_site(prefix="chartbad", order="CTRL-9", chart_of_accounts="Nope")

	def test_invalid_or_duplicate_prefix(self):
		with self.assertRaises(frappe.ValidationError):
			make_site(prefix="admin")
		make_site()
		with self.assertRaises(frappe.DuplicateEntryError):
			make_site(order="CTRL-2")

	def test_cannot_skip_statuses(self):
		doc = make_site()
		doc.flags.allow_status_change = True
		doc.status = "Ready"
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_publish_requires_ready(self):
		doc = make_site()
		doc.publish_to_customer = 1
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_publish_flow_and_unpublish_blocked(self):
		doc = make_site()
		self.advance(doc, "Creating", "Configuring", "Awaiting Domain Mapping", "Ready")
		doc.publish_to_customer = 1
		doc.save()
		self.enqueue_publish.assert_called_once_with(doc.name)  # publishing starts the email/password job
		self.assertEqual(doc.status, "Published")
		self.assertTrue(doc.published_on)
		self.assertEqual(doc.published_by, "Administrator")

		doc.publish_to_customer = 0
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_status_published_needs_checkbox(self):
		doc = make_site()
		self.advance(doc, "Creating", "Configuring", "Awaiting Domain Mapping", "Ready")
		doc.flags.allow_status_change = True
		doc.status = "Published"
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_stop_and_resume_returns_to_previous_status(self):
		doc = make_site()
		self.advance(doc, "Creating", "Configuring", "Awaiting Domain Mapping", "Ready")
		doc.publish_to_customer = 1
		doc.save()
		self.advance(doc, "Stopped")
		self.assertTrue(doc.is_stopped)
		self.assertEqual(doc.status_before_stop, "Published")

		doc.flags.allow_status_change = True
		doc.status = "Ready"
		with self.assertRaises(frappe.ValidationError):
			doc.save()
		doc.reload()
		self.advance(doc, "Published")
		self.assertFalse(doc.is_stopped)

	def test_published_site_cannot_be_deleted(self):
		doc = make_site()
		self.advance(doc, "Creating", "Configuring", "Awaiting Domain Mapping", "Ready")
		doc.publish_to_customer = 1
		doc.save()
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("Customer Site", doc.name)

	def test_event_log_is_append_only(self):
		doc = make_site()
		log = frappe.get_all("SaaS Event Log", filters={"customer_site": doc.name}, pluck="name")
		self.assertTrue(log)
		entry = frappe.get_doc("SaaS Event Log", log[0])
		entry.detail = "tampered"
		with self.assertRaises(frappe.ValidationError):
			entry.save()
		with self.assertRaises(frappe.ValidationError):
			entry.delete()
