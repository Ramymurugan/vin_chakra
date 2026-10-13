import re

import frappe
from frappe import _

ABBR_PATTERN = re.compile(r"^[A-Z]{2}$")


def normalize_abbr(value: str | None) -> str:
	"""Trim and uppercase an abbreviation. Returns an empty string for blank values."""
	return (value or "").strip().upper()


def is_valid_abbr(value: str | None) -> bool:
	return bool(ABBR_PATTERN.match(value or ""))


def validate_abbr(doc, method=None):
	"""Employee.validate: abbreviation must be exactly two letters and unique across all employees."""
	abbr = normalize_abbr(doc.get("custom_abbr"))
	doc.custom_abbr = abbr or None

	if not abbr:
		return

	if not is_valid_abbr(abbr):
		frappe.throw(
			_("Abbreviation {0} is invalid. It must be exactly two letters, for example AB.").format(
				frappe.bold(abbr)
			),
			title=_("Invalid Abbreviation"),
		)

	# Covers every status, including Left: old invoice numbers still carry the abbreviation.
	duplicate = frappe.db.get_value(
		"Employee",
		{"custom_abbr": abbr, "name": ("!=", doc.name)},
		["name", "employee_name"],
		as_dict=True,
	)
	if duplicate:
		frappe.throw(
			_("Abbreviation {0} is already used by employee {1} ({2}).").format(
				frappe.bold(abbr), duplicate.name, duplicate.employee_name
			),
			title=_("Duplicate Abbreviation"),
		)

	if not doc.is_new() and doc.has_value_changed("custom_abbr"):
		old_abbr = (doc.get_doc_before_save() or {}).get("custom_abbr")
		from vin_chakra.vin_chakra.custom.sales_invoice import abbr_in_use

		if old_abbr and abbr_in_use(old_abbr):
			frappe.throw(
				_(
					"Abbreviation {0} cannot be changed because Sales Invoices were already numbered with it."
				).format(frappe.bold(old_abbr)),
				title=_("Abbreviation In Use"),
			)
