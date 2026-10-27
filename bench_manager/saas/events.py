# -*- coding: utf-8 -*-
# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe


def log_event(
	event,
	source="Admin User",
	customer_site=None,
	marketing_order_id=None,
	result="OK",
	detail=None,
	commit=False,
):
	"""Append a row to SaaS Event Log. Never include passwords, keys or raw payloads in `detail`."""
	doc = frappe.get_doc(
		{
			"doctype": "SaaS Event Log",
			"event": event,
			"source": source,
			"customer_site": customer_site,
			"marketing_order_id": marketing_order_id,
			"result": result,
			"detail": (detail or "")[:1000],
			"user": frappe.session.user,
			"remote_ip": getattr(frappe.local, "request_ip", None),
		}
	)
	doc.insert(ignore_permissions=True)
	if commit:
		frappe.db.commit()
	return doc.name
