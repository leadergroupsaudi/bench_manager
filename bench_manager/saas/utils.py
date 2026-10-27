# -*- coding: utf-8 -*-
# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

"""Shared helpers for the SaaS site-creation flow (naming rules, settings, errors)."""

import os
import re

import frappe
from frappe import _
from frappe.utils import cint, get_bench_path

INTEGRATION_ROLE = "SaaS Integration"
ADMIN_ROLES = ("SaaS Admin", "System Manager")

DEFAULT_RESERVED_PREFIXES = (
	"admin",
	"www",
	"api",
	"mail",
	"smtp",
	"ftp",
	"lesaas",
	"local",
	"test",
	"staging",
	"demo",
	"support",
	"app",
	"dashboard",
	"static",
	"cdn",
)

PREFIX_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
DOMAIN_PATTERN = re.compile(r"^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$")


class SaaSAPIError(Exception):
	"""Raised by validators and API helpers; mapped to a structured HTTP response."""

	def __init__(self, error_code, message, http_status=422, field=None):
		super().__init__(message)
		self.error_code = error_code
		self.message = message
		self.http_status = http_status
		self.field = field


def get_settings():
	"""SaaS Settings with safe fallbacks (a never-saved Single has no values)."""
	doc = frappe.get_single("SaaS Settings")
	reserved = (doc.reserved_prefixes or "").split()
	return frappe._dict(
		base_domain=(doc.base_domain or "").strip().lower(),
		min_prefix_length=cint(doc.min_prefix_length) or 3,
		max_prefix_length=cint(doc.max_prefix_length) or 30,
		reserved_prefixes={r.strip().lower() for r in reserved} or set(DEFAULT_RESERVED_PREFIXES),
		max_sites_per_hour=cint(doc.max_sites_per_hour) or 10,
		auto_provision=_auto_provision_enabled(),
		domain_check_base_url=str(_stored("domain_check_base_url", "https://{site}")).strip().rstrip("/"),
		password_link_validity_days=cint(_stored("password_link_validity_days", 2)) or 2,
		backup_failure_policy=_stored("backup_failure_policy", "Stop anyway"),
		backup_before_stop=cint(_stored("backup_before_stop", 1)),
		min_disk_gb=cint(doc.pre_check_min_disk_gb) or 5,
		ip_allowlist=[ip.strip() for ip in re.split(r"[\s,]+", doc.marketing_ip_allowlist or "") if ip.strip()],
	)


def _stored(field, default):
	"""Value of a SaaS Settings field, or `default` if it was never saved.

	A field added after the Single was first saved reads as 0/empty, which cannot be told apart from
	"set to 0", so look at the stored row: no row means the field's default still applies.
	"""
	rows = frappe.db.sql(
		"select value from `tabSingles` where doctype=%s and field=%s", ("SaaS Settings", field)
	)
	return default if not rows or rows[0][0] in (None, "") else rows[0][0]


def _auto_provision_enabled():
	return cint(_stored("auto_provision_on_save", 1))


def normalize_prefix(prefix):
	return (prefix or "").strip().lower()


def validate_base_domain(base_domain):
	if not base_domain:
		raise SaaSAPIError(
			"not_configured",
			_("Base Domain is not set in SaaS Settings."),
			http_status=503,
		)
	if not DOMAIN_PATTERN.match(base_domain):
		raise SaaSAPIError(
			"not_configured",
			_("Base Domain in SaaS Settings is not a valid domain name."),
			http_status=503,
		)


def validate_prefix(prefix, settings=None):
	"""Format and policy checks. Does not look at existing sites."""
	settings = settings or get_settings()
	prefix = normalize_prefix(prefix)

	if not prefix:
		raise SaaSAPIError("invalid_prefix", _("Prefix is required."), field="prefix")
	if len(prefix) < settings.min_prefix_length:
		raise SaaSAPIError(
			"prefix_too_short",
			_("Prefix must be at least {0} characters.").format(settings.min_prefix_length),
			field="prefix",
		)
	if len(prefix) > settings.max_prefix_length:
		raise SaaSAPIError(
			"prefix_too_long",
			_("Prefix must be at most {0} characters.").format(settings.max_prefix_length),
			field="prefix",
		)
	if not PREFIX_PATTERN.match(prefix) or "--" in prefix:
		raise SaaSAPIError(
			"invalid_prefix",
			_("Prefix may contain only lowercase letters, digits and single hyphens, and must start and end with a letter or digit."),
			field="prefix",
		)
	if prefix in settings.reserved_prefixes:
		raise SaaSAPIError("prefix_reserved", _("This prefix is reserved."), field="prefix")
	return prefix


