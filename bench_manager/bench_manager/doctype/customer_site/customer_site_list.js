// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

frappe.listview_settings["Customer Site"] = {
	add_fields: ["status", "publish_to_customer"],
	get_indicator(doc) {
		const colors = {
			Queued: "grey",
			Creating: "orange",
			Configuring: "orange",
			"Awaiting Domain Mapping": "yellow",
			Ready: "blue",
			Published: "green",
			Stopped: "red",
			Failed: "red",
		};
		return [__(doc.status), colors[doc.status] || "grey", "status,=," + doc.status];
	},
};
