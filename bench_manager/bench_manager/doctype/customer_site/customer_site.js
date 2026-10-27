// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("Customer Site", {
	setup(frm) {
		frm.set_query("language", () => ({ filters: { enabled: 1 } }));
	},

	refresh(frm) {
		if (!frm.is_new() && frm.doc.site_url) {
			frm.add_custom_button(__("Open Site"), () => window.open(frm.doc.site_url, "_blank"));
		}
		frm.trigger("load_chart_options");
		if (frm.is_new()) return;

		frm.trigger("show_banners");
		if (frm.is_dirty()) return; // actions run on the saved record

		frm.trigger("provisioning_buttons");
		frm.trigger("domain_buttons");
		frm.trigger("publish_buttons");
		frm.trigger("stop_buttons");
		frm.trigger("admin_buttons");
	},

	// ------------------------------------------------------------------ banners

	show_banners(frm) {
		const d = frm.doc;
		const alert = (text, color) => frm.dashboard.set_headline_alert(text, color);
		const esc = frappe.utils.escape_html;
		const link = (doctype, name, label) =>
			name ? ` <a href="${frappe.utils.get_form_link(doctype, name)}">${label}</a>` : "";

		if (d.status === "Failed") {
			alert(
				__("Site creation failed at step '{0}'. {1}", [d.current_step, esc(d.last_error || "")]) +
					link("Error Log", d.error_log, __("View Error Log")),
				"red"
			);
		} else if (["Creating", "Configuring"].includes(d.status)) {
			alert(__("Site is being created ({0}). This can take several minutes; reload to see progress.", [d.current_step || "..."]), "orange");
		} else if (d.status === "Awaiting Domain Mapping") {
			alert(
				__("Domain check: {0}", [esc(d.domain_check_detail || __("not checked yet"))]) +
					__(" Map the domain, verify the site, then use Mark Ready."),
				d.domain_check_status === "Reachable" ? "green" : "yellow"
			);
		} else if (d.status === "Ready" && !d.publish_to_customer) {
			alert(__("Site is Ready. Tick 'Publish to Customer' to notify the customer."), "blue");
		} else if (d.status === "Published" && d.publish_error) {
			alert(
				__("Published, but the customer may not have been notified: {0}", [esc(d.publish_error)]) +
					link("Error Log", d.publish_error_log, __("View Error Log")),
				"red"
			);
		} else if (d.status === "Published" && !d.email_sent) {
			alert(__("Published. The customer email is being sent..."), "orange");
		}

		if (d.stop_state === "Pending" || d.stop_state === "Backing up") {
			alert(__("Stopping the site ({0})... reload to see progress.", [d.stop_state]), "orange");
		} else if (d.stop_state === "Failed") {
			alert(__("Stopping failed; the site is still running.") + link("Error Log", d.stop_error_log, __("View Error Log")), "red");
		} else if (d.status === "Stopped") {
			alert(__("Site is stopped (maintenance mode). Customers see an 'unavailable' page. Reason: {0}", [esc(d.stop_reason || "")]), "red");
		}
	},

	// ------------------------------------------------------------------ buttons

	provisioning_buttons(frm) {
		if (["Queued", "Failed"].includes(frm.doc.status)) {
			const label = frm.doc.status === "Failed" ? __("Retry Provisioning") : __("Start Provisioning");
			frm.add_custom_button(label, () => {
				frappe.confirm(__("This will create the site now. Continue?"), () => {
					frm.call("start_provisioning").then(() => frm.reload_doc());
				});
			}).addClass("btn-primary");
		}

		if (frm.doc.created_by_job && !frm.doc.admin_password_rotated) {
			frm.add_custom_button(
				__("Show Admin Password"),
				() => {
					frm.call("reveal_admin_password").then((r) => {
						frappe.msgprint({
							title: __("Temporary Administrator Password"),
							message: `<code>${frappe.utils.escape_html(r.message)}</code><br><br>${__("Use this only to verify the site. It is replaced when the site is published.")}`,
						});
					});
				},
				__("Verify")
			);
		}
	},

	domain_buttons(frm) {
		if (!["Awaiting Domain Mapping", "Ready"].includes(frm.doc.status)) return;

		frm.add_custom_button(
			__("Check Domain"),
			() => {
				frm.call("check_domain_now").then((r) => {
					const res = r.message || {};
					frappe.msgprint({
						title: res.ok ? __("Domain is reachable") : __("Domain not reachable"),
						indicator: res.ok ? "green" : "red",
						message: frappe.utils.escape_html(res.message || ""),
					});
					frm.reload_doc();
				});
			},
			__("Verify")
		);

		if (frm.doc.status === "Awaiting Domain Mapping") {
			frm.add_custom_button(__("Mark Ready"), () => {
				frappe.confirm(
					__("Mark this site Ready? It checks that the site answers on its domain first. Only do this after verifying the site."),
					() => frm.call("mark_ready").then(() => frm.reload_doc())
				);
			}).addClass("btn-primary");

			if (frappe.user.has_role("System Manager")) {
				frm.add_custom_button(
					__("Mark Ready (skip domain check)"),
					() => {
						frappe.prompt(
							[{ fieldname: "reason", label: __("Reason"), fieldtype: "Small Text", reqd: 1, description: __("Recorded in the SaaS Event Log.") }],
							(v) => frm.call("mark_ready", { skip_check: 1, reason: v.reason }).then(() => frm.reload_doc()),
							__("Skip the domain check"),
							__("Mark Ready")
						);
					},
					__("Admin")
				);
			}
		}
	},

	publish_buttons(frm) {
		if (frm.doc.status !== "Published") return;
		frm.add_custom_button(
			frm.doc.email_sent ? __("Resend Publish Email") : __("Retry Publish Email"),
			() => {
				frappe.confirm(
					__("Send a new password-setup link to {0}? Any earlier link stops working.", [frm.doc.first_user_email]),
					() => frm.call("resend_publish_email").then(() => frappe.show_alert({ message: __("Queued"), indicator: "green" }))
				);
			},
			__("Publish")
		);
	},

	stop_buttons(frm) {
		const busy = ["Pending", "Backing up"].includes(frm.doc.stop_state);
		if (frm.doc.status === "Stopped") {
			frm.add_custom_button(__("Resume Site"), () => {
				frappe.confirm(__("Bring this site back online?"), () => frm.call("resume_site").then(() => frm.reload_doc()));
			}).addClass("btn-primary");
		} else if (["Awaiting Domain Mapping", "Ready", "Published"].includes(frm.doc.status) && !busy) {
			frm.add_custom_button(
				__("Stop Site"),
				() => {
					frappe.prompt(
						[{ fieldname: "reason", label: __("Reason"), fieldtype: "Small Text", reqd: 1 }],
						(v) => frm.call("stop_site", { reason: v.reason }).then(() => frm.reload_doc()),
						__("Stop this site?"),
						__("Stop Site")
					);
				},
				__("Admin")
			);
		}
	},

	admin_buttons(frm) {
		if (!frappe.user.has_role("System Manager")) return;
		frm.call("get_allowed_statuses").then((r) => {
			const statuses = r.message || [];
			if (!statuses.length) return;
			frm.add_custom_button(
				__("Change Status (override)"),
				() => {
					frappe.prompt(
						[
							{ fieldname: "status", label: __("New Status"), fieldtype: "Select", options: statuses.join("\n"), reqd: 1 },
							{ fieldname: "reason", label: __("Reason"), fieldtype: "Small Text", reqd: 1, description: __("Recorded in the SaaS Event Log.") },
						],
						(values) => frm.call("override_status", values).then(() => frm.reload_doc()),
						__("Change Status Manually"),
						__("Change")
					);
				},
				__("Admin")
			);
		});
	},

	// ------------------------------------------------------------------ locale (same sources as the setup wizard)

	country(frm) {
		frm.set_value("timezone", "");
		if (frm.doc.country) {
			frappe.call({
				method: "frappe.geo.country_info.get_country_timezone_info",
				callback(r) {
					const info = (r.message.country_info || {})[frm.doc.country] || {};
					const timezones = (info.timezones || []).slice().sort();
					frm.set_value("timezone", timezones[0] || "");
				},
			});
			frappe.call({
				method: "bench_manager.saas.utils.get_default_fiscal_year",
				args: { country: frm.doc.country },
				callback(r) {
					if (r.message) frm.set_value("fy_start_date", r.message.fy_start_date);
				},
			});
		}
		frm.set_value("chart_of_accounts", "");
		frm.trigger("load_chart_options");
	},

	load_chart_options(frm) {
		if (!frm.doc.country) {
			frm.set_df_property("chart_of_accounts", "options", "");
			return;
		}
		frappe.call({
			method: "erpnext.accounts.doctype.account.chart_of_accounts.chart_of_accounts.get_charts_for_country",
			args: { country: frm.doc.country, with_standard: true },
			callback(r) {
				const charts = r.message || [];
				frm.set_df_property("chart_of_accounts", "options", ["", ...charts].join("\n"));
				frm.refresh_field("chart_of_accounts");
			},
		});
	},
});
