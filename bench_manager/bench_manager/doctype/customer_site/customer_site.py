# -*- coding: utf-8 -*-
# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, add_years, cint, getdate, now_datetime, validate_email_address

from bench_manager.saas import domain, lifecycle
from bench_manager.saas.events import log_event
from bench_manager.saas.provisioner import enqueue_provisioning
from bench_manager.saas.publish import enqueue_publish
from bench_manager.saas.utils import (
	ADMIN_ROLES,
	SaaSAPIError,
	build_site_name,
	get_chart_options,
	get_default_fiscal_year,
	get_default_timezone,
	get_settings,
	is_site_name_taken,
	is_valid_timezone,
	validate_base_domain,
	validate_prefix,
)

# status -> statuses it may move to. "Stopped" can only return to the status it was stopped from
# (checked separately). "Published" is reached only through the Publish to Customer checkbox.
ALLOWED_TRANSITIONS = {
	"Queued": {"Creating", "Failed"},
	"Creating": {"Configuring", "Failed"},
	"Configuring": {"Awaiting Domain Mapping", "Failed"},
	"Awaiting Domain Mapping": {"Ready", "Failed", "Stopped"},
	"Ready": {"Published", "Awaiting Domain Mapping", "Stopped"},
	"Published": {"Stopped"},
	"Stopped": {"Awaiting Domain Mapping", "Ready", "Published"},
	"Failed": {"Queued", "Creating", "Configuring"},
}

LOCKED_FROM_DELETE = ("Published", "Stopped")


