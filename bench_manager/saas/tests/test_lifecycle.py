# -*- coding: utf-8 -*-
# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import json
import os
import tempfile
from contextlib import ExitStack
from unittest import mock

import frappe
import requests
from frappe.tests import IntegrationTestCase

from bench_manager.bench_manager.doctype.customer_site.test_customer_site import make_published, make_site
from bench_manager.saas import api, domain, lifecycle, publish, site_config
from bench_manager.saas.commands import CommandError
from bench_manager.saas.publish import PublishError

CUSTOMER_SITE = "bench_manager.bench_manager.doctype.customer_site.customer_site"


class LifecycleCase(IntegrationTestCase):
	"""Common setup: nothing may start a real job, send mail or touch a real site."""

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.local.response = frappe._dict()
		frappe.db.set_single_value("SaaS Settings", "base_domain", "test.example.com")
		frappe.db.set_single_value("SaaS Settings", "auto_provision_on_save", 0)
		frappe.db.set_single_value("SaaS Settings", "domain_check_base_url", "http://{site}:8000")
		for target in (f"{CUSTOMER_SITE}.enqueue_publish", "bench_manager.saas.alerts.notify_admins"):
			patcher = mock.patch(target)
			patcher.start()
			self.addCleanup(patcher.stop)
		for name in ("commit", "rollback"):  # jobs commit/rollback; keep test data transactional
			patcher = mock.patch.object(frappe.db, name)
			patcher.start()
			self.addCleanup(patcher.stop)

	def awaiting_site(self, prefix, order):
		doc = make_site(prefix=prefix, order=order)
		for status in ("Creating", "Configuring", "Awaiting Domain Mapping"):
			doc.flags.allow_status_change = True
			doc.status = status
			doc.save()
		return doc


def response(status=200, text=""):
	r = mock.Mock()
	r.status_code, r.text = status, text
	return r


class TestDomainCheck(LifecycleCase):
	def test_probe_url_uses_the_configured_base(self):
		self.assertEqual(domain.probe_url("a.test.example.com"), "http://a.test.example.com:8000/files/saas-domain-check.txt")

	def test_probe_outcomes(self):
		get = "bench_manager.saas.domain.requests.get"
		cases = [
			(dict(return_value=response(200, "tok\n")), True, "ok"),
			(dict(return_value=response(200, "someone else")), False, "wrong_site"),
			(dict(return_value=response(404)), False, "http_404"),
			(dict(side_effect=requests.exceptions.SSLError("bad cert")), False, "ssl"),
			(dict(side_effect=requests.exceptions.Timeout()), False, "timeout"),
			(dict(side_effect=requests.exceptions.ConnectionError("Name or service not known")), False, "dns"),
			(dict(side_effect=requests.exceptions.ConnectionError("refused")), False, "connection"),
		]
		for kwargs, ok, stage in cases:
			with mock.patch(get, **kwargs):
				result = domain.probe("a.test.example.com", "tok")
			self.assertEqual((result["ok"], result["stage"]), (ok, stage), stage)

	def test_check_domain_records_result_and_never_changes_status(self):
		doc = self.awaiting_site("domaincheck", "DOM-1")
		with mock.patch.object(domain, "write_token_file"), mock.patch.object(
			domain, "probe", return_value={"ok": True, "stage": "ok", "message": "fine"}
		):
			domain.check_domain(doc)
		doc.reload()
		self.assertEqual(doc.domain_check_status, "Reachable")
		self.assertTrue(doc.domain_checked_on)
		self.assertTrue(doc.domain_check_token)
		self.assertEqual(doc.status, "Awaiting Domain Mapping")

	def test_scheduler_alerts_once_and_never_changes_status(self):
		doc = self.awaiting_site("sched", "DOM-2")
		ok = {"ok": True, "stage": "ok", "message": "fine"}
		with mock.patch.object(domain, "check_domain", return_value=ok), mock.patch(
			"bench_manager.saas.alerts.notify_admins"
		) as notify:
			domain.check_pending_domains()
			domain.check_pending_domains()
		alerted = [call.args[2] for call in notify.call_args_list]
		self.assertEqual(alerted.count(doc.name), 1)  # once per site, however often the job runs
		self.assertEqual(frappe.db.get_value("Customer Site", doc.name, "status"), "Awaiting Domain Mapping")


