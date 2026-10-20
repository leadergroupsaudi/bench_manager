# -*- coding: utf-8 -*-
# Copyright (c) 2017, Frappé and contributors
# For license information, please see license.txt


import re
import shlex
import time
from subprocess import PIPE, STDOUT, Popen
import frappe
from frappe import _
from frappe.model.document import Document


SENSITIVE_FLAGS = [
	"--mariadb-root-password",
	"--db-root-password",
	"--admin-password",
	"--root-password",
]


def mask_passwords(command):
	"""Replace the value of password flags (quoted or not) with ******."""
	pattern = r"({flags})(\s+|=)((?:'[^']*'|\"[^\"]*\"|\S)+)".format(
		flags="|".join(re.escape(flag) for flag in SENSITIVE_FLAGS)
	)
	return re.sub(pattern, r"\1\2******", command)


def get_bench_binary(settings=None):
	"""Bench executable from Bench Settings, else auto-detected from PATH."""
	import shutil

	settings = settings or frappe.get_single("Bench Settings")
	bench_bin = (settings.bench_binary or "").strip() or shutil.which("bench")
	if not bench_bin:
		frappe.throw(
			_("Bench binary not found. Set 'Bench Binary Path' in Bench Settings."),
			title=_("Bench Not Found"),
		)
	return bench_bin


def get_site_creation_settings():
	"""Validated site creation config from Bench Settings."""
	settings = frappe.get_single("Bench Settings")

	config = {
		"db_host": (settings.db_host or "").strip(),
		"db_root_user": (settings.db_root_user or "").strip(),
		"db_root_password": settings.get_password("db_root_password", raise_exception=False),
		"db_host_login_scope": (settings.db_host_login_scope or "%").strip(),
		"db_name_pattern": (settings.db_name_pattern or "{site_name}").strip(),
		"developer_mode": bool(settings.developer_mode_on_create),
		"apps": [row.app_name.strip() for row in settings.site_apps if row.enabled and row.app_name],
		"bench_bin": get_bench_binary(settings),
	}

	missing = [
		label
		for label, key in (
			("DB Host", "db_host"),
			("DB Root Username", "db_root_user"),
			("DB Root Password", "db_root_password"),
		)
		if not config[key]
	]
	if missing:
		frappe.throw(
			_("Please set {0} in Bench Settings > Site Creation.").format(", ".join(missing)),
			title=_("Site Creation Not Configured"),
		)
	if not config["apps"]:
		frappe.throw(
			_("Please add the apps to install in Bench Settings > Site Creation."),
			title=_("Site Creation Not Configured"),
		)

	return config


def validate_site_name(site_name):
	if not site_name or not re.match(r"^[A-Za-z0-9]([A-Za-z0-9._\-]*[A-Za-z0-9])?$", site_name):
		frappe.throw(
			_("Invalid site name. Use only letters, numbers, dots, hyphens and underscores."),
			title=_("Invalid Site Name"),
		)


def run_command(commands, doctype, key, cwd="..", docname=" ", after_command=None):
	verify_whitelisted_call()
	start_time = time.time()
	console_dump = ""
	logged_command = " && ".join(commands)
	logged_command += (
		" "  # to make sure passwords at the end of the commands are also hidden
	)
	logged_command = mask_passwords(logged_command)
	doc = frappe.get_doc(
		{
			"doctype": "Bench Manager Command",
			"key": key,
			"source": doctype + ": " + docname,
			"command": logged_command,
			"console": console_dump,
			"status": "Ongoing",
		}
	)
	doc.insert()
	frappe.db.commit()
	frappe.publish_realtime(
		key,
		"Executing Command:\n{logged_command}\n\n".format(logged_command=logged_command),
		user=frappe.session.user,
	)
	try:
		for command in commands:
			terminal = Popen(
				shlex.split(command), stdin=PIPE, stdout=PIPE, stderr=STDOUT, cwd=cwd
			)
			for c in iter(lambda: safe_decode(terminal.stdout.read(1)), ""):
				frappe.publish_realtime(key, c, user=frappe.session.user)
				console_dump += str(c)
		if terminal.wait():
			_close_the_doc(
				start_time, key, console_dump, status="Failed", user=frappe.session.user
			)
		else:
			_close_the_doc(
				start_time, key, console_dump, status="Success", user=frappe.session.user
			)
	except Exception as e:
		_close_the_doc(
			start_time,
			key,
			"{} \n\n{}".format(e, console_dump),
			status="Failed",
			user=frappe.session.user,
		)
	finally:
		frappe.db.commit()
		# hack: frappe.db.commit() to make sure the log created is robust,
		# and the _refresh throws an error if the doc is deleted
		frappe.enqueue(
			"bench_manager.bench_manager.utils._refresh",
			doctype=doctype,
			docname=docname,
			commands=commands,
		)


def _close_the_doc(start_time, key, console_dump, status, user):
	time_taken = time.time() - start_time
	final_console_dump = ""
	console_dump = console_dump.split("\n\r")
	for i in console_dump:
		i = i.split("\r")
		final_console_dump += "\n" + i[-1]

	# For Webhook to trigger using cmd.save()
	cmd = frappe.get_doc("Bench Manager Command", key)
	cmd.console = final_console_dump
	cmd.status = status
	cmd.time_taken = time_taken
	cmd.save()

	frappe.publish_realtime(
		key,
		"\n\n" + status + "!\nThe operation took " + str(time_taken) + " seconds",
		user=user,
	)


def _refresh(doctype, docname, commands):
	frappe.get_doc(doctype, docname).run_method("after_command", commands=commands)


@frappe.whitelist()
def verify_whitelisted_call():
	if "bench_manager" not in frappe.get_installed_apps():
		raise ValueError("This site does not have bench manager installed.")


def safe_decode(string, encoding="utf-8"):
	try:
		string = string.decode(encoding)
	except Exception:
		pass
	return string
