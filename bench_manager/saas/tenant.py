# -*- coding: utf-8 -*-
# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

"""Functions that run *inside a customer site*.

They are started by the provisioner through ``bench_manager.saas.tenant_runner`` (not ``bench execute``,
which refuses apps that are not installed on the site), so they must only use frappe and the apps
installed on the customer site, never Bench Manager DocTypes.
A failure is reported by raising: the runner process then exits non-zero and the provisioner records it.
"""

import json
import os
import secrets

import frappe

# Apps whose "seed" patches install first-run defaults (branding, menus, dashboards...). Frappe marks
# every patch of a freshly installed app as already run, so these would otherwise never execute.
SEED_APPS = ("lesaas_hr",)


def run_setup(payload_file):
	"""Complete the ERPNext setup wizard with the details of the Customer Site.

	`payload_file` holds the wizard arguments (no password: the first user gets a password-setup link).
	"""
	with open(payload_file) as f:
		args = json.load(f)

	frappe.set_user("Administrator")
	from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

	setup_complete(args)

	if not frappe.is_setup_complete():
		raise Exception("Setup wizard finished without marking the site as set up.")
	frappe.db.commit()
	return "Setup wizard completed"


def seed_defaults():
	"""Run the seed patches of SEED_APPS, in the order listed in the app's patches.txt."""
	frappe.set_user("Administrator")
	ran, failed = [], []

	for app in SEED_APPS:
		if app not in frappe.get_installed_apps():
			continue
		for dotted in _patch_paths(app):
			try:
				frappe.get_attr(f"{dotted}.execute")()
				frappe.db.commit()
				ran.append(dotted)
			except Exception as e:
				frappe.db.rollback()
				failed.append(f"{dotted}: {e}")

	if failed:
		raise Exception("Seed patches failed:\n" + "\n".join(failed))
	return f"{len(ran)} seed patches applied"


def _patch_paths(app):
	path = frappe.get_app_path(app, "patches.txt")
	if not os.path.exists(path):
		return []

	paths = []
	with open(path) as f:
		for line in f:
			line = line.split(" #")[0].strip()
			if not line or line.startswith(("#", "[", "execute:")):
				continue
			paths.append(line)
	return paths


def apply_license(max_users=None, license_end_date=None):
	"""Write the user limit and licence dates into foxerp's `License and Usage Info` and lock down sign-up."""
	frappe.set_user("Administrator")
	notes = []

	if frappe.db.exists("DocType", "License and Usage Info"):
		from frappe.utils import get_datetime, now_datetime

		lic = frappe.get_single("License and Usage Info")
		if max_users:
			lic.allowed_user = int(max_users)
			notes.append(f"user limit {int(max_users)}")
		else:
			notes.append("no max_users supplied, user limit NOT enforced")

		# foxerp's validate compares end_date < start_date, which fails if one is text and the other a
		# datetime (a stored value loads as text), so both are set as datetimes.
		lic.start_date = now_datetime()
		if license_end_date:
			lic.end_date = get_datetime(f"{license_end_date} 23:59:59")
			notes.append(f"licence ends {license_end_date}")
		elif lic.end_date:
			lic.end_date = get_datetime(lic.end_date)
		lic.save(ignore_permissions=True)
	else:
		notes.append("License and Usage Info is not installed, user limit NOT enforced")

	# Customers' users are created by the customer, never through public sign-up.
	frappe.db.set_single_value("Website Settings", "disable_signup", 1)
	frappe.db.commit()
	return "; ".join(notes)


def healthcheck(company_name, first_user_email, apps=None, max_users=None):
	"""Raise if the site is not in the state the provisioner expects."""
	frappe.set_user("Administrator")
	problems = []

	if not frappe.is_setup_complete():
		problems.append("setup wizard not complete")
	if not frappe.db.exists("Company", company_name):
		problems.append(f"company '{company_name}' missing")
	if not frappe.db.get_value("User", first_user_email, "enabled"):
		problems.append(f"first user {first_user_email} missing or disabled")

	missing = sorted(set(apps or []) - set(frappe.get_installed_apps()))
	if missing:
		problems.append("apps not installed: " + ", ".join(missing))

	if max_users and frappe.db.exists("DocType", "License and Usage Info"):
		allowed = frappe.db.get_single_value("License and Usage Info", "allowed_user")
		if int(allowed or 0) != int(max_users):
			problems.append(f"user limit is {allowed}, expected {max_users}")

	if problems:
		raise Exception("Health check failed: " + "; ".join(problems))
	return "Health check passed"


def make_reset_link(email, output_file, validity_days=2):
	"""Create a password-setup link for the user and write it to `output_file` (mode 0600).

	The link is written to a file and not printed: command output is stored in Bench Manager Command logs.
	The caller reads and deletes the file straight away."""
	frappe.set_user("Administrator")
	frappe.db.set_single_value(
		"System Settings", "reset_password_link_expiry_duration", int(validity_days) * 24 * 60 * 60
	)

	user = frappe.get_doc("User", email)
	link = user._reset_password(send_email=False)

	fd = os.open(output_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
	with os.fdopen(fd, "w") as f:
		f.write(link)
	frappe.db.commit()
	return "Password setup link created"


def rotate_admin_password():
	"""Give Administrator a random password nobody knows and end its sessions, so the customer's own
	System Manager user is the only way in. The new password is never stored or printed."""
	frappe.set_user("Administrator")
	from frappe.utils.password import update_password

	update_password("Administrator", secrets.token_urlsafe(32), logout_all_sessions=True)
	frappe.db.commit()
	return "Administrator password rotated"
