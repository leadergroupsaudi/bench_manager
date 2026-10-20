# -*- coding: utf-8 -*-
# Copyright (c) 2017, Frappe and contributors
# For license information, please see license.txt

# Local copy of the helpers that used to live in
# frappe.integrations.offsite_backup_utils (removed in Frappe v16).

import frappe
from frappe.utils import flt, split_emails


def send_email(success, service_name, doctype, email_field, error_status=None):
	recipients = get_recipients(doctype, email_field)
	if not recipients:
		frappe.log_error(
			"No Email Recipient found for {0}".format(service_name),
			"{0}: Failed to send backup status email".format(service_name),
		)
		return

	if success:
		if not frappe.db.get_single_value(doctype, "send_email_for_successful_backup"):
			return

		subject = "Backup Upload Successful"
		message = """
<h3>Backup Uploaded Successfully!</h3>
<p>Hi there, this is just to inform you that your backup was successfully uploaded to your {0} account. So relax!</p>
""".format(
			service_name
		)
	else:
		subject = "[Warning] Backup Upload Failed"
		message = """
<h3>Backup Upload Failed!</h3>
<p>Oops, your automated backup to {0} failed.</p>
<p>Error message: {1}</p>
<p>Please contact your system manager for more information.</p>
""".format(
			service_name, error_status
		)

	frappe.sendmail(recipients=recipients, subject=subject, message=message)


def get_recipients(doctype, email_field):
	if not frappe.db:
		frappe.connect()

	return split_emails(frappe.db.get_value(doctype, None, email_field))


def get_chunk_site(file_size):
	"""Return upload chunk size in bytes based on the file size."""
	MB = 1024 * 1024
	file_size_in_mb = flt(file_size) / MB

	if file_size_in_mb > 5000:
		return 200 * MB
	elif file_size_in_mb >= 3000:
		return 150 * MB
	elif file_size_in_mb >= 1000:
		return 100 * MB
	elif file_size_in_mb >= 500:
		return 50 * MB
	return 15 * MB
