# -*- coding: utf-8 -*-
# Copyright (c) 2017, Frappé and contributors
# For license information, please see license.txt


import os
import re
import shlex
import time
from email.utils import parseaddr
from importlib import metadata as importlib_metadata
from subprocess import PIPE, STDOUT, Popen, check_output

import frappe
from bench_manager.bench_manager.utils import (
	safe_decode,
	verify_whitelisted_call,
)
from frappe.model.document import Document


def get_app_metadata(app_name):
	"""Return version/summary/author details of an installed app, or None.

	Frappe v16 apps are installed from pyproject.toml, so the details live in the
	package's dist-info. The legacy <app>.egg-info/PKG-INFO file is used as fallback.
	"""
	try:
		meta = importlib_metadata.metadata(app_name)
	except importlib_metadata.PackageNotFoundError:
		meta = None

	if meta is not None:
		author = meta.get("Author")
		author_email = meta.get("Author-email") or ""
		name, address = parseaddr(author_email)
		return {
			"version": meta.get("Version") or "",
			"summary": meta.get("Summary") or "",
			"author": author or name or "",
			"author_email": address or author_email,
		}

	pkg_info_file = os.path.join("..", "apps", app_name, "{0}.egg-info".format(app_name), "PKG-INFO")
	if not os.path.isfile(pkg_info_file):
		return None

	with open(pkg_info_file, "r") as f:
		data = frappe.as_unicode(f.read())
	result = {"version": "", "summary": "", "author": "", "author_email": ""}
	for key, field in (
		("Version", "version"),
		("Summary", "summary"),
		("Author", "author"),
		("Author-email", "author_email"),
	):
		found = re.search("^{0}: (.*)$".format(key), data, flags=re.MULTILINE)
		if found:
			result[field] = found.group(1).strip()
	return result


class App(Document):
	app_info_fields = [
		"app_title",
		"app_description",
		"app_publisher",
		"app_email",
		"app_icon",
		"app_color",
		"app_license",
	]

	def validate(self):
		if self.get("__islocal"):
			if self.developer_flag == 0:
				frappe.throw("Creation of new apps is not supported at the moment!")
			self.developer_flag = 0
			# wait briefly for app metadata (e.g. right after get-app), but never forever
			for _attempt in range(5):
				if get_app_metadata(self.app_name):
					break
				time.sleep(2)
			self.update_app_details()
		else:
			if self.developer_flag == 0:
				self.update_app_details()

	def onload(self):
		self.update_app_details()

	def get_attr(self, varname):
		return getattr(self, varname)

	def set_attr(self, varname, varval):
		return setattr(self, varname, varval)

	def after_command(self, commands=None):
		frappe.publish_realtime("Bench-Manager:reload-page")

	def on_trash(self):
		if self.developer_flag == 0:
			frappe.throw("Not allowed!")
		else:
			apps_file = "apps.txt"
			with open(apps_file, "r") as f:
				apps = f.readlines()
			try:
				apps.remove(self.app_name)
			except:
				try:
					apps.remove(self.app_name + "\n")
				except:
					pass
			os.remove(apps_file)
			with open(apps_file, "w") as f:
				f.writelines(apps)
			if self.app_name != "":
				check_output(shlex.split("rm -r ../apps/{app_name}".format(app_name=self.app_name)))

	def update_app_details(self):
		app_meta = get_app_metadata(self.app_name)
		if app_meta:
			self.version = app_meta["version"]
			self.app_description = app_meta["summary"]
			self.app_publisher = app_meta["author"]
			self.app_email = app_meta["author_email"]
			self.app_title = self.app_name
			self.app_title = self.app_title.replace("-", " ")
			self.app_title = self.app_title.replace("_", " ")
			if os.path.isdir(os.path.join("..", "apps", self.app_name, ".git")):
				self.current_git_branch = safe_decode(
					check_output(
						"git rev-parse --abbrev-ref HEAD".split(),
						cwd=os.path.join("..", "apps", self.app_name),
					)
				).strip("\n")
				self.is_git_repo = True
			else:
				self.current_git_branch = None
				self.is_git_repo = False
		else:
			frappe.throw(
				"Hey developer, the app you're trying to create an \
				instance of doesn't actually exist. You could consider setting \
				developer flag to 0 to actually create the app"
			)

	@frappe.whitelist()
	def pull_rebase(self, key, remote):
		remote, branch_name = remote.split("/")
		self.console_command(
			key=key, caller="pull-rebase", branch_name=branch_name, remote=remote
		)

	@frappe.whitelist()
	def console_command(self, key, caller, branch_name=None, remote=None, commit_msg=None):
		commands = {
			"git_init": ["git init", "git add .", "git commit -m 'Initial Commit'"],
			"switch_branch": ["git checkout {branch_name}".format(branch_name=branch_name)],
			"new_branch": ["git branch {branch_name}".format(branch_name=branch_name)],
			"delete_branch": ["git branch -D {branch_name}".format(branch_name=branch_name)],
			"git_fetch": ["git fetch --all"],
			"track-remote": [
				"git checkout -b {branch_name} -t {remote}".format(
					branch_name=branch_name, remote=remote
				)
			],
			"pull-rebase": [
				"git pull --rebase {remote} {branch_name}".format(
					branch_name=branch_name, remote=remote
				)
			],
			"commit": [
				"git add .",
				'git commit -m "{commit_msg}"'.format(commit_msg=commit_msg),
			],
			"stash": ["git add .", "git stash"],
			"apply-stash": ["git stash apply"],
		}
		frappe.enqueue(
			"bench_manager.bench_manager.utils.run_command",
			commands=commands[caller],
			cwd=os.path.join("..", "apps", self.name),
			doctype=self.doctype,
			key=key,
			docname=self.name,
		)


@frappe.whitelist()
def get_branches(doctype, docname, current_branch):
	verify_whitelisted_call()
	app_path = os.path.join("..", "apps", docname)  #'../apps/'+docname
	branches = (check_output("git branch".split(), cwd=app_path)).split()
	branches.remove("*")
	branches.remove(current_branch)
	return branches


@frappe.whitelist()
def get_remotes(docname):
	command = "git branch -r"
	remotes = (
		safe_decode(
			check_output(shlex.split(command), cwd=os.path.join("..", "apps", docname))
		)
		.strip("\n")
		.split("\n  ")
	)
	remotes = [remote for remote in remotes if "HEAD" not in remote]
	return remotes
