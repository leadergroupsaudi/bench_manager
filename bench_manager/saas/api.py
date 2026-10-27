# -*- coding: utf-8 -*-
# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

"""Marketing -> admin site API.

Authentication is Frappe's standard API key/secret (``Authorization: token <key>:<secret>``) for a
user holding the "SaaS Integration" role. The role has no DocType permissions: it can only call the
methods below, which act on its behalf after validating everything server-side.

Endpoints (all under ``/api/method/bench_manager.saas.api.<name>``):
	check_subdomain        GET|POST  is a prefix usable?
	create_site_request    POST      queue a new customer site (idempotent on marketing_order_id)
	get_site_status        GET|POST  status of a requested site
	stop_site              POST      stop a Published site (maintenance mode); idempotent
	resume_site            POST      bring a stopped site back; idempotent
"""

import hashlib
import json
import re
from functools import wraps

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import add_to_date, getdate, now_datetime, today, validate_email_address

from bench_manager.saas import lifecycle
from bench_manager.saas.events import log_event
from bench_manager.saas.utils import (
	INTEGRATION_ROLE,
	SaaSAPIError,
	build_site_name,
	check_prefix_availability,
	get_chart_options,
	get_default_fiscal_year,
	get_default_timezone,
	get_settings,
	is_site_name_taken,
	is_valid_timezone,
	resolve_language,
	validate_base_domain,
	validate_prefix,
)

ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:\-]{1,100}$")
ABBR_PATTERN = re.compile(r"^[A-Za-z0-9]{1,10}$")
MAX_TEXT = 140

# Customer Site status -> coarse state shown to the marketing site
STATE_MAP = {
	"Queued": "queued",
	"Creating": "provisioning",
	"Configuring": "provisioning",
	"Awaiting Domain Mapping": "awaiting_verification",
	"Ready": "awaiting_verification",
	"Published": "published",
	"Stopped": "stopped",
	"Failed": "failed",
}


# ----------------------------------------------------------------------------- plumbing


def integration_endpoint(log_rejections=False):
	"""Authorise the caller, and turn SaaSAPIError into a structured JSON error response."""

	def decorator(fn):
		@wraps(fn)
		def wrapper(*args, **kwargs):
			try:
				_authorize()
				return fn(*args, **kwargs)
			except SaaSAPIError as e:
				frappe.db.rollback()
				if log_rejections:
					log_event(
						"Request Rejected",
						"Marketing API",
						marketing_order_id=_safe_order_id(kwargs.get("marketing_order_id")),
						result="Rejected",
						detail=f"{e.error_code}" + (f" ({e.field})" if e.field else ""),
						commit=True,
					)
				return _error_response(e)

		return wrapper

	return decorator


def _authorize():
	if frappe.session.user == "Guest":
		raise SaaSAPIError("unauthenticated", _("Authentication required."), 401)
	if INTEGRATION_ROLE not in frappe.get_roles():
		raise SaaSAPIError("forbidden", _("This user is not allowed to use the site API."), 403)

	allowlist = get_settings().ip_allowlist
	if allowlist and frappe.local.request_ip not in allowlist:
		raise SaaSAPIError("ip_not_allowed", _("Requests from this address are not allowed."), 403)


def _error_response(e):
	frappe.local.response["http_status_code"] = e.http_status
	error = {"code": e.error_code, "message": e.message}
	if e.field:
		error["field"] = e.field
	return {"ok": False, "error": error}


def _safe_order_id(value):
	value = (value or "").strip() if isinstance(value, str) else ""
	return value[:100] if ORDER_ID_PATTERN.match(value) else None


# ----------------------------------------------------------------------------- endpoints


