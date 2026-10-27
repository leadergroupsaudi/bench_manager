# -*- coding: utf-8 -*-
# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

"""Alerts for the people running the SaaS side: in-app notifications to SaaS Admins, plus email to the
addresses in SaaS Settings > Alert Recipients (when outgoing email is set up). Never raises: an alert
that cannot be delivered must not break the operation it reports on."""

import re

import frappe


def notify_admins(subject, message, customer_site=None):
	try:
		_notify_in_app(subject, message, customer_site)
	except Exception:
		frappe.log_error(title="SaaS alert: in-app notification failed", message=frappe.get_traceback())
	try:
		_notify_by_email(subject, message)
	except Exception:
		frappe.log_error(title="SaaS alert: email failed", message=frappe.get_traceback())


def get_alert_recipients():
	raw = frappe.db.get_single_value("SaaS Settings", "alert_recipients") or ""
	return [e.strip() for e in re.split(r"[\s,;]+", raw) if e.strip()]


def get_admin_users():
	users = set(frappe.get_all("Has Role", filters={"role": "SaaS Admin", "parenttype": "User"}, pluck="parent"))
	users.add("Administrator")
	return [u for u in users if frappe.db.get_value("User", u, "enabled")]


def _notify_in_app(subject, message, customer_site):
	for user in get_admin_users():
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"for_user": user,
				"type": "Alert",
				"subject": subject,
				"email_content": message,
				"document_type": "Customer Site" if customer_site else None,
				"document_name": customer_site,
			}
		).insert(ignore_permissions=True)


def _notify_by_email(subject, message):
	recipients = get_alert_recipients()
	if not recipients or frappe.flags.in_test:
		return
	if not frappe.db.exists("Email Account", {"enable_outgoing": 1}):
		return  # email is not set up yet; the in-app notification still went out
	frappe.sendmail(recipients=recipients, subject=subject, message=frappe.utils.escape_html(message).replace("\n", "<br>"))