def build_site_name(prefix, base_domain):
	return f"{normalize_prefix(prefix)}.{base_domain}"


def site_exists_on_disk(site_name):
	return os.path.isdir(os.path.join(get_bench_path(), "sites", site_name))


def is_site_name_taken(site_name):
	return bool(frappe.db.exists("Customer Site", site_name)) or site_exists_on_disk(site_name)


def check_prefix_availability(prefix):
	"""Returns {"available": bool, "site_name": str, "reason": str|None}."""
	settings = get_settings()
	validate_base_domain(settings.base_domain)
	try:
		prefix = validate_prefix(prefix, settings)
	except SaaSAPIError as e:
		return {"available": False, "site_name": None, "reason": e.error_code}

	site_name = build_site_name(prefix, settings.base_domain)
	if is_site_name_taken(site_name):
		return {"available": False, "site_name": site_name, "reason": "prefix_taken"}
	return {"available": True, "site_name": site_name, "reason": None}


# ----------------------------------------------------------------------------- locale helpers
# These mirror what the ERPNext setup wizard offers, so a Customer Site can only hold values the
# wizard itself would accept.


def get_country_timezones(country):
	"""Timezones the wizard lists first for this country, sorted (may be empty)."""
	from frappe.geo.country_info import get_country_info

	return sorted(get_country_info(country).get("timezones") or [])


def get_default_timezone(country):
	timezones = get_country_timezones(country)
	return timezones[0] if timezones else None


def is_valid_timezone(timezone):
	import pytz

	return timezone in pytz.all_timezones_set


def resolve_language(value):
	"""Accept a Language code ("en") or name ("English"); return the enabled Language's code, else None."""
	value = (value or "").strip()
	if not value:
		return None
	code = frappe.db.get_value("Language", {"language_name": value, "enabled": 1}) or frappe.db.get_value(
		"Language", {"name": value, "enabled": 1}
	)
	return code


def get_chart_options(country):
	"""Same list the setup wizard shows for the country (including the Standard templates)."""
	from erpnext.accounts.doctype.account.chart_of_accounts.chart_of_accounts import get_charts_for_country

	return get_charts_for_country(country, with_standard=True) if country else []


_FISCAL_YEAR_LINE = re.compile(r'^\s*(?:"([^"]+)"|([A-Za-z_]+))\s*:\s*\["(\d{2}-\d{2})",\s*"(\d{2}-\d{2})"\]')


def _wizard_fiscal_years():
	"""Country -> (start "MM-DD", end "MM-DD"), read from the table the setup wizard itself uses
	(erpnext/public/js/setup_wizard.js), so it stays in step with ERPNext upgrades."""
	cached = frappe.cache.get_value("saas_wizard_fiscal_years")
	if cached:
		return cached

	years = {}
	path = frappe.get_app_path("erpnext", "public", "js", "setup_wizard.js")
	in_table = False
	with open(path) as f:
		for line in f:
			if "erpnext.setup.fiscal_years = {" in line:
				in_table = True
				continue
			if in_table:
				if line.strip().startswith("}"):
					break
				match = _FISCAL_YEAR_LINE.match(line)
				if match:
					years[match.group(1) or match.group(2)] = [match.group(3), match.group(4)]

	frappe.cache.set_value("saas_wizard_fiscal_years", years)
	return years


@frappe.whitelist()
def get_default_fiscal_year(country=None):
	"""Financial year the setup wizard pre-fills for `country` (1 Jan - 31 Dec when it has no entry).
	Returns {"fy_start_date", "fy_end_date"} as ISO dates."""
	from frappe.utils import getdate, today

	now = getdate(today())
	year, next_year = now.year, now.year + 1
	span = _wizard_fiscal_years().get(country)
	if not span:
		span, next_year = ["01-01", "12-31"], year

	# Same rule as the wizard: a year that has not started yet means the previous one is current.
	if getdate(f"{year}-{span[0]}") > now:
		next_year = year
		year -= 1

	return {"fy_start_date": f"{year}-{span[0]}", "fy_end_date": f"{next_year}-{span[1]}"}
