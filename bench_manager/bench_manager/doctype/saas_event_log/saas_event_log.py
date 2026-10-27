# -*- coding: utf-8 -*-
# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class SaaSEventLog(Document):
	"""Append-only audit trail: rows can be inserted but never changed or deleted."""

	def before_save(self):
		if not self.is_new():
			frappe.throw(_("SaaS Event Log entries cannot be modified."))

	def on_trash(self):
		if not frappe.flags.in_install and not frappe.flags.in_migrate:
			frappe.throw(_("SaaS Event Log entries cannot be deleted."))
