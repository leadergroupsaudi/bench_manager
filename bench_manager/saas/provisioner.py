# -*- coding: utf-8 -*-
# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

"""Builds a customer site for a Customer Site record.

Reuses Bench Manager's settings (DB root, app list, bench binary) and its command runner
(`run_command`, which logs every command as a Bench Manager Command). The job is resumable: every
step is recorded on the Customer Site, a retry skips finished steps, and a site that was left
half-built by the failed step is dropped and rebuilt (only if this system created it).

On failure an Error Log is created with the details and linked on the Customer Site (`error_log`);
it is cleared again when the site is retried.
"""

import json
import os
import re
import secrets
import shlex
import shutil
import sys

import frappe
from frappe import _
from frappe.utils import cint, get_bench_path, now_datetime
from frappe.utils.synchronization import filelock

from bench_manager.bench_manager.utils import (
	get_site_creation_settings,
	run_command,
	validate_site_name,
)
from bench_manager.saas.alerts import notify_admins
from bench_manager.saas.events import log_event
from bench_manager.saas.utils import get_settings

STEPS = (
	"reserve",
	"create_site",
	"install_apps",
	"verify_apps",
	"setup_wizard",
	"seed_defaults",
	"apply_license",
	"healthcheck",
	"domain_wait",
)
CREATING_STEPS = {"reserve", "create_site", "install_apps", "verify_apps"}
# If one of these fails the site cannot be trusted (partial database / half-run wizard), so a retry
# drops it and starts again. Later steps are safe to resume.
RESTART_STEPS = {"reserve", "create_site", "setup_wizard"}
CONSOLE_TAIL_CHARS = 4000
JOB_TIMEOUT = 3 * 60 * 60


class ProvisioningError(Exception):
	def __init__(self, step, message, details=""):
		super().__init__(message)
		self.step = step
		self.message = message
		self.details = details


def enqueue_provisioning(site_name):
	"""Queue the build of `site_name`. Safe to call twice: a job already queued/running is not duplicated."""
	frappe.enqueue(
		"bench_manager.saas.provisioner.provision_site",
		queue="long",
		timeout=JOB_TIMEOUT,
		job_id=f"saas_provision::{site_name}",
		deduplicate=True,
		enqueue_after_commit=True,
		site_name=site_name,
	)


def provision_site(site_name):
	"""Background job entry point."""
	frappe.set_user("Administrator")
	# One build at a time per bench: concurrent `new-site`/`migrate` runs fight over the same files.
	with filelock("saas_provisioning", timeout=JOB_TIMEOUT, is_global=True):
		doc = _claim(site_name)
		if not doc:
			return
		provisioner = Provisioner(doc)
		try:
			provisioner.run()
		except ProvisioningError as e:
			provisioner.fail(e)
		except Exception as e:
			provisioner.fail(ProvisioningError(provisioner.current or "unknown", str(e), frappe.get_traceback()))


def _claim(site_name):
	"""Move Queued -> Creating atomically so two workers never build the same site."""
	doc = frappe.get_doc("Customer Site", site_name, for_update=True)
	if doc.status != "Queued":
		frappe.db.rollback()
		return None
	doc.status = "Creating"
	_save(doc)
	return doc


def _save(doc):
	doc.flags.allow_status_change = True  # the provisioner is a sanctioned status changer
	doc.flags.ignore_permissions = True
	doc.flags.ignore_version = True
	doc.save()
	frappe.db.commit()


