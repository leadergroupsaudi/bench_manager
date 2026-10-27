# -*- coding: utf-8 -*-
# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe

SAAS_ROLES = ("SaaS Admin", "SaaS Support", "SaaS Integration")


def after_install():
	ensure_saas_roles()


def ensure_saas_roles():
	"""Idempotent. SaaS Integration is not used in any DocType permission, so it is not auto-created."""
	for role in SAAS_ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
				ignore_permissions=True
			)
	# The API role is for key/secret users only, so it needs no desk access.
	frappe.db.set_value("Role", "SaaS Integration", "desk_access", 0)