class TestMarkReady(LifecycleCase):
	def test_ready_only_when_domain_answers(self):
		doc = self.awaiting_site("markready", "RDY-1")
		bad = {"ok": False, "stage": "dns", "message": "does not resolve"}
		with mock.patch.object(domain, "check_domain", return_value=bad):
			with self.assertRaises(frappe.ValidationError):
				doc.mark_ready()
		self.assertEqual(frappe.db.get_value("Customer Site", doc.name, "status"), "Awaiting Domain Mapping")

		good = {"ok": True, "stage": "ok", "message": "fine"}
		with mock.patch.object(domain, "check_domain", return_value=good):
			doc.reload()
			self.assertEqual(doc.mark_ready(), "Ready")
		doc.reload()
		self.assertEqual(doc.verified_by, "Administrator")
		self.assertTrue(doc.verified_on)

	def test_skip_needs_a_reason_and_is_logged(self):
		doc = self.awaiting_site("skipcheck", "RDY-2")
		with self.assertRaises(frappe.ValidationError):
			doc.mark_ready(skip_check=1, reason="")
		self.assertEqual(doc.mark_ready(skip_check=1, reason="local test, no DNS"), "Ready")
		log = frappe.get_all("SaaS Event Log", filters={"customer_site": doc.name, "event": "Admin Action"}, pluck="detail")
		self.assertTrue(any("skipped: local test, no DNS" in d for d in log))

	def test_only_from_awaiting_domain_mapping(self):
		doc = make_site(prefix="wrongstate", order="RDY-3")
		with self.assertRaises(frappe.ValidationError):
			doc.mark_ready(skip_check=1, reason="not allowed from Queued")


class TestPublish(LifecycleCase):
	def run_publish(self, doc, resend=False, send=None):
		with ExitStack() as stack:
			mocks = {
				"link": stack.enter_context(
					mock.patch.object(publish, "_make_link", return_value="https://x.test/update-password?key=SECRETKEY")
				),
				"set_flag": stack.enter_context(mock.patch.object(publish.site_config, "set_flag")),
				"run": stack.enter_context(mock.patch.object(publish, "run_on_site")),
				"remove": stack.enter_context(mock.patch.object(publish.domain, "remove_token_file")),
				"send": stack.enter_context(mock.patch.object(publish, "_send_email", side_effect=send)),
			}
			publish.publish_site(doc.name, resend=resend)
		return mocks

	def test_success_sends_email_then_replaces_admin_password(self):
		doc = make_published("pubok", "PUB-1")
		doc.admin_password = "TempPass-123"
		doc.save()
		m = self.run_publish(doc)

		m["send"].assert_called_once()
		m["set_flag"].assert_called_with(doc.name, "host_name", "http://pubok.test.example.com:8000")
		self.assertEqual(m["run"].call_args.args[1], "rotate_admin_password")
		doc.reload()
		self.assertTrue(doc.email_sent)
		self.assertTrue(doc.admin_password_rotated)
		self.assertFalse(doc.get_password("admin_password", raise_exception=False))
		self.assertFalse(doc.publish_error)

	def test_publish_job_is_idempotent_and_resend_keeps_the_admin_password(self):
		doc = make_published("pubrep", "PUB-2")
		self.run_publish(doc)
		again = self.run_publish(doc)
		again["send"].assert_not_called()

		resent = self.run_publish(doc, resend=True)
		resent["send"].assert_called_once()
		resent["run"].assert_not_called()  # already rotated, never rotated twice

	def test_email_failure_keeps_temporary_admin_password_and_links_error_log(self):
		doc = make_published("pubfail", "PUB-3")
		doc.admin_password = "TempPass-456"
		doc.save()
		with mock.patch("bench_manager.saas.publish.notify_admins") as notify:
			m = self.run_publish(doc, send=PublishError("email", "No outgoing Email Account is enabled."))
		m["run"].assert_not_called()  # the temporary password stays so the team can still log in
		notify.assert_called_once()
		doc.reload()
		self.assertFalse(doc.email_sent)
		self.assertFalse(doc.admin_password_rotated)
		self.assertTrue(doc.publish_error_log)
		self.assertIn("email", doc.publish_error)
		self.assertEqual(doc.get_password("admin_password"), "TempPass-456")
		self.assertNotIn("SECRETKEY", frappe.get_doc("Error Log", doc.publish_error_log).error)

	def test_resend_button_only_for_published(self):
		doc = make_site(prefix="notpub", order="PUB-4")
		with self.assertRaises(frappe.ValidationError):
			doc.resend_publish_email()
		published = make_published("ispub", "PUB-5")
		with mock.patch(f"{CUSTOMER_SITE}.enqueue_publish") as enqueue:
			published.resend_publish_email()
		enqueue.assert_called_once_with(published.name, resend=True)