class Provisioner:
	def __init__(self, doc):
		self.doc = doc
		self.site = doc.name
		self.bench_path = get_bench_path()
		self.current = None
		self.cfg = None
		self.last_key = None

	# ------------------------------------------------------------------ flow

	def run(self):
		log_event("Provisioning Started", "Scheduler", self.site, self.doc.marketing_order_id, detail=f"attempt {cint(self.doc.attempts) + 1}")
		self.ensure_step_rows()
		self.prepare_attempt()

		for step in STEPS:
			if self.step_row(step).status == "Done":
				continue
			self.run_step(step)

		self.finish()

	def run_step(self, step):
		self.current = step
		row = self.step_row(step)
		self.set_status("Creating" if step in CREATING_STEPS else "Configuring")
		self.doc.current_step = step
		row.status = "Running"
		row.started_on = now_datetime()
		row.finished_on = None
		row.attempts = cint(row.attempts) + 1
		row.message = ""
		_save(self.doc)

		message = getattr(self, f"step_{step}")() or ""

		row.status = "Done"
		row.finished_on = now_datetime()
		row.message = (message or "")[:500]
		_save(self.doc)

	def finish(self):
		if self.doc.status == "Creating":  # Creating -> Configuring -> Awaiting Domain Mapping
			self.set_status("Configuring")
			_save(self.doc)
		self.set_status("Awaiting Domain Mapping")
		self.doc.current_step = "domain_wait"
		self.doc.error_log = None
		self.doc.last_error = None
		self.doc.failed_at = None
		_save(self.doc)
		log_event("Provisioning Succeeded", "Scheduler", self.site, self.doc.marketing_order_id, detail="Awaiting domain mapping")

	def set_status(self, status):
		if self.doc.status != status:
			self.doc.status = status

	def fail(self, error):
		"""Record the failure: Error Log with full details, linked on the Customer Site."""
		frappe.db.rollback()
		doc = frappe.get_doc("Customer Site", self.site)
		attempt = cint(doc.attempts) + 1

		details = "\n".join(
			[
				f"Site: {self.site}",
				f"Marketing order: {doc.marketing_order_id}",
				f"Step: {error.step}",
				f"Attempt: {attempt}",
				f"Time: {now_datetime()}",
				"",
				f"Error: {error.message}",
				"",
				self.scrub(error.details or ""),
			]
		)
		error_log = frappe.get_doc(
			{
				"doctype": "Error Log",
				"method": f"Site provisioning failed: {self.site} [{error.step}]"[:140],
				"error": details,
			}
		).insert(ignore_permissions=True)

		for row in doc.steps:
			if row.step == error.step:
				row.status = "Failed"
				row.finished_on = now_datetime()
				row.message = error.message[:500]

		doc.status = "Failed"
		doc.current_step = error.step
		doc.attempts = attempt
		doc.failed_at = now_datetime()
		doc.error_log = error_log.name
		doc.last_error = f"[{error.step}] {error.message}"[:500]
		if self.last_key and frappe.db.exists("Bench Manager Command", self.last_key):
			doc.bench_command = self.last_key
		try:
			_save(doc)
		except Exception:
			# Never leave a site stuck in Creating just because the full save was rejected.
			frappe.db.rollback()
			frappe.db.set_value(
				"Customer Site",
				self.site,
				{
					"status": "Failed",
					"current_step": error.step,
					"attempts": attempt,
					"failed_at": now_datetime(),
					"error_log": error_log.name,
					"last_error": doc.last_error,
				},
			)
			frappe.db.commit()

		log_event(
			"Provisioning Failed",
			"Scheduler",
			self.site,
			doc.marketing_order_id,
			result="Error",
			detail=f"{error.step}: {error.message}",
			commit=True,
		)
		notify_admins(
			_("Site provisioning failed: {0}").format(self.site),
			_("Step '{0}' failed: {1}\nSee the Error Log linked on the Customer Site.").format(error.step, error.message),
			self.site,
		)
		frappe.db.commit()

	# ------------------------------------------------------------------ steps bookkeeping

	def ensure_step_rows(self):
		existing = {row.step for row in self.doc.steps}
		for step in STEPS:
			if step not in existing:
				self.doc.append("steps", {"step": step, "status": "Pending", "attempts": 0})
		_save(self.doc)

	def step_row(self, step):
		return next(row for row in self.doc.steps if row.step == step)

	def reset_steps(self):
		for row in self.doc.steps:
			row.status = "Pending"
			row.message = ""
			row.started_on = row.finished_on = None

	def prepare_attempt(self):
		"""Decide on a retry whether to resume or to drop the half-built site and start again."""
		if not os.path.isdir(self.site_path):
			# Nothing on disk, so any earlier "Done" step (e.g. from a dropped site) is void.
			if self.doc.created_by_job:
				self.doc.created_by_job = 0
			self.reset_steps()
			_save(self.doc)
			return

		if not self.doc.created_by_job:
			raise ProvisioningError(
				"reserve",
				_("Site folder {0} already exists and was not created by this system. Nothing was changed.").format(self.site),
			)

		failed_step = self.doc.current_step
		if failed_step in RESTART_STEPS:
			self.drop_site()
			self.reset_steps()
			_save(self.doc)

	@property
	def site_path(self):
		return os.path.join(self.bench_path, "sites", self.site)

	# ------------------------------------------------------------------ steps

	def step_reserve(self):
		try:
			self.cfg = get_site_creation_settings()
		except frappe.ValidationError as e:
			raise ProvisioningError("reserve", _("Bench Settings > Site Creation is not complete: {0}").format(frappe.as_unicode(e)))

		try:
			validate_site_name(self.site)
		except frappe.ValidationError as e:
			raise ProvisioningError("reserve", frappe.as_unicode(e))

		db_name = self.db_name()
		if len(db_name) > 64:
			raise ProvisioningError("reserve", _("Database name {0} is longer than 64 characters.").format(db_name))

		apps_txt = os.path.join(self.bench_path, "sites", "apps.txt")
		with open(apps_txt) as f:
			available = {line.strip() for line in f if line.strip()}
		missing = [a for a in self.cfg["apps"] if a not in available]
		if missing:
			raise ProvisioningError("reserve", _("Apps not available in this bench: {0}").format(", ".join(missing)))

		free_gb = shutil.disk_usage(self.bench_path).free / (1024**3)
		if free_gb < get_settings().min_disk_gb:
			raise ProvisioningError("reserve", _("Only {0:.1f} GB of disk space is free.").format(free_gb))

		self.check_database()

		if not self.doc.get_password("admin_password", raise_exception=False):
			self.doc.admin_password = secrets.token_urlsafe(18)
		return _("Pre-checks passed")

	def step_create_site(self):
		cfg = self.config()
		# Marked before running so that a crash mid-way can still be cleaned up safely on retry.
		self.doc.created_by_job = 1
		_save(self.doc)

		new_site = " ".join(
			[
				cfg["bench_bin"],
				"new-site",
				shlex.quote(self.site),
				"--db-host",
				shlex.quote(cfg["db_host"]),
				"--db-root-username",
				shlex.quote(cfg["db_root_user"]),
				"--db-root-password",
				shlex.quote(cfg["db_root_password"]),
				"--db-name",
				shlex.quote(self.db_name()),
				"--mariadb-user-host-login-scope={}".format(shlex.quote(cfg["db_host_login_scope"])),
				"--admin-password",
				shlex.quote(self.doc.get_password("admin_password")),
				"--no-mariadb-socket",
			]
		)
		commands = [new_site]
		if cfg["developer_mode"]:
			commands.append(f"{cfg['bench_bin']} --site {shlex.quote(self.site)} set-config developer_mode 1")

		self.run_cmd("create_site", commands, _("bench new-site"))
		if not os.path.isfile(os.path.join(self.site_path, "site_config.json")):
			raise ProvisioningError("create_site", _("bench new-site finished but site_config.json was not created."))
		return _("Site created")

	def step_install_apps(self):
		bench = self.config()["bench_bin"]
		installed = self.installed_apps("install_apps")
		todo = [app for app in self.config()["apps"] if app not in installed]

		for app in todo:
			self.run_cmd(
				"install_apps",
				[f"{bench} --site {shlex.quote(self.site)} install-app {shlex.quote(app)}"],
				_("install-app {0}").format(app),
			)
		self.run_cmd("install_apps", [f"{bench} --site {shlex.quote(self.site)} migrate"], _("bench migrate"))
		return _("Installed: {0}").format(", ".join(todo) or _("none (already installed)"))

	def step_verify_apps(self):
		installed = self.installed_apps("verify_apps")
		missing = [app for app in self.config()["apps"] if app not in installed]
		if missing:
			raise ProvisioningError("verify_apps", _("Apps missing after install: {0}").format(", ".join(missing)))
		return _("All {0} apps present").format(len(self.config()["apps"]))

	def step_setup_wizard(self):
		doc = self.doc
		language_name = frappe.db.get_value("Language", doc.language, "language_name") or doc.language
		args = {
			"language": language_name,
			"lang": language_name,  # frappe's wizard reads both keys for non-English languages
			"country": doc.country,
			"timezone": doc.timezone,
			"currency": doc.currency,
			"full_name": doc.first_user_full_name,
			"email": doc.first_user_email,
			"company_name": doc.company_name,
			"company_abbr": doc.company_abbr,
			"chart_of_accounts": doc.chart_of_accounts,
			"fy_start_date": str(doc.fy_start_date),
			"fy_end_date": str(doc.fy_end_date),
			"enable_telemetry": 0,
			"setup_demo": 0,
		}
		# No password: the first user sets one through a link when the site is published.
		payload_file = os.path.join(self.site_path, "private", "saas_setup.json")
		fd = os.open(payload_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
		with os.fdopen(fd, "w") as f:
			json.dump(args, f)
		try:
			out = self.execute_on_site("setup_wizard", "run_setup", {"payload_file": payload_file})
		finally:
			if os.path.exists(payload_file):
				os.remove(payload_file)
		return out

	def step_seed_defaults(self):
		return self.execute_on_site("seed_defaults", "seed_defaults", {})

	def step_apply_license(self):
		return self.execute_on_site(
			"apply_license",
			"apply_license",
			{
				"max_users": self.doc.max_users or None,
				"license_end_date": str(self.doc.license_end_date) if self.doc.license_end_date else None,
			},
		)

	def step_healthcheck(self):
		return self.execute_on_site(
			"healthcheck",
			"healthcheck",
			{
				"company_name": self.doc.company_name,
				"first_user_email": self.doc.first_user_email,
				"apps": self.config()["apps"],
				"max_users": self.doc.max_users or None,
			},
		)

	def step_domain_wait(self):
		return _("Waiting for DNS / SSL mapping and manual verification")

	# ------------------------------------------------------------------ helpers

	def config(self):
		if self.cfg is None:
			try:
				self.cfg = get_site_creation_settings()
			except frappe.ValidationError as e:
				raise ProvisioningError(self.current or "reserve", frappe.as_unicode(e))
		return self.cfg

	def db_name(self):
		return self.config()["db_name_pattern"].format(site_name=self.site)

	def check_database(self):
		import pymysql

		cfg = self.config()
		try:
			pymysql.connect(
				host=cfg["db_host"], user=cfg["db_root_user"], passwd=cfg["db_root_password"]
			).close()
		except Exception as e:
			raise ProvisioningError("reserve", _("Cannot connect to the database server as root: {0}").format(e))

	def run_cmd(self, step, commands, label):
		"""Run commands through Bench Manager's runner; raise with the output tail on failure."""
		key = f"saas-{self.site}-{step}-{frappe.generate_hash(length=6)}"
		self.doc.bench_command = self.last_key = key
		status, console = run_command(
			commands,
			doctype="Customer Site",
			key=key,
			cwd=self.bench_path,
			docname=self.site,
			realtime=False,
		)
		if status != "Success":
			raise ProvisioningError(step, _("{0} failed").format(label), f"Command log: {key}\n\n{self.tail(console)}")
		return console

	def execute_on_site(self, step, function, kwargs):
		"""Run a bench_manager.saas.tenant function inside the customer site (see tenant_runner)."""
		python = os.path.join(self.bench_path, "env", "bin", "python")
		if not os.path.exists(python):
			python = sys.executable
		command = " ".join(
			[
				shlex.quote(python),
				"-m",
				"bench_manager.saas.tenant_runner",
				"--site",
				shlex.quote(self.site),
				"--function",
				shlex.quote(function),
				"--kwargs",
				shlex.quote(json.dumps(kwargs)),
			]
		)
		console = self.run_cmd(step, [command], function)
		# the runner prints the function's return value last
		lines = [line.strip() for line in self.clean(console).splitlines() if line.strip()]
		return lines[-1] if lines else ""

	def installed_apps(self, step):
		bench = self.config()["bench_bin"]
		console = self.run_cmd(step, [f"{bench} --site {shlex.quote(self.site)} list-apps"], "bench list-apps")
		apps = set()
		for line in self.clean(console).splitlines():
			token = line.split(" ")[0].strip()
			if re.fullmatch(r"[a-z][a-z0-9_]*", token):
				apps.add(token)
		return apps

	def drop_site(self):
		"""Remove a half-built site that this system created. Normal drop first (archives the folder),
		forced only if that fails."""
		doc = self.doc
		if not (doc.created_by_job and doc.status in ("Creating", "Configuring") and doc.name == self.site):
			raise ProvisioningError("reserve", _("Refusing to drop a site that was not created by this provisioning job."))

		cfg = self.config()
		base = (
			f"{cfg['bench_bin']} drop-site {shlex.quote(self.site)} "
			f"--db-root-username {shlex.quote(cfg['db_root_user'])} "
			f"--db-root-password {shlex.quote(cfg['db_root_password'])}"
		)
		key = f"saas-{self.site}-cleanup-{frappe.generate_hash(length=6)}"
		status, console = run_command([base], "Customer Site", key, self.bench_path, self.site, realtime=False)
		if status != "Success" or os.path.isdir(self.site_path):
			key = f"{key}-force"
			status, console = run_command(
				[base + " --force --no-backup"], "Customer Site", key, self.bench_path, self.site, realtime=False
			)
		if os.path.isdir(self.site_path):
			raise ProvisioningError("reserve", _("Could not remove the half-built site folder."), self.tail(console))

		doc.created_by_job = 0
		doc.admin_password = secrets.token_urlsafe(18)
		log_event("Cleanup", "Scheduler", self.site, doc.marketing_order_id, detail="Half-built site dropped before retry")

	def clean(self, text):
		return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", (text or "").replace("\r", "\n"))

	def scrub(self, text):
		"""Never let secrets reach an Error Log."""
		for secret in (self.doc.get_password("admin_password", raise_exception=False),):
			if secret:
				text = text.replace(secret, "******")
		cfg = self.cfg or {}
		if cfg.get("db_root_password"):
			text = text.replace(cfg["db_root_password"], "******")
		return text

	def tail(self, console):
		text = self.scrub(self.clean(console))
		return text[-CONSOLE_TAIL_CHARS:]
