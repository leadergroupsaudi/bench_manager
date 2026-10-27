# -*- coding: utf-8 -*-
# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

"""What happens after a site is published (the Publish to Customer checkbox).

1. Point the customer site at its public URL (host_name) so links are correct.
2. Create a password-setup link for the first user (kept in memory only).
3. Email it to the customer.
4. Only after the email was sent: give Administrator a random password and forget the temporary one.

If something fails, the site stays Published, an Error Log is linked on the record and the admins are
alerted. "Resend Publish Email" tries again. A resend creates a new link and never touches Administrator.
"""

import os

import frappe
from frappe import _
from frappe.utils import get_bench_path, now_datetime

from bench_manager.saas import domain, site_config
from bench_manager.saas.alerts import notify_admins
from bench_manager.saas.commands import CommandError, run_on_site, tail
from bench_manager.saas.events import log_event
from bench_manager.saas.utils import get_settings

DEFAULT_SUBJECT = "Your {{ company_name }} workspace is ready"
DEFAULT_BODY = """
<p>Hello {{ first_user_name }},</p>
<p>Your workspace <b>{{ company_name }}</b> is ready at
<a href="{{ site_url }}">{{ site_url }}</a>.</p>
<p>To start, set your password using this link. It works for {{ validity_days }} day(s):</p>
<p><a href="{{ link }}">{{ link }}</a></p>
<p>Your username is <b>{{ first_user_email }}</b>.</p>
"""


class PublishError(Exception):
	def __init__(self, stage, message, details=""):
		super().__init__(message)
		self.stage = stage
		self.message = message
		self.details = details


def enqueue_publish(site_name, resend=False):
	frappe.enqueue(
		"bench_manager.saas.publish.publish_site",
		queue="default",
		timeout=900,
		job_id=f"saas_publish::{site_name}",
		deduplicate=True,
		enqueue_after_commit=True,
		site_name=site_name,
		resend=resend,
	)


def publish_site(site_name, resend=False):
	"""Background job."""
	frappe.set_user("Administrator")
	doc = frappe.get_doc("Customer Site", site_name)
	if doc.status != "Published":
		return
	if doc.email_sent and not resend:
		return  # already done; the Publish checkbox cannot be repeated, only Resend

	stage = "prepare"
	try:
		settings = get_settings()
		site_url = settings.domain_check_base_url.format(site=site_name)

		stage = "link"
		site_config.set_flag(site_name, "host_name", site_url)
		link = _make_link(doc, settings.password_link_validity_days)

		stage = "email"
		_send_email(doc, link, site_url, settings)
		_set(doc, email_sent=1, email_sent_on=now_datetime(), publish_error=None, publish_error_log=None)
		log_event("Email Sent", "Scheduler", site_name, doc.marketing_order_id, detail=f"Publish email to {doc.first_user_email}")

		if not doc.admin_password_rotated:
			stage = "rotate_password"
			run_on_site(site_name, "rotate_admin_password", {}, tag="rotate")
			frappe.db.set_value("Customer Site", site_name, "admin_password_rotated", 1)
			from frappe.utils.password import remove_encrypted_password

			remove_encrypted_password("Customer Site", site_name, "admin_password")
			log_event("Admin Action", "Scheduler", site_name, doc.marketing_order_id, detail="Administrator password replaced; temporary one forgotten")

		stage = "cleanup"
		domain.remove_token_file(site_name)
		frappe.db.commit()
	except Exception as e:
		_fail(doc, stage, e)


def _make_link(doc, validity_days):
	path = os.path.join(get_bench_path(), "sites", doc.name, "private", "saas_link.txt")
	try:
		run_on_site(
			doc.name,
			"make_reset_link",
			{"email": doc.first_user_email, "output_file": path, "validity_days": validity_days},
			tag="reset-link",
		)
		with open(path) as f:
			return f.read().strip()
	except CommandError:
		raise
	finally:
		if os.path.exists(path):
			os.remove(path)


def _send_email(doc, link, site_url, settings):
	if not frappe.db.exists("Email Account", {"enable_outgoing": 1}):
		raise PublishError("email", _("No outgoing Email Account is enabled on the admin site."))

	context = {
		"first_user_name": doc.first_user_full_name,
		"first_user_email": doc.first_user_email,
		"customer_name": doc.customer_name,
		"company_name": doc.company_name,
		"site_name": doc.name,
		"site_url": site_url,
		"link": link,
		"validity_days": settings.password_link_validity_days,
	}
	subject, body = DEFAULT_SUBJECT, DEFAULT_BODY
	template = frappe.db.get_single_value("SaaS Settings", "publish_email_template")
	if template:
		t = frappe.get_doc("Email Template", template)
		subject = t.subject or subject
		body = (t.response_html if t.use_html else t.response) or body

	sender = None
	account = frappe.db.get_single_value("SaaS Settings", "email_account")
	if account:
		sender = frappe.db.get_value("Email Account", account, "email_id")

	frappe.sendmail(
		recipients=[doc.first_user_email],
		sender=sender or "",
		subject=frappe.render_template(subject, context),
		message=frappe.render_template(body, context),
		reference_doctype="Customer Site",
		reference_name=doc.name,
		now=True,
	)
	_forget_link_in_email_queue(doc.name, link)


def _forget_link_in_email_queue(site_name, link):
	"""The sent message sits in Email Queue; blank out the link there once the email has gone."""
	try:
		frappe.db.sql(
			"""update `tabEmail Queue` set message = replace(message, %s, %s)
			where reference_doctype = 'Customer Site' and reference_name = %s and status = 'Sent'""",
			(link, "[link removed after sending]", site_name),
		)
	except Exception:
		frappe.log_error(title="Could not clear password link from Email Queue", message=frappe.get_traceback())


def _set(doc, **values):
	frappe.db.set_value("Customer Site", doc.name, values)
	doc.update(values)
	frappe.db.commit()


def _fail(doc, stage, error):
	frappe.db.rollback()
	if isinstance(error, (PublishError, CommandError)):
		message, details = error.message, getattr(error, "details", "") or ""
	else:
		message, details = str(error), frappe.get_traceback()
	stage = getattr(error, "stage", stage)

	text = "\n".join(
		[
			f"Site: {doc.name}",
			f"Marketing order: {doc.marketing_order_id}",
			f"Publish stage: {stage}",
			f"Time: {now_datetime()}",
			"",
			f"Error: {message}",
			"",
			tail(details, (doc.get_password("admin_password", raise_exception=False),)),
		]
	)
	error_log = frappe.get_doc(
		{"doctype": "Error Log", "method": f"Publish failed: {doc.name} [{stage}]"[:140], "error": text}
	).insert(ignore_permissions=True)
	frappe.db.set_value(
		"Customer Site",
		doc.name,
		{"publish_error": f"[{stage}] {message}"[:500], "publish_error_log": error_log.name},
	)
	log_event("Email Sent", "Scheduler", doc.name, doc.marketing_order_id, result="Error", detail=f"{stage}: {message}"[:500])
	frappe.db.commit()
	notify_admins(
		_("Publishing {0} is incomplete").format(doc.name),
		_("Stage '{0}' failed: {1}\nThe site is published but the customer may not have been notified. Fix the cause, then use Resend Publish Email.").format(stage, message),
		doc.name,
	)
	frappe.db.commit()