class StopCase(LifecycleCase):
	def setUp(self):
		super().setUp()
		frappe.db.set_single_value("SaaS Settings", "backup_before_stop", 1)
		frappe.db.set_single_value("SaaS Settings", "backup_failure_policy", "Stop anyway")
		for target, kw in (
			("bench_manager.saas.lifecycle.enqueue_stop", {}),
			("bench_manager.saas.lifecycle._latest_backup", {"return_value": "sites/x/private/backups/b-database.sql.gz"}),
		):
			patcher = mock.patch(target, **kw)
			setattr(self, target.rsplit(".", 1)[1], patcher.start())
			self.addCleanup(patcher.stop)

	def stop(self, doc, backup=None, flag=None):
		"""Request and run a stop with the backup and the config write mocked."""
		lifecycle.request_stop(doc, "subscription ended", "Marketing API", "marketing-api", allowed_statuses=("Published",))
		with mock.patch.object(lifecycle, "backup_site", **(backup or {})) as backup_site, mock.patch.object(
			lifecycle.site_config, "set_flag", **(flag or {})
		) as set_flag:
			lifecycle.run_stop(doc.name)
		return backup_site, set_flag


class TestStopResume(StopCase):
	def test_stop_backs_up_sets_maintenance_mode_then_marks_stopped(self):
		doc = make_published("stopok", "STP-1")
		backup, set_flag = self.stop(doc)
		backup.assert_called_once_with(doc.name)
		set_flag.assert_called_once_with(doc.name, "maintenance_mode", 1)
		doc.reload()
		self.assertEqual(doc.status, "Stopped")
		self.assertTrue(doc.is_stopped)
		self.assertEqual(doc.status_before_stop, "Published")
		self.assertEqual(doc.backup_status, "OK")
		self.assertTrue(doc.last_backup)
		self.assertFalse(doc.stop_state)
		self.assertEqual(doc.stopped_by, "marketing-api")

	def test_no_backup_when_switched_off(self):
		frappe.db.set_single_value("SaaS Settings", "backup_before_stop", 0)
		doc = make_published("nobackup", "STP-2")
		backup, _ = self.stop(doc)
		backup.assert_not_called()
		self.assertEqual(frappe.db.get_value("Customer Site", doc.name, "status"), "Stopped")

	def test_failed_backup_still_stops_by_default_and_alerts(self):
		doc = make_published("stopanyway", "STP-3")
		with mock.patch("bench_manager.saas.lifecycle.notify_admins") as notify:
			self.stop(doc, backup={"side_effect": CommandError("bench backup failed", "disk full")})
		notify.assert_called_once()
		doc.reload()
		self.assertEqual(doc.status, "Stopped")
		self.assertEqual(doc.backup_status, "Failed")
		self.assertTrue(doc.stop_error_log)

	def test_failed_backup_aborts_when_policy_says_so(self):
		frappe.db.set_single_value("SaaS Settings", "backup_failure_policy", "Abort stop")
		doc = make_published("abortstop", "STP-4")
		_, set_flag = self.stop(doc, backup={"side_effect": CommandError("bench backup failed", "disk full")})
		set_flag.assert_not_called()
		doc.reload()
		self.assertEqual(doc.status, "Published")
		self.assertEqual(doc.stop_state, "Failed")

	def test_config_failure_leaves_site_running_and_says_so(self):
		doc = make_published("cfgfail", "STP-5")
		self.stop(doc, flag={"side_effect": OSError("read-only file system")})
		doc.reload()
		self.assertEqual(doc.status, "Published")  # the status never claims a stop that did not happen
		self.assertEqual(doc.stop_state, "Failed")
		self.assertTrue(doc.stop_error_log)

	def test_stop_rules(self):
		doc = make_published("rules", "STP-6")
		with self.assertRaises(api.SaaSAPIError) as ctx:
			lifecycle.request_stop(doc, "no", "Marketing API", "m")
		self.assertEqual(ctx.exception.error_code, "invalid_reason")

		ready = make_site(prefix="notyet", order="STP-7")
		with self.assertRaises(api.SaaSAPIError) as ctx:
			lifecycle.request_stop(ready, "subscription ended", "Marketing API", "m", allowed_statuses=("Published",))
		self.assertEqual(ctx.exception.error_code, "not_stoppable")

		self.assertEqual(lifecycle.request_stop(doc, "subscription ended", "Marketing API", "m"), "stopping")
		doc.reload()
		self.assertEqual(lifecycle.request_stop(doc, "subscription ended", "Marketing API", "m"), "stopping")
		self.enqueue_stop.assert_called_once_with(doc.name)  # the second request did not queue another job

	def test_resume_restores_previous_status_and_removes_maintenance_mode(self):
		doc = make_published("resumeok", "STP-8")
		self.stop(doc)
		doc.reload()
		with mock.patch.object(lifecycle.site_config, "set_flag") as set_flag:
			status = lifecycle.request_resume(doc, "Admin User", "admin")
		set_flag.assert_called_once_with(doc.name, "maintenance_mode", None)
		self.assertEqual(status, "Published")
		doc.reload()
		self.assertFalse(doc.is_stopped)
		self.assertTrue(doc.resumed_on)

	def test_resume_rules(self):
		doc = make_published("resumerules", "STP-9")
		with self.assertRaises(api.SaaSAPIError) as ctx:
			lifecycle.request_resume(doc, "Admin User", "admin")
		self.assertEqual(ctx.exception.error_code, "not_stopped")

		lifecycle.request_stop(doc, "subscription ended", "Marketing API", "m")
		doc.reload()
		with self.assertRaises(api.SaaSAPIError) as ctx:
			lifecycle.request_resume(doc, "Admin User", "admin")
		self.assertEqual(ctx.exception.error_code, "stop_in_progress")

	def test_desk_buttons_need_a_reason_and_valid_state(self):
		doc = make_published("deskstop", "STP-10")
		with self.assertRaises(frappe.ValidationError):
			doc.stop_site("")
		self.assertEqual(doc.stop_site("customer asked to pause"), "stopping")


