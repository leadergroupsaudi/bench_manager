# -*- coding: utf-8 -*-
# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

"""Run bench commands and customer-site functions for the lifecycle features (publish, stop).

Everything goes through Bench Manager's run_command, so each run is logged as a Bench Manager Command.
"""

import json
import os
import re
import shlex
import sys

import frappe
from frappe.utils import get_bench_path

from bench_manager.bench_manager.utils import get_bench_binary, run_command

CONSOLE_TAIL_CHARS = 4000


class CommandError(Exception):
	def __init__(self, message, details=""):
		super().__init__(message)
		self.message = message
		self.details = details


def clean(text):
	return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", (text or "").replace("\r", "\n"))


def tail(text, secrets=()):
	text = clean(text)
	for secret in secrets:
		if secret:
			text = text.replace(secret, "******")
	return text[-CONSOLE_TAIL_CHARS:]


def _run(site, label, tag, commands, secrets=()):
	key = f"saas-{site}-{tag}-{frappe.generate_hash(length=6)}"
	status, console = run_command(
		commands, doctype="Customer Site", key=key, cwd=get_bench_path(), docname=site, realtime=False
	)
	if status != "Success":
		raise CommandError(f"{label} failed", f"Command log: {key}\n\n{tail(console, secrets)}")
	return console


def run_on_site(site, function, kwargs, tag=None, secrets=()):
	"""Call an allowed bench_manager.saas.tenant function inside the customer site; returns its last output line."""
	python = os.path.join(get_bench_path(), "env", "bin", "python")
	if not os.path.exists(python):
		python = sys.executable
	command = " ".join(
		[
			shlex.quote(python),
			"-m",
			"bench_manager.saas.tenant_runner",
			"--site",
			shlex.quote(site),
			"--function",
			shlex.quote(function),
			"--kwargs",
			shlex.quote(json.dumps(kwargs)),
		]
	)
	console = _run(site, function, tag or function, [command], secrets)
	lines = [line.strip() for line in clean(console).splitlines() if line.strip()]
	return lines[-1] if lines else ""


def backup_site(site):
	"""`bench backup --with-files`; returns the output."""
	bench = get_bench_binary()
	return _run(site, "bench backup", "backup", [f"{bench} --site {shlex.quote(site)} backup --with-files"])
