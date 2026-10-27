# -*- coding: utf-8 -*-
# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

"""Read and change one key of a customer site's site_config.json (used for maintenance_mode).

The admin site and all customer sites live in the same bench, so the file is edited directly. Frappe
re-reads site_config.json on every request, so the change takes effect immediately and needs no restart.
"""

import json
import os
import tempfile

from frappe.utils import get_bench_path

from bench_manager.bench_manager.utils import validate_site_name


def _site_dir(site):
	validate_site_name(site)  # only plain site names, never a path
	return os.path.join(get_bench_path(), "sites", site)


def _config_path(site):
	return os.path.join(_site_dir(site), "site_config.json")


def get_flag(site, key):
	with open(_config_path(site)) as f:
		return json.load(f).get(key)


def set_flag(site, key, value):
	"""Set `key`; a value of None removes it."""
	path = _config_path(site)
	if not os.path.isfile(path):
		raise FileNotFoundError(f"site_config.json not found for {site}")

	with open(path) as f:
		config = json.load(f)
	if value is None:
		config.pop(key, None)
	else:
		config[key] = value

	# write beside the original and swap, so a crash never leaves a half-written config
	fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".site_config.", suffix=".tmp")
	try:
		with os.fdopen(fd, "w") as f:
			json.dump(config, f, indent=1)
		os.chmod(tmp, os.stat(path).st_mode & 0o777)
		os.replace(tmp, path)
	finally:
		if os.path.exists(tmp):
			os.remove(tmp)

	if get_flag(site, key) != value:
		raise RuntimeError(f"Could not set {key} for {site}")
