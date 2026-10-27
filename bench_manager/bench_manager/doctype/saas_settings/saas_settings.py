# -*- coding: utf-8 -*-
# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from bench_manager.saas.utils import validate_base_domain


class SaaSSettings(Document):
	def validate(self):
		self.base_domain = (self.base_domain or "").strip().lower()
		try:
			validate_base_domain(self.base_domain)
		except Exception as e:
			frappe.throw(str(e), title=_("Invalid Base Domain"))

		if cint(self.min_prefix_length) < 1:
			frappe.throw(_("Minimum prefix length must be at least 1."))
		if cint(self.max_prefix_length) > 50:
			frappe.throw(_("Maximum prefix length cannot exceed 50."))
		if cint(self.min_prefix_length) > cint(self.max_prefix_length):
			frappe.throw(_("Minimum prefix length cannot exceed the maximum."))
		if cint(self.max_sites_per_hour) < 1:
			frappe.throw(_("Max New Sites Per Hour must be at least 1."))