class CustomerSite(Document):
	def autoname(self):
		# Runs before validate on insert, so the prefix is normalised and checked here.
		self.set_site_identity()
		self.name = self.site_name

	def validate(self):
		self.validate_emails()
		self.validate_locale()
		self.set_financial_year_end()
		self.validate_publish()
		self.validate_status_transition()
		self.sync_stop_fields()
		self.clear_error_on_retry()

	def after_insert(self):
		if not self.flags.from_api:
			log_event("Record Created", "Admin User", self.name, self.marketing_order_id, detail=self.status)
		# Saving a new record starts building its site (unless switched off in SaaS Settings).
		if self.status == "Queued" and get_settings().auto_provision:
			enqueue_provisioning(self.name)

	def on_update(self):
		if self.flags.just_published:
			enqueue_publish(self.name)  # email the password-setup link, replace the temporary admin password
		before = self.get_doc_before_save()
		if before and before.status == "Failed" and self.status == "Queued":
			log_event("Retry", "Admin User", self.name, self.marketing_order_id, detail="Previous error cleared")
			enqueue_provisioning(self.name)
		if before and before.status != self.status:
			event = {"Published": "Published", "Stopped": "Stopped"}.get(self.status, "Status Changed")
			if before.status == "Stopped":
				event = "Resumed"
			log_event(
				event,
				"Admin User",
				self.name,
				self.marketing_order_id,
				detail=f"{before.status} -> {self.status}",
			)

	def on_trash(self):
		if self.status in LOCKED_FROM_DELETE or self.publish_to_customer:
			frappe.throw(_("A published or stopped site record cannot be deleted."))

	# ------------------------------------------------------------------ helpers

	def set_site_identity(self):
		settings = get_settings()
		try:
			validate_base_domain(settings.base_domain)
			self.prefix = validate_prefix(self.prefix, settings)
		except SaaSAPIError as e:
			frappe.throw(e.message, title=_("Invalid Site Name"))

		self.base_domain = self.base_domain or settings.base_domain
		self.site_name = build_site_name(self.prefix, self.base_domain)
		self.site_url = f"https://{self.site_name}"

		if is_site_name_taken(self.site_name):
			frappe.throw(_("Site {0} already exists.").format(self.site_name), frappe.DuplicateEntryError)

	def validate_emails(self):
		for field in ("customer_email", "first_user_email"):
			if self.get(field):
				self.set(field, validate_email_address(self.get(field), throw=True))

	def validate_locale(self):
		"""Timezone follows the country; language and chart of accounts must be ones the wizard offers."""
		if not self.timezone:
			self.timezone = get_default_timezone(self.country)
		if not self.timezone:
			frappe.throw(_("No default timezone is known for {0}.").format(self.country))
		if not is_valid_timezone(self.timezone):
			frappe.throw(_("Unknown timezone {0}.").format(self.timezone))

		if not frappe.db.get_value("Language", self.language, "enabled"):
			frappe.throw(_("Language {0} is not enabled.").format(self.language))

		if self.chart_of_accounts not in get_chart_options(self.country):
			frappe.throw(
				_("Chart of Accounts {0} is not available for {1}.").format(self.chart_of_accounts, self.country)
			)

	def set_financial_year_end(self):
		if not self.fy_start_date and self.country:
			self.fy_start_date = get_default_fiscal_year(self.country)["fy_start_date"]
		if self.fy_start_date:
			self.fy_end_date = add_days(add_years(getdate(self.fy_start_date), 1), -1)

	def validate_publish(self):
		was_published = bool(self.get_db_value("publish_to_customer")) if not self.is_new() else False

		if self.publish_to_customer and not was_published:
			frappe.only_for(ADMIN_ROLES)
			if self.status != "Ready":
				frappe.throw(
					_("A site can be published only when its status is Ready (current status: {0}).").format(
						self.status
					)
				)
			self.flags.allow_status_change = True
			self.flags.just_published = True
			self.status = "Published"
			self.published_on = now_datetime()
			self.published_by = frappe.session.user
		elif was_published and not self.publish_to_customer:
			frappe.throw(_("A published site cannot be unpublished. Use Stop instead."))

		if self.status == "Published" and not self.publish_to_customer:
			frappe.throw(_("Use the Publish to Customer checkbox to publish a site."))

	def validate_status_transition(self):
		if self.is_new():
			if self.status != "Queued":
				frappe.throw(_("A new Customer Site must start with status Queued."))
			return

		old = self.get_db_value("status")
		if old == self.status:
			return

		# Status is changed only by the actions (provisioning, retry, publish, stop...), never by hand.
		if not self.flags.allow_status_change:
			frappe.throw(_("Status cannot be edited directly. Use the buttons on the form."))

		if self.status not in ALLOWED_TRANSITIONS.get(old, set()):
			frappe.throw(_("Status cannot change from {0} to {1}.").format(old, self.status))

		if old == "Stopped" and self.status != self.status_before_stop:
			frappe.throw(
				_("A stopped site can only return to its previous status ({0}).").format(self.status_before_stop)
			)

	def clear_error_on_retry(self):
		"""Once a failed site is queued again, its old error no longer applies."""
		if self.is_new() or self.status != "Queued" or self.get_db_value("status") != "Failed":
			return
		self.error_log = None
		self.last_error = None
		self.failed_at = None

	@frappe.whitelist()
	def start_provisioning(self):
		"""Button: start a Queued site by hand, or retry a Failed one."""
		frappe.only_for(ADMIN_ROLES)
		if self.status == "Failed":
			self.flags.allow_status_change = True
			self.status = "Queued"
			self.save()  # on_update clears the old error and queues the build
		elif self.status == "Queued":
			enqueue_provisioning(self.name)
		else:
			frappe.throw(_("Provisioning can only be started for a Queued or Failed site (current status: {0}).").format(self.status))
		return self.status

	def get_overridable_statuses(self):
		"""Statuses an admin may set by hand. The rest belong to an action: provisioning moves a site
		through Queued/Creating/Configuring, and Published is reached only through the Publish checkbox."""
		allowed = ALLOWED_TRANSITIONS.get(self.get_db_value("status") or self.status, set())
		# Ready comes from Mark Ready (domain check), Stopped from Stop Site, Published from the checkbox.
		return sorted(allowed - {"Queued", "Creating", "Configuring", "Published", "Ready", "Stopped"})

	@frappe.whitelist()
	def get_allowed_statuses(self):
		frappe.only_for("System Manager")
		return self.get_overridable_statuses()

	@frappe.whitelist()
	def override_status(self, status, reason):
		"""Last-resort manual status change for System Managers. The reason is required and logged."""
		frappe.only_for("System Manager")
		reason = (reason or "").strip()
		if len(reason) < 5:
			frappe.throw(_("Please give a reason (at least 5 characters)."))
		if status not in self.get_overridable_statuses():
			frappe.throw(_("Status {0} cannot be set by hand from {1}.").format(status, self.get_db_value("status")))

		old = self.get_db_value("status")
		self.flags.allow_status_change = True
		self.status = status
		self.save()
		log_event(
			"Admin Action", "Admin User", self.name, self.marketing_order_id,
			detail=f"Status override {old} -> {status}: {reason}",
		)
		return self.status

	@frappe.whitelist()
	def check_domain_now(self):
		"""Test whether the site answers on its domain. Records the result; never changes the status."""
		frappe.only_for(ADMIN_ROLES)
		return domain.check_domain(frappe.get_doc("Customer Site", self.name))

	@frappe.whitelist()
	def mark_ready(self, skip_check=0, reason=None):
		"""Awaiting Domain Mapping -> Ready, but only if the site answers on its domain.
		A System Manager can skip the check with a reason (logged)."""
		frappe.only_for(ADMIN_ROLES)
		if self.get_db_value("status") != "Awaiting Domain Mapping":
			frappe.throw(_("Only a site that is Awaiting Domain Mapping can be marked Ready."))

		if cint(skip_check):
			frappe.only_for("System Manager")
			if len((reason or "").strip()) < 5:
				frappe.throw(_("Please give a reason (at least 5 characters)."))
			detail = f"Marked Ready, domain check skipped: {reason.strip()}"
		else:
			result = domain.check_domain(self)
			if not result["ok"]:
				frappe.db.commit()  # keep the recorded check result; throwing rolls back the request
				frappe.throw(_("Domain check failed: {0}").format(result["message"]), title=_("Not Ready"))
			detail = "Marked Ready, domain check passed"

		self.flags.allow_status_change = True
		self.status = "Ready"
		self.verified_by = frappe.session.user
		self.verified_on = now_datetime()
		self.save()
		log_event("Admin Action", "Admin User", self.name, self.marketing_order_id, detail=detail)
		return self.status

	@frappe.whitelist()
	def resend_publish_email(self):
		"""New password-setup link and email. Does not touch the Administrator password."""
		frappe.only_for(ADMIN_ROLES)
		if self.get_db_value("status") != "Published":
			frappe.throw(_("The publish email can only be sent for a Published site."))
		enqueue_publish(self.name, resend=True)
		log_event("Admin Action", "Admin User", self.name, self.marketing_order_id, detail="Publish email resend requested")
		return "queued"

	@frappe.whitelist()
	def stop_site(self, reason):
		"""Backup (if enabled) and put the site into maintenance mode."""
		frappe.only_for(ADMIN_ROLES)
		try:
			return lifecycle.request_stop(
				frappe.get_doc("Customer Site", self.name), reason, "Admin User", frappe.session.user
			)
		except SaaSAPIError as e:
			frappe.throw(e.message)

	@frappe.whitelist()
	def resume_site(self):
		"""Take the site out of maintenance mode and restore its previous status."""
		frappe.only_for(ADMIN_ROLES)
		try:
			return lifecycle.request_resume(
				frappe.get_doc("Customer Site", self.name), "Admin User", frappe.session.user
			)
		except SaaSAPIError as e:
			frappe.throw(e.message)

	@frappe.whitelist()
	def reveal_admin_password(self):
		"""Temporary Administrator password, for the manual check before publishing."""
		frappe.only_for(ADMIN_ROLES)
		password = self.get_password("admin_password", raise_exception=False)
		if not password:
			frappe.throw(_("No administrator password has been generated for this site yet."))
		log_event("Admin Action", "Admin User", self.name, self.marketing_order_id, detail="Administrator password viewed")
		return password

	def sync_stop_fields(self):
		old = None if self.is_new() else self.get_db_value("status")
		if old == self.status:
			return

		if self.status == "Stopped":
			self.status_before_stop = old
			self.is_stopped = 1
			self.stopped_on = now_datetime()
			self.stopped_by = self.stopped_by or frappe.session.user
		elif old == "Stopped":
			self.is_stopped = 0
			self.resumed_on = now_datetime()
