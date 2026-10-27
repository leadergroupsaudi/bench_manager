# -*- coding: utf-8 -*-
# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase

from bench_manager.bench_manager.doctype.customer_site.test_customer_site import make_site
from bench_manager.saas.provisioner import STEPS, Provisioner, ProvisioningError

ENQUEUE = "bench_manager.bench_manager.doctype.customer_site.customer_site.enqueue_provisioning"


class TestProvisioning(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("SaaS Settings", "base_domain", "test.example.com")
		frappe.db.set_single_value("SaaS Settings", "auto_provision_on_save", 0)

	# ------------------------------------------------------------- auto start

	def test_save_starts_provisioning_when_enabled(self):
		frappe.db.set_single_value("SaaS Settings", "auto_provision_on_save", 1)
		with mock.patch(ENQUEUE) as enqueue:
			doc = make_site(prefix="autostart", order="PROV-1")
		enqueue.assert_called_once_with(doc.name)

	def test_auto_start_defaults_on_when_never_saved(self):
		from bench_manager.saas.utils import get_settings

		frappe.db.delete("Singles", {"doctype": "SaaS Settings", "field": "auto_provision_on_save"})
		self.assertEqual(get_settings().auto_provision, 1)
		frappe.db.set_single_value("SaaS Settings", "auto_provision_on_save", 0)
		self.assertEqual(get_settings().auto_provision, 0)

	def test_save_does_not_start_when_disabled(self):
		with mock.patch(ENQUEUE) as enqueue:
			make_site(prefix="manualstart", order="PROV-2")
		enqueue.assert_not_called()

	def test_start_button_queues_a_queued_site(self):
		doc = make_site(prefix="buttonstart", order="PROV-3")
		with mock.patch(ENQUEUE) as enqueue:
			doc.start_provisioning()
		enqueue.assert_called_once_with(doc.name)

	def test_start_button_rejected_while_running(self):
		doc = make_site(prefix="running", order="PROV-4")
		doc.flags.allow_status_change = True
		doc.status = "Creating"
		doc.save()
		with self.assertRaises(frappe.ValidationError):
			doc.start_provisioning()

	# ------------------------------------------------------------- failure and retry

	def test_failure_creates_linked_error_log_and_retry_clears_it(self):
		doc = make_site(prefix="willfail", order="PROV-5")
		doc.admin_password = "S3cretPw-xyz"
		doc.flags.allow_status_change = True
		doc.status = "Creating"
		doc.save()

		error = ProvisioningError("create_site", "bench new-site failed", "console says S3cretPw-xyz is bad")
		# fail() commits/rolls back like a real job; keep the test data transactional.
		with mock.patch.object(frappe.db, "commit"), mock.patch.object(frappe.db, "rollback"):
			Provisioner(doc).fail(error)

		doc.reload()
		self.assertEqual(doc.status, "Failed")
		self.assertEqual(doc.current_step, "create_site")
		self.assertEqual(doc.attempts, 1)
		self.assertTrue(doc.error_log)
		self.assertIn("create_site", doc.last_error)

		log = frappe.get_doc("Error Log", doc.error_log)
		self.assertIn("Step: create_site", log.error)
		self.assertIn("willfail.test.example.com", log.error)
		self.assertNotIn("S3cretPw-xyz", log.error)  # secrets never reach the Error Log

		with mock.patch(ENQUEUE) as enqueue:
			doc.start_provisioning()
		doc.reload()
		self.assertEqual(doc.status, "Queued")
		self.assertFalse(doc.error_log)
		self.assertFalse(doc.last_error)
		self.assertFalse(doc.failed_at)
		enqueue.assert_called_once_with(doc.name)
		self.assertTrue(frappe.db.exists("SaaS Event Log", {"customer_site": doc.name, "event": "Retry"}))

	def test_step_rows_cover_every_step(self):
		doc = make_site(prefix="steprows", order="PROV-6")
		doc.flags.allow_status_change = True
		doc.status = "Creating"
		doc.save()
		with mock.patch.object(frappe.db, "commit"):
			Provisioner(doc).ensure_step_rows()
		self.assertEqual([row.step for row in doc.steps], list(STEPS))

	# ------------------------------------------------------------- guards

	def test_failure_alerts_saas_admins_in_app(self):
		doc = make_site(prefix="alerted", order="PROV-9")
		doc.flags.allow_status_change = True
		doc.status = "Creating"
		doc.save()
		before = frappe.db.count("Notification Log", {"document_name": doc.name})
		with mock.patch.object(frappe.db, "commit"), mock.patch.object(frappe.db, "rollback"):
			Provisioner(doc).fail(ProvisioningError("create_site", "boom"))
		self.assertGreater(frappe.db.count("Notification Log", {"document_name": doc.name}), before)

	def test_refuses_to_drop_a_site_it_did_not_create(self):
		doc = make_site(prefix="notours", order="PROV-7")
		doc.flags.allow_status_change = True
		doc.status = "Creating"
		doc.save()
		with self.assertRaises(ProvisioningError):
			Provisioner(doc).drop_site()

	def test_admin_password_reveal_needs_a_password_and_is_logged(self):
		doc = make_site(prefix="revealpw", order="PROV-8")
		with self.assertRaises(frappe.ValidationError):
			doc.reveal_admin_password()
		doc.admin_password = "Temp-Pass-123"
		doc.save()
		self.assertEqual(doc.reveal_admin_password(), "Temp-Pass-123")
		self.assertTrue(frappe.db.exists("SaaS Event Log", {"customer_site": doc.name, "event": "Admin Action"}))

	# ------------------------------------------------------------- tenant helpers

	def test_seed_patch_list_is_parsed_without_comments(self):
		from bench_manager.saas.tenant import _patch_paths

		if "lesaas_hr" not in frappe.get_installed_apps():
			self.skipTest("lesaas_hr not installed")
		paths = _patch_paths("lesaas_hr")
		self.assertIn("lesaas_hr.patches.v1_0.seed_frontend_settings", paths)
		self.assertTrue(all(" " not in p and "#" not in p and not p.startswith("[") for p in paths))

	def test_tenant_runner_only_allows_listed_functions(self):
		from bench_manager.saas import tenant_runner

		for name in tenant_runner.ALLOWED:
			self.assertTrue(callable(tenant_runner.resolve(name)))
		for bad in ("os.system", "__import__", "frappe", "_patch_paths", "drop_everything"):
			with self.assertRaises(ValueError):
				tenant_runner.resolve(bad)

	# ------------------------------------------------------------- tenant: licence

	def test_apply_license_sets_limit_and_dates_without_type_errors(self):
		from bench_manager.saas import tenant

		if not frappe.db.exists("DocType", "License and Usage Info"):
			self.skipTest("foxerp is not installed")
		with mock.patch.object(frappe.db, "commit"):
			note = tenant.apply_license(max_users=1000, license_end_date="2031-06-30")
			lic = frappe.get_single("License and Usage Info")
			self.assertEqual(lic.allowed_user, 1000)
			self.assertEqual(str(lic.end_date)[:10], "2031-06-30")
			self.assertIn("user limit 1000", note)

			# a second run now has a stored end date that loads as text: must still not fail
			tenant.apply_license(max_users=1000)
			self.assertEqual(frappe.db.get_single_value("Website Settings", "disable_signup"), 1)

	def test_apply_license_without_a_limit_says_so(self):
		from bench_manager.saas import tenant

		if not frappe.db.exists("DocType", "License and Usage Info"):
			self.skipTest("foxerp is not installed")
		with mock.patch.object(frappe.db, "commit"):
			self.assertIn("NOT enforced", tenant.apply_license())
