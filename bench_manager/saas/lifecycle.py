# -*- coding: utf-8 -*-
# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

"""Stop and resume a customer site.

Stopping sets maintenance_mode in the customer site's site_config.json: every web and API request gets
Frappe's 503 page and the site's scheduler pauses. No data is touched, the bench CLI (backups, migrate)
still works, and resuming just removes the flag.

Stop is a background job (a backup can take minutes). The record only becomes Stopped once the flag is
really set, so the status never claims something that did not happen. Resume is instant.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

from bench_manager.saas import site_config
from bench_manager.saas.alerts import notify_admins
from bench_manager.saas.commands import CommandError, backup_site
from bench_manager.saas.events import log_event
from bench_manager.saas.utils import ADMIN_ROLES, SaaSAPIError, get_settings

STOPPABLE_FROM_DESK = ("Awaiting Domain Mapping", "Ready", "Published")
IN_PROGRESS = ("Pending", "Backing up")


def enqueue_stop(site_name):
	frappe.enqueue(
		"bench_manager.saas.lifecycle.run_stop",
		queue="long",
		timeout=3600,
		job_id=f"saas_stop::{site_name}",
		deduplicate=True,
		enqueue_after_commit=True,
		site_name=site_name,
	)


def request_stop(doc, reason, source, by, allowed_statuses=STOPPABLE_FROM_DESK):
	"""Validate and queue a stop. Returns "stopped" (already), "stopping" or raises SaaSAPIError."""
	reason = (reason or "").strip()
	if len(reason) < 5:
		raise SaaSAPIError("invalid_reason", _("A reason of at least 5 characters is required."), 422, "reason")
	if len(reason) > 140:
		raise SaaSAPIError("invalid_reason", _("The reason is too long (140 characters at most)."), 422, "reason")

	if doc.status == "Stopped":
		return "stopped"
	if doc.stop_state in IN_PROGRESS:
		return "stopping"
	if doc.status not in allowed_statuses:
		raise SaaSAPIError(
			"not_stoppable",
			_("A site with status {0} cannot be stopped.").format(doc.status),
			409,
		)

	frappe.db.set_value(
		"Customer Site",
		doc.name,
		{"stop_state": "Pending", "stop_reason": reason, "stopped_by": by, "stop_error_log": None, "backup_status": None},
	)
	log_event("Stopped", source, doc.name, doc.marketing_order_id, detail=f"Stop requested by {by}: {reason}"[:500])
	enqueue_stop(doc.name)
	return "stopping"


def run_stop(site_name):
	"""Background job: backup (if enabled), set maintenance_mode, then mark the record Stopped."""
	frappe.set_user("Administrator")
	doc = frappe.get_doc("Customer Site", site_name)
	if doc.status == "Stopped" or doc.stop_state != "Pending":
		return

	settings = get_settings()
	try:
		if settings.backup_before_stop:
			_set(site_name, stop_state="Backing up")
			try:
				backup_site(site_name)
				_set(site_name, backup_status="OK", last_backup=_latest_backup(site_name))
			except CommandError as e:
				_set(site_name, backup_status="Failed")
				_backup_failed(doc, e)
				if settings.backup_failure_policy == "Abort stop":
					_set(site_name, stop_state="Failed")
					return

		site_config.set_flag(site_name, "maintenance_mode", 1)

		doc.reload()
		doc.flags.allow_status_change = True
		doc.flags.ignore_permissions = True
		doc.status = "Stopped"
		doc.save()
		_set(site_name, stop_state=None)
	except Exception as e:
		_stop_failed(doc, e)


def request_resume(doc, source, by):
	"""Remove maintenance_mode and restore the status the site had before it was stopped."""
	if doc.stop_state in IN_PROGRESS:
		raise SaaSAPIError("stop_in_progress", _("The site is still being stopped. Try again shortly."), 409)
	if doc.status != "Stopped":
		raise SaaSAPIError("not_stopped", _("The site is not stopped."), 409)

	site_config.set_flag(doc.name, "maintenance_mode", None)

	doc.flags.allow_status_change = True
	doc.flags.ignore_permissions = True
	doc.status = doc.status_before_stop or "Published"
	doc.stop_state = None
	doc.save()
	log_event("Resumed", source, doc.name, doc.marketing_order_id, detail=f"Resumed by {by}")
	return doc.status


# ------------------------------------------------------------------ helpers


def _set(site_name, **values):
	frappe.db.set_value("Customer Site", site_name, values)
	frappe.db.commit()


def _latest_backup(site_name):
	"""Path of the newest database backup, relative to the bench."""
	import glob
	import os

	from frappe.utils import get_bench_path

	pattern = os.path.join(get_bench_path(), "sites", site_name, "private", "backups", "*-database.sql.gz")
	files = sorted(glob.glob(pattern), key=os.path.getmtime)
	return os.path.relpath(files[-1], get_bench_path()) if files else None


def _error_log(doc, title, message, details):
	return frappe.get_doc(
		{
			"doctype": "Error Log",
			"method": f"{title}: {doc.name}"[:140],
			"error": f"Site: {doc.name}\nMarketing order: {doc.marketing_order_id}\nTime: {now_datetime()}\n\nError: {message}\n\n{details}",
		}
	).insert(ignore_permissions=True)


def _backup_failed(doc, error):
	"""The backup failed. With policy 'Stop anyway' the stop continues; either way admins are told."""
	log = _error_log(doc, "Backup before stop failed", error.message, error.details)
	policy = get_settings().backup_failure_policy
	frappe.db.set_value("Customer Site", doc.name, "stop_error_log", log.name)
	frappe.db.commit()
	notify_admins(
		_("Backup before stopping {0} failed").format(doc.name),
		_("The backup failed ({0}). Policy: {1}. See the Error Log linked on the Customer Site.").format(error.message, policy),
		doc.name,
	)
	frappe.db.commit()


def _stop_failed(doc, error):
	frappe.db.rollback()
	log = _error_log(doc, "Stopping site failed", str(error), frappe.get_traceback())
	frappe.db.set_value("Customer Site", doc.name, {"stop_state": "Failed", "stop_error_log": log.name})
	frappe.db.commit()
	notify_admins(
		_("Stopping {0} failed").format(doc.name),
		_("The site is still running. {0}\nSee the Error Log linked on the Customer Site.").format(error),
		doc.name,
	)
	frappe.db.commit()