@frappe.whitelist(methods=["GET", "POST"])
@rate_limit(limit=120, seconds=60)
@integration_endpoint()
def check_subdomain(prefix=None):
	"""Is `prefix` valid and free?  -> {ok, available, site_name, reason}"""
	result = check_prefix_availability(prefix)
	return {"ok": True, **result}


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
@integration_endpoint(log_rejections=True)
def create_site_request(
	marketing_order_id=None,
	prefix=None,
	plan_name=None,
	customer=None,
	company=None,
	setup=None,
	first_user=None,
	persona=None,
	limits=None,
):
	"""Queue a customer site. Safe to retry: the same marketing_order_id with the same payload returns
	the existing record; the same id with a different payload is rejected (409)."""
	payload = {
		"marketing_order_id": marketing_order_id,
		"prefix": prefix,
		"plan_name": plan_name,
		"customer": _as_dict(customer, "customer"),
		"company": _as_dict(company, "company"),
		"setup": _as_dict(setup, "setup"),
		"first_user": _as_dict(first_user, "first_user"),
		"persona": _as_dict(persona, "persona"),
		"limits": _as_dict(limits, "limits"),
	}
	_reject_password_fields(payload)
	clean = _validate_payload(payload)
	payload_hash = hashlib.sha256(json.dumps(clean, sort_keys=True, default=str).encode()).hexdigest()

	existing = _get_by_order_id(clean["marketing_order_id"])
	if existing:
		return _replay_response(existing, payload_hash)

	settings = get_settings()
	validate_base_domain(settings.base_domain)
	site_name = build_site_name(clean["prefix"], settings.base_domain)
	if is_site_name_taken(site_name):
		raise SaaSAPIError("prefix_taken", _("This site name is not available."), 409, "prefix")

	recent = frappe.db.count("Customer Site", {"creation": [">", add_to_date(now_datetime(), hours=-1)]})
	if recent >= settings.max_sites_per_hour:
		raise SaaSAPIError("hourly_cap_reached", _("Too many site requests. Try again later."), 429)

	doc = frappe.get_doc(_to_doc_values(clean, payload_hash))
	doc.flags.from_api = True
	try:
		doc.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		# Lost a race against a concurrent request for the same order or the same prefix.
		frappe.db.rollback()
		existing = _get_by_order_id(clean["marketing_order_id"])
		if existing:
			return _replay_response(existing, payload_hash)
		raise SaaSAPIError("prefix_taken", _("This site name is not available."), 409, "prefix")

	log_event(
		"Request Received",
		"Marketing API",
		doc.name,
		doc.marketing_order_id,
		detail=f"plan={clean.get('plan_name') or '-'}",
	)
	frappe.local.response["http_status_code"] = 202
	return _site_response(doc, idempotent_replay=False)


@frappe.whitelist(methods=["GET", "POST"])
@rate_limit(limit=300, seconds=60)
@integration_endpoint()
def get_site_status(marketing_order_id=None, site_name=None):
	"""Status of a requested site, looked up by marketing_order_id or site_name."""
	return _site_response(_find_site(marketing_order_id, site_name), idempotent_replay=None)


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=30, seconds=60)
@integration_endpoint(log_rejections=True)
def stop_site(marketing_order_id=None, site_name=None, reason=None):
	"""Stop a Published site on marketing's instruction (e.g. subscription ended).

	Returns 202 {state: "stopping"} while the job runs (backup first, if enabled), 200 {state: "stopped"}
	if it already is. Repeating the call is safe."""
	doc = _find_site(marketing_order_id, site_name)
	state = lifecycle.request_stop(doc, reason, "Marketing API", "marketing-api", allowed_statuses=("Published",))
	if state == "stopping":
		frappe.local.response["http_status_code"] = 202
	return _site_response(frappe.get_doc("Customer Site", doc.name), idempotent_replay=None)


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=30, seconds=60)
@integration_endpoint(log_rejections=True)
def resume_site(marketing_order_id=None, site_name=None):
	"""Bring a stopped site back (e.g. subscription renewed). A site that is not stopped is left as it is."""
	doc = _find_site(marketing_order_id, site_name)
	if doc.status == "Published" and doc.stop_state not in lifecycle.IN_PROGRESS:
		return _site_response(doc, idempotent_replay=None)  # already running: nothing to do
	lifecycle.request_resume(doc, "Marketing API", "marketing-api")
	return _site_response(frappe.get_doc("Customer Site", doc.name), idempotent_replay=None)


def _find_site(marketing_order_id, site_name):
	name = None
	if marketing_order_id:
		name = _get_by_order_id(_safe_order_id(marketing_order_id))
	elif site_name:
		name = frappe.db.exists("Customer Site", (site_name or "").strip().lower())
	else:
		raise SaaSAPIError("missing_parameter", _("Provide marketing_order_id or site_name."), 422)
	if not name:
		raise SaaSAPIError("not_found", _("No site found."), 404)
	return frappe.get_doc("Customer Site", name)


# ----------------------------------------------------------------------------- validation


