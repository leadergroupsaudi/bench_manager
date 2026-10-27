# -*- coding: utf-8 -*-
# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

"""Run one of the functions in `bench_manager.saas.tenant` inside a customer site.

    python -m bench_manager.saas.tenant_runner --site <site> --function <name> --kwargs '<json>'

Why not `bench execute`? It refuses functions from apps that are not installed on the site, and
Bench Manager must never be installed on a customer site. This opens the site directly instead.

Only the functions in ALLOWED can be called, and the arguments are parsed as JSON (not evaluated).
A failure raises, so the process exits non-zero and the provisioner records the traceback.
"""

import argparse
import json
import os

ALLOWED = (
	"run_setup",
	"seed_defaults",
	"apply_license",
	"healthcheck",
	"make_reset_link",
	"rotate_admin_password",
)


def resolve(function):
	from bench_manager.saas import tenant

	if function not in ALLOWED:
		raise ValueError(f"'{function}' is not an allowed tenant function")
	return getattr(tenant, function)


def run(site, function, kwargs):
	import frappe

	fn = resolve(function)
	frappe.init(site)
	frappe.connect()
	try:
		result = fn(**kwargs)
		frappe.db.commit()
	finally:
		frappe.destroy()
	return result


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("--site", required=True)
	parser.add_argument("--function", required=True)
	parser.add_argument("--kwargs", default="{}")
	args = parser.parse_args()

	kwargs = json.loads(args.kwargs)
	if not isinstance(kwargs, dict):
		raise ValueError("--kwargs must be a JSON object")

	# frappe resolves the sites folder relative to the working directory
	bench_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
	os.chdir(os.path.join(bench_path, "sites"))

	result = run(args.site, args.function, kwargs)
	if result:
		print(result)


if __name__ == "__main__":
	main()
