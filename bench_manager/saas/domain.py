# -*- coding: utf-8 -*-
# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

"""Check that a customer site answers on its own domain.

A bare /api/method/ping is not enough: with wildcard DNS, another site on the bench could answer for
the same host. So a random token is written to a public file inside the customer site, and the check
must read that exact token back over the real URL (DNS, TLS and routing all included).
"""

import os
import secrets

import frappe
import requests
from frappe.utils import get_bench_path, now_datetime

from bench_manager.saas.utils import get_settings

TOKEN_FILENAME = "saas-domain-check.txt"
TIMEOUT_SECONDS = 8


def token_file_path(site):
	return os.path.join(get_bench_path(), "sites", site, "public", "files", TOKEN_FILENAME)


def write_token_file(site, token):
	path = token_file_path(site)
	if not os.path.isdir(os.path.dirname(path)):
		raise FileNotFoundError(f"Site folder for {site} not found")
	with open(path, "w") as f:
		f.write(token)


def remove_token_file(site):
	path = token_file_path(site)
	if os.path.exists(path):
		os.remove(path)


def probe_url(site):
	base = get_settings().domain_check_base_url.format(site=site)
	return f"{base}/files/{TOKEN_FILENAME}"


def _result(ok, stage, message):
	return {"ok": ok, "stage": stage, "message": message}


def probe(site, token):
	"""Request the token file over the real URL. Returns {ok, stage, message}."""
	url = probe_url(site)
	try:
		response = requests.get(url, timeout=TIMEOUT_SECONDS, allow_redirects=False)
	except requests.exceptions.SSLError as e:
		return _result(False, "ssl", f"Certificate problem for {url}: {e}")
	except requests.exceptions.Timeout:
		return _result(False, "timeout", f"{url} did not answer within {TIMEOUT_SECONDS} seconds")
	except requests.exceptions.ConnectionError as e:
		text = str(e)
		if "Name or service not known" in text or "getaddrinfo" in text or "Failed to resolve" in text:
			return _result(False, "dns", f"The domain does not resolve yet: {url}")
		return _result(False, "connection", f"Could not connect to {url}: {text[:200]}")
	except requests.exceptions.RequestException as e:
		return _result(False, "request", f"Request to {url} failed: {str(e)[:200]}")

	if response.status_code != 200:
		return _result(False, f"http_{response.status_code}", f"{url} answered with HTTP {response.status_code}")
	if response.text.strip() != token:
		return _result(False, "wrong_site", f"{url} answered, but not from this site (token mismatch)")
	return _result(True, "ok", f"{url} answers from this site")


def check_domain(doc):
	"""Run the check for a Customer Site, store the outcome on it, and return it. Never changes status."""
	site = doc.name
	try:
		token = doc.domain_check_token or secrets.token_hex(16)
		write_token_file(site, token)  # rewritten each time so a deleted file cannot cause a false failure
	except Exception as e:
		result = _result(False, "site_missing", f"Cannot prepare the check file: {e}")
		_store(doc, result)
		return result

	result = probe(site, token)
	_store(doc, result, token)
	return result


def _store(doc, result, token=None):
	values = {
		"domain_check_status": "Reachable" if result["ok"] else "Not reachable",
		"domain_check_detail": f"[{result['stage']}] {result['message']}"[:500],
		"domain_checked_on": now_datetime(),
	}
	if token:
		values["domain_check_token"] = token
	frappe.db.set_value("Customer Site", doc.name, values, update_modified=False)
	doc.update(values)


def check_pending_domains():
	"""Scheduler job (every 10 minutes): check sites waiting for domain mapping.

	Only records the result and alerts once when a site becomes reachable. It never changes a status:
	a person still checks the site and marks it Ready."""
	from bench_manager.saas.alerts import notify_admins

	names = frappe.get_all(
		"Customer Site",
		filters={"status": "Awaiting Domain Mapping"},
		order_by="domain_checked_on asc",
		limit=20,
		pluck="name",
	)
	for name in names:
		try:
			doc = frappe.get_doc("Customer Site", name)
			result = check_domain(doc)
			if result["ok"] and not doc.domain_alerted:
				frappe.db.set_value("Customer Site", name, "domain_alerted", 1)
				notify_admins(
					f"Domain is reachable: {name}",
					f"{name} now answers on its domain. Open it, check it, then use Mark Ready.",
					name,
				)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Domain check failed for {name}", message=frappe.get_traceback())