def _as_dict(value, field):
	if value in (None, ""):
		return {}
	if isinstance(value, str):
		try:
			value = frappe.parse_json(value)
		except Exception:
			raise SaaSAPIError("invalid_payload", _("{0} must be a JSON object.").format(field), 422, field)
	if not isinstance(value, dict):
		raise SaaSAPIError("invalid_payload", _("{0} must be an object.").format(field), 422, field)
	return value


def _reject_password_fields(node):
	"""The API never accepts credentials; first login is through a password-setup link."""
	if isinstance(node, dict):
		for key, value in node.items():
			if re.search(r"pass(word|wd)?|secret|token", str(key), re.I):
				raise SaaSAPIError(
					"password_not_accepted",
					_("Credentials are not accepted by this API."),
					422,
					str(key),
				)
			_reject_password_fields(value)
	elif isinstance(node, (list, tuple)):
		for item in node:
			_reject_password_fields(item)


def _text(section, key, label, required=True, max_len=MAX_TEXT):
	value = section.get(key)
	if value is None or (isinstance(value, str) and not value.strip()):
		if required:
			raise SaaSAPIError("missing_field", _("{0} is required.").format(label), 422, label)
		return None
	if not isinstance(value, (str, int)):
		raise SaaSAPIError("invalid_field", _("{0} is invalid.").format(label), 422, label)
	value = str(value).strip()
	if len(value) > max_len:
		raise SaaSAPIError("invalid_field", _("{0} is too long.").format(label), 422, label)
	if re.search(r"[<>]", value):
		raise SaaSAPIError("invalid_field", _("{0} contains invalid characters.").format(label), 422, label)
	return value


def _email(section, key, label):
	value = _text(section, key, label)
	validated = validate_email_address(value, throw=False)
	if not validated:
		raise SaaSAPIError("invalid_email", _("{0} is not a valid email address.").format(label), 422, label)
	return validated.lower()


def _validate_payload(p):
	order_id = _text(p, "marketing_order_id", "marketing_order_id", max_len=100)
	if not ORDER_ID_PATTERN.match(order_id):
		raise SaaSAPIError(
			"invalid_field", _("marketing_order_id has invalid characters."), 422, "marketing_order_id"
		)

	clean = {
		"marketing_order_id": order_id,
		"prefix": validate_prefix(p.get("prefix")),
		"plan_name": _text(p, "plan_name", "plan_name", required=False),
	}

	c, co, s, u = p["customer"], p["company"], p["setup"], p["first_user"]
	clean["customer"] = {
		"name": _text(c, "name", "customer.name"),
		"email": _email(c, "email", "customer.email"),
	}

	abbr = _text(co, "abbr", "company.abbr", max_len=10)
	if not ABBR_PATTERN.match(abbr):
		raise SaaSAPIError(
			"invalid_field", _("company.abbr must be 1-10 letters or digits."), 422, "company.abbr"
		)
	clean["company"] = {
		"name": _text(co, "name", "company.name"),
		"abbr": abbr,
		"chart_of_accounts": _text(co, "chart_of_accounts", "company.chart_of_accounts"),
	}

	country = _text(s, "country", "setup.country")
	if not frappe.db.exists("Country", country):
		raise SaaSAPIError("invalid_country", _("Unknown country."), 422, "setup.country")
	currency = _text(s, "currency", "setup.currency")
	if not frappe.db.exists("Currency", currency):
		raise SaaSAPIError("invalid_currency", _("Unknown currency."), 422, "setup.currency")
	# Optional: defaults to the country's timezone, exactly like the setup wizard.
	timezone = _text(s, "timezone", "setup.timezone", required=False) or get_default_timezone(country)
	if not timezone or not is_valid_timezone(timezone):
		raise SaaSAPIError("invalid_timezone", _("Unknown or missing timezone."), 422, "setup.timezone")
	# Accepts a language code ("en") or name ("English").
	language = resolve_language(_text(s, "language", "setup.language"))
	if not language:
		raise SaaSAPIError("invalid_language", _("Unknown or disabled language."), 422, "setup.language")
	# Optional: defaults to the financial year the setup wizard proposes for the country.
	fy_start_text = _text(s, "fy_start_date", "setup.fy_start_date", required=False)
	try:
		fy_start = getdate(fy_start_text or get_default_fiscal_year(country)["fy_start_date"])
	except Exception:
		raise SaaSAPIError("invalid_date", _("Invalid financial year start date."), 422, "setup.fy_start_date")

	if clean["company"]["chart_of_accounts"] not in get_chart_options(country):
		raise SaaSAPIError(
			"invalid_chart_of_accounts",
			_("Chart of accounts is not available for this country."),
			422,
			"company.chart_of_accounts",
		)

	clean["setup"] = {
		"country": country,
		"currency": currency,
		"timezone": timezone,
		"language": language,
		"fy_start_date": str(fy_start),
	}
	clean["first_user"] = {
		"full_name": _text(u, "full_name", "first_user.full_name"),
		"email": _email(u, "email", "first_user.email"),
	}

	persona = p["persona"]
	clean["persona"] = {k: _text(persona, k, f"persona.{k}", required=False) for k in sorted(persona)}

	limits = p["limits"]
	clean["limits"] = {}
	if limits.get("max_users") not in (None, ""):
		try:
			max_users = int(limits["max_users"])
		except (TypeError, ValueError):
			max_users = 0
		if not 1 <= max_users <= 100000:
			raise SaaSAPIError(
				"invalid_field", _("limits.max_users must be between 1 and 100000."), 422, "limits.max_users"
			)
		clean["limits"]["max_users"] = max_users
	if limits.get("license_end_date") not in (None, ""):
		try:
			end = getdate(limits["license_end_date"])
		except Exception:
			raise SaaSAPIError(
				"invalid_date", _("Invalid license_end_date."), 422, "limits.license_end_date"
			)
		if end < getdate(today()):
			raise SaaSAPIError(
				"invalid_date", _("license_end_date cannot be in the past."), 422, "limits.license_end_date"
			)
		clean["limits"]["license_end_date"] = str(end)

	return clean


