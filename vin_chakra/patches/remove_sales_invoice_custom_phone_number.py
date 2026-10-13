import frappe


def execute():
	"""Delete deprecated Custom Fields that were removed from source.

	Removing a field from the exported customization/fixture files never deletes
	it from a site's database on migrate (sync_fixtures/sync_customizations only
	insert or update) -- so any site that already had these fields needs this
	one-time cleanup.
	"""
	frappe.delete_doc(
		"Custom Field",
		"Sales Invoice-custom_phone_number",
		ignore_missing=True,
		ignore_permissions=True,
	)
	frappe.clear_cache(doctype="Sales Invoice")

	frappe.delete_doc(
		"Custom Field",
		"Quotation-custom_sales_officer_signature",
		ignore_missing=True,
		ignore_permissions=True,
	)
	frappe.clear_cache(doctype="Quotation")