class TestStopResumeApi(StopCase):
	"""The same lifecycle, through the marketing endpoints."""

	def test_stop_endpoint(self):
		doc = make_published("apistop", "API-1")
		res = api.stop_site(marketing_order_id="API-1", reason="subscription ended")
		self.assertEqual((res["ok"], res["state"]), (True, "stopping"))
		self.assertEqual(frappe.local.response["http_status_code"], 202)
		self.assertEqual(api.get_site_status(marketing_order_id="API-1")["state"], "stopping")

		self.stop(doc)  # the job runs
		status = api.get_site_status(marketing_order_id="API-1")
		self.assertEqual((status["state"], status["stopped"], status["site_url"]), ("stopped", True, None))

		again = api.stop_site(marketing_order_id="API-1", reason="subscription ended")  # idempotent
		self.assertEqual(again["state"], "stopped")

	def test_stop_endpoint_errors(self):
		make_site(prefix="apinotpub", order="API-2")
		cases = [
			(dict(marketing_order_id="API-2", reason="subscription ended"), "not_stoppable", 409),
			(dict(marketing_order_id="NOPE", reason="subscription ended"), "not_found", 404),
			(dict(reason="subscription ended"), "missing_parameter", 422),
		]
		make_published("apipub", "API-3")
		cases.append((dict(marketing_order_id="API-3", reason=""), "invalid_reason", 422))
		for kwargs, code, http in cases:
			frappe.local.response = frappe._dict()
			res = api.stop_site(**kwargs)
			self.assertEqual((res["error"]["code"], frappe.local.response["http_status_code"]), (code, http), code)

	def test_resume_endpoint(self):
		doc = make_published("apiresume", "API-4")
		running = api.resume_site(marketing_order_id="API-4")  # not stopped: nothing to do
		self.assertEqual((running["ok"], running["state"]), (True, "published"))

		self.stop(doc)
		with mock.patch.object(lifecycle.site_config, "set_flag"):
			res = api.resume_site(marketing_order_id="API-4")
		self.assertEqual((res["ok"], res["state"], res["stopped"]), (True, "published", False))

		unfinished = make_site(prefix="apiunfinished", order="API-5")
		res = api.resume_site(marketing_order_id=unfinished.marketing_order_id)
		self.assertEqual(res["error"]["code"], "not_stopped")


class TestSiteConfig(IntegrationTestCase):
	def test_set_and_remove_flag_keeps_other_keys(self):
		with tempfile.TemporaryDirectory() as folder:
			path = os.path.join(folder, "site_config.json")
			with open(path, "w") as f:
				json.dump({"db_name": "x", "limits": {"users": 5}}, f)
			with mock.patch.object(site_config, "_config_path", return_value=path):
				site_config.set_flag("a.example.com", "maintenance_mode", 1)
				self.assertEqual(site_config.get_flag("a.example.com", "maintenance_mode"), 1)
				site_config.set_flag("a.example.com", "maintenance_mode", None)
				self.assertIsNone(site_config.get_flag("a.example.com", "maintenance_mode"))
			with open(path) as f:
				self.assertEqual(json.load(f), {"db_name": "x", "limits": {"users": 5}})
			self.assertEqual([x for x in os.listdir(folder) if x.endswith(".tmp")], [])

	def test_only_plain_site_names_are_accepted(self):
		for bad in ("../etc", "a/b", "", "a b"):
			with self.assertRaises(frappe.ValidationError, msg=bad):
				site_config.get_flag(bad, "maintenance_mode")