def _to_doc_values(clean, payload_hash):
	return {
		"doctype": "Customer Site",
		"prefix": clean["prefix"],
		"marketing_order_id": clean["marketing_order_id"],
		"plan_name": clean.get("plan_name"),
		"status": "Queued",
		"customer_name": clean["customer"]["name"],
		"customer_email": clean["customer"]["email"],
		"company_name": clean["company"]["name"],
		"company_abbr": clean["company"]["abbr"],
		"chart_of_accounts": clean["company"]["chart_of_accounts"],
		"country": clean["setup"]["country"],
		"currency": clean["setup"]["currency"],
		"timezone": clean["setup"]["timezone"],
		"language": clean["setup"]["language"],
		"fy_start_date": clean["setup"]["fy_start_date"],
		"first_user_full_name": clean["first_user"]["full_name"],
		"first_user_email": clean["first_user"]["email"],
		"persona_notes": "\n".join(f"{k}: {v}" for k, v in clean["persona"].items() if v) or None,
		"max_users": clean["limits"].get("max_users"),
		"license_end_date": clean["limits"].get("license_end_date"),
		"payload_hash": payload_hash,
	}


# ----------------------------------------------------------------------------- responses


def _get_by_order_id(order_id):
	if not order_id:
		return None
	return frappe.db.get_value("Customer Site", {"marketing_order_id": order_id}, "name")


def _replay_response(name, payload_hash):
	doc = frappe.get_doc("Customer Site", name)
	if doc.payload_hash != payload_hash:
		raise SaaSAPIError(
			"idempotency_conflict",
			_("This marketing_order_id was already used with different data."),
			409,
			"marketing_order_id",
		)
	log_event(
		"Request Replayed",
		"Marketing API",
		doc.name,
		doc.marketing_order_id,
		commit=True,
	)
	return _site_response(doc, idempotent_replay=True)


def _site_response(doc, idempotent_replay):
	published = bool(doc.publish_to_customer) and doc.status == "Published"
	state = STATE_MAP.get(doc.status, "unknown")
	if doc.stop_state in lifecycle.IN_PROGRESS:
		state = "stopping"
	out = {
		"ok": True,
		"marketing_order_id": doc.marketing_order_id,
		"site_name": doc.site_name,
		"status": doc.status,
		"state": state,
		"published": published,
		# The URL is withheld until the team has verified and published the site.
		"site_url": doc.site_url if published else None,
		"stopped": bool(doc.is_stopped),
		"stop_failed": doc.stop_state == "Failed",
	}
	if idempotent_replay is not None:
		out["idempotent_replay"] = idempotent_replay
	return out
