from typing import Dict, List, Optional, Union

import re

import frappe
from frappe import _
from frappe.utils import cstr, now_datetime, strip_html

from vin_chakra.vin_chakra.custom.sales_invoice import check_ticket_can_be_invoiced, get_new_invoice_branch


def _is_technician(user=None):
	"""Returns True if the user is a regular agent/technician (not a manager/admin)."""
	if not user:
		user = frappe.session.user
	if user == "Administrator":
		return False
	roles = frappe.get_roles(user)
	return not ({"Agent Manager", "System Manager"} & set(roles))


def _clean_phone_10(phone: str) -> str:
	"""Extract clean 10-digit phone number without country code or symbols."""
	if not phone:
		return ""
	digits = re.sub(r"\D", "", str(phone))
	if len(digits) == 12 and digits.startswith("91"):
		return digits[2:]
	if len(digits) == 11 and digits.startswith("0"):
		return digits[1:]
	if len(digits) == 10:
		return digits
	if len(digits) > 10:
		return digits[-10:]
	return digits


def _format_display_phone(phone: str) -> str:
	"""Format phone as +91-XXXXXXXXXX if valid 10 digits, else cleaned string."""
	c10 = _clean_phone_10(phone)
	if len(c10) == 10:
		return f"+91-{c10}"
	raw = str(phone or "").strip()
	return raw


def _get_ticket_phone_numbers(ticket) -> dict:
	"""
	Fetch verified phone numbers for a ticket:
	- Primary Mobile:
	  1. Ticket's custom_customer_mobile_number (if set)
	  2. Linked Customer's mobile_no
	  3. Customer's primary contact primary mobile (is_primary_mobile_no=1 or Contact.mobile_no)
	- Secondary Mobile(s):
	  1. Ticket's custom__secondary_phone_number
	  2. Linked Customer's custom_secondary_phone
	  3. Customer's primary contact other phones, or other linked contacts' phones
	- Validates contact:
	  Ensures ticket's contact is actually linked to the customer via Dynamic Link
	  or Customer.customer_primary_contact. Rejects foreign/mismatched contacts.
	- Normalizes and deduplicates:
	  Secondary numbers NEVER duplicate the primary number or each other.
	"""
	t_dict = ticket.as_dict() if hasattr(ticket, "as_dict") else ticket
	cust_name = t_dict.get("customer")

	if not cust_name and t_dict.get("custom_customer_name"):
		c_val = t_dict.get("custom_customer_name")
		if frappe.db.exists("Customer", c_val):
			cust_name = c_val
		else:
			cust_name = frappe.db.get_value("Customer", {"customer_name": c_val}, "name")

	if not cust_name and t_dict.get("custom_customer_mobile_number"):
		c10 = _clean_phone_10(t_dict.get("custom_customer_mobile_number"))
		if len(c10) == 10:
			cust_name = frappe.db.get_value("Customer", {"mobile_no": ["like", f"%{c10}%"]}, "name")

	cust_doc = None
	valid_contacts = set()
	primary_contact = None
	if cust_name and frappe.db.exists("Customer", cust_name):
		cust_doc = frappe.db.get_value(
			"Customer",
			cust_name,
			["name", "customer_name", "mobile_no", "custom_secondary_phone", "customer_primary_contact", "customer_primary_address"],
			as_dict=True
		)
		if cust_doc:
			primary_contact = cust_doc.get("customer_primary_contact")
			dyn_contacts = frappe.get_all(
				"Dynamic Link",
				filters={"link_doctype": "Customer", "link_name": cust_name, "parenttype": "Contact"},
				pluck="parent"
			)
			valid_contacts = set(dyn_contacts)
			if primary_contact:
				valid_contacts.add(primary_contact)

	# Verify ticket's contact against valid customer contacts
	t_contact = t_dict.get("contact")
	if t_contact and t_contact in valid_contacts:
		verified_contact = t_contact
	else:
		verified_contact = primary_contact or (list(valid_contacts)[0] if valid_contacts else None)

	# Primary phone candidates
	primary_candidates = []
	if t_dict.get("custom_customer_mobile_number"):
		primary_candidates.append(t_dict.get("custom_customer_mobile_number"))
	if cust_doc and cust_doc.get("mobile_no"):
		primary_candidates.append(cust_doc.get("mobile_no"))
	if verified_contact and frappe.db.exists("Contact", verified_contact):
		contact_phones = frappe.get_all(
			"Contact Phone",
			filters={"parent": verified_contact},
			fields=["phone", "is_primary_mobile_no", "is_primary_phone"]
		)
		for cp in contact_phones:
			if cp.get("is_primary_mobile_no") or cp.get("is_primary_phone"):
				primary_candidates.append(cp.get("phone"))
		c_mob = frappe.db.get_value("Contact", verified_contact, "mobile_no")
		if c_mob:
			primary_candidates.append(c_mob)

	primary_phone = ""
	primary_10 = ""
	for p in primary_candidates:
		c10 = _clean_phone_10(p)
		if c10 and len(c10) == 10:
			primary_phone = _format_display_phone(p)
			primary_10 = c10
			break
		elif not primary_phone and str(p).strip():
			primary_phone = str(p).strip()

	# Secondary phone candidates
	secondary_candidates = []
	if t_dict.get("custom__secondary_phone_number"):
		secondary_candidates.append(t_dict.get("custom__secondary_phone_number"))
	if t_dict.get("custom_secondary_phone_number"):
		secondary_candidates.append(t_dict.get("custom_secondary_phone_number"))
	if cust_doc and cust_doc.get("custom_secondary_phone"):
		secondary_candidates.append(cust_doc.get("custom_secondary_phone"))
	if verified_contact and frappe.db.exists("Contact", verified_contact):
		contact_phones = frappe.get_all(
			"Contact Phone",
			filters={"parent": verified_contact},
			fields=["phone", "is_primary_mobile_no", "is_primary_phone"]
		)
		for cp in contact_phones:
			if not (cp.get("is_primary_mobile_no") or cp.get("is_primary_phone")):
				secondary_candidates.append(cp.get("phone"))
		c_ph = frappe.db.get_value("Contact", verified_contact, "phone")
		if c_ph:
			secondary_candidates.append(c_ph)

	# Deduplicate secondary phones against primary and each other
	secondary_phones = []
	seen_10 = {primary_10} if primary_10 else set()
	for p in secondary_candidates:
		c10 = _clean_phone_10(p)
		if c10 and len(c10) == 10:
			if c10 not in seen_10:
				seen_10.add(c10)
				secondary_phones.append(_format_display_phone(p))
		else:
			raw = str(p or "").strip()
			if raw and raw not in secondary_phones and raw != primary_phone:
				secondary_phones.append(raw)

	# Build phone_nos_list
	phone_nos_list = []
	if primary_phone:
		phone_nos_list.append({"phone": primary_phone, "is_primary": True, "idx": 1})
	for sp in secondary_phones:
		phone_nos_list.append({"phone": sp, "is_primary": False, "idx": len(phone_nos_list) + 1})

	return {
		"primary_phone": primary_phone,
		"secondary_phones": secondary_phones,
		"phone_nos_list": phone_nos_list,
		"verified_contact": verified_contact,
		"customer_doc": cust_doc,
		"customer_name": (cust_doc.get("customer_name") if cust_doc else None) or cust_name or ""
	}


def enrich_tickets_customer_details(tickets: Union[List[dict], dict]) -> Union[List[dict], dict]:
	"""
	Enrich ticket dictionary/dictionaries with customer details.
	If HD Ticket fields (custom_customer_name, custom_customer_mobile_number, custom_address, etc.)
	are empty or blank, retrieve details from linked Customer, Contact, and Address doctypes.
	Also resolves the valid Customer docname (Link ID) if customer field is missing or unlinked.
	Validates that ticket.contact belongs to ticket's customer; discards mismatched contacts.
	Populates verified primary_phone, secondary_phones, and phone_nos_list.
	"""
	if not tickets:
		return tickets

	is_single = isinstance(tickets, dict)
	ticket_list = [tickets] if is_single else tickets

	# Gather customer and contact names for bulk fetching
	customer_ids = list({t.get("customer") for t in ticket_list if t.get("customer")})
	contact_ids = list({t.get("contact") for t in ticket_list if t.get("contact")})

	# Gather missing customer names & phones for secondary lookup
	missing_names = list({t.get("custom_customer_name") for t in ticket_list if t.get("custom_customer_name") and not t.get("customer")})
	missing_phones = list({t.get("custom_customer_mobile_number") for t in ticket_list if t.get("custom_customer_mobile_number") and not t.get("customer")})

	cust_map = {}
	if customer_ids:
		cust_rows = frappe.get_all(
			"Customer",
			filters={"name": ["in", customer_ids]},
			fields=["name", "customer_name", "mobile_no", "customer_primary_contact", "customer_primary_address", "custom_secondary_phone"]
		)
		for c in cust_rows:
			cust_map[c.name] = c

	if missing_names:
		name_rows = frappe.get_all(
			"Customer",
			filters={"customer_name": ["in", missing_names]},
			fields=["name", "customer_name", "mobile_no", "customer_primary_contact", "customer_primary_address", "custom_secondary_phone"]
		)
		for c in name_rows:
			if c.customer_name not in cust_map:
				cust_map[c.customer_name] = c
			if c.name not in cust_map:
				cust_map[c.name] = c

	if missing_phones:
		clean_missing = [_clean_phone_10(p) for p in missing_phones if _clean_phone_10(p)]
		if clean_missing:
			phone_rows = frappe.get_all(
				"Customer",
				filters={"mobile_no": ["in", clean_missing]},
				fields=["name", "customer_name", "mobile_no", "customer_primary_contact", "customer_primary_address", "custom_secondary_phone"]
			)
			for c in phone_rows:
				c10 = _clean_phone_10(c.mobile_no)
				if c10 not in cust_map:
					cust_map[c10] = c
				if c.name not in cust_map:
					cust_map[c.name] = c

	all_cust_ids = list({c["name"] for c in cust_map.values() if isinstance(c, dict) and c.get("name")})

	# Map customer -> set of valid contacts (primary + dynamic links)
	cust_contacts_map = {c_id: set() for c_id in all_cust_ids}
	for c in cust_map.values():
		if isinstance(c, dict) and c.get("name") and c.get("customer_primary_contact"):
			cust_contacts_map[c["name"]].add(c["customer_primary_contact"])

	if all_cust_ids:
		dyn_links = frappe.get_all(
			"Dynamic Link",
			filters={"link_doctype": "Customer", "link_name": ["in", all_cust_ids], "parenttype": "Contact"},
			fields=["link_name", "parent"]
		)
		for dl in dyn_links:
			cust_contacts_map.setdefault(dl.link_name, set()).add(dl.parent)

	# Fetch primary addresses & dynamic address links
	cust_dyn_addr_map = {}
	if all_cust_ids:
		dyn_addrs = frappe.get_all(
			"Dynamic Link",
			filters={"link_doctype": "Customer", "link_name": ["in", all_cust_ids], "parenttype": "Address"},
			fields=["link_name", "parent"]
		)
		for da in dyn_addrs:
			if da.link_name not in cust_dyn_addr_map:
				cust_dyn_addr_map[da.link_name] = da.parent

	address_ids = set()
	for c in cust_map.values():
		if isinstance(c, dict) and c.get("customer_primary_address"):
			address_ids.add(c["customer_primary_address"])
	address_ids.update(cust_dyn_addr_map.values())

	addr_map = {}
	if address_ids:
		addr_rows = frappe.get_all(
			"Address",
			filters={"name": ["in", list(address_ids)]},
			fields=["name", "address_line1", "city", "state"]
		)
		for a in addr_rows:
			addr_map[a.name] = a

	# Gather all contacts to fetch details
	all_contacts_to_fetch = set()
	for s in cust_contacts_map.values():
		all_contacts_to_fetch.update(s)
	for t in ticket_list:
		if t.get("contact"):
			all_contacts_to_fetch.add(t.get("contact"))

	contact_map = {}
	contact_phones_map = {}
	if all_contacts_to_fetch:
		c_rows = frappe.get_all(
			"Contact",
			filters={"name": ["in", list(all_contacts_to_fetch)]},
			fields=["name", "first_name", "last_name", "mobile_no", "phone"]
		)
		for c in c_rows:
			contact_map[c.name] = c
		cp_rows = frappe.get_all(
			"Contact Phone",
			filters={"parent": ["in", list(all_contacts_to_fetch)]},
			fields=["parent", "phone", "is_primary_mobile_no", "is_primary_phone"]
		)
		for cp in cp_rows:
			contact_phones_map.setdefault(cp.parent, []).append(cp)

	# Enrich each ticket
	for t in ticket_list:
		c_id = t.get("customer")
		cust_doc = None
		if c_id and c_id in cust_map:
			cust_doc = cust_map[c_id]
		elif t.get("custom_customer_name") and t.get("custom_customer_name") in cust_map:
			cust_doc = cust_map[t.get("custom_customer_name")]
		elif t.get("custom_customer_mobile_number"):
			c10 = _clean_phone_10(t.get("custom_customer_mobile_number"))
			if c10 in cust_map:
				cust_doc = cust_map[c10]

		if cust_doc:
			t["customer"] = cust_doc.get("name")
			if not t.get("custom_customer_name") or str(t.get("custom_customer_name")).strip() == "":
				t["custom_customer_name"] = cust_doc.get("customer_name") or cust_doc.get("name")

		# Contact verification: must belong to the resolved customer
		valid_c_for_cust = cust_contacts_map.get(t.get("customer"), set()) if t.get("customer") else set()
		primary_c = cust_doc.get("customer_primary_contact") if cust_doc else None
		curr_contact = t.get("contact")
		if curr_contact and curr_contact in valid_c_for_cust:
			verified_contact = curr_contact
		else:
			verified_contact = primary_c or (list(valid_c_for_cust)[0] if valid_c_for_cust else None)
			if verified_contact:
				t["contact"] = verified_contact

		# Primary phone resolution
		primary_candidates = []
		if t.get("custom_customer_mobile_number"):
			primary_candidates.append(t.get("custom_customer_mobile_number"))
		if cust_doc and cust_doc.get("mobile_no"):
			primary_candidates.append(cust_doc.get("mobile_no"))
		if verified_contact:
			for cp in contact_phones_map.get(verified_contact, []):
				if cp.get("is_primary_mobile_no") or cp.get("is_primary_phone"):
					primary_candidates.append(cp.get("phone"))
			c_info = contact_map.get(verified_contact)
			if c_info and c_info.get("mobile_no"):
				primary_candidates.append(c_info.get("mobile_no"))

		primary_phone = ""
		primary_10 = ""
		for p in primary_candidates:
			c10 = _clean_phone_10(p)
			if c10 and len(c10) == 10:
				primary_phone = _format_display_phone(p)
				primary_10 = c10
				break
			elif not primary_phone and str(p).strip():
				primary_phone = str(p).strip()

		# Secondary phone resolution
		sec_candidates = []
		if t.get("custom__secondary_phone_number"):
			sec_candidates.append(t.get("custom__secondary_phone_number"))
		if t.get("custom_secondary_phone_number"):
			sec_candidates.append(t.get("custom_secondary_phone_number"))
		if cust_doc and cust_doc.get("custom_secondary_phone"):
			sec_candidates.append(cust_doc.get("custom_secondary_phone"))
		if verified_contact:
			for cp in contact_phones_map.get(verified_contact, []):
				if not (cp.get("is_primary_mobile_no") or cp.get("is_primary_phone")):
					sec_candidates.append(cp.get("phone"))
			c_info = contact_map.get(verified_contact)
			if c_info and c_info.get("phone"):
				sec_candidates.append(c_info.get("phone"))

		secondary_phones = []
		seen_10 = {primary_10} if primary_10 else set()
		for p in sec_candidates:
			c10 = _clean_phone_10(p)
			if c10 and len(c10) == 10:
				if c10 not in seen_10:
					seen_10.add(c10)
					secondary_phones.append(_format_display_phone(p))
			else:
				raw = str(p or "").strip()
				if raw and raw not in secondary_phones and raw != primary_phone:
					secondary_phones.append(raw)

		phone_nos_list = []
		if primary_phone:
			phone_nos_list.append({"phone": primary_phone, "is_primary": True, "idx": 1})
		for sp in secondary_phones:
			phone_nos_list.append({"phone": sp, "is_primary": False, "idx": len(phone_nos_list) + 1})

		t["primary_phone"] = primary_phone
		t["secondary_phones"] = secondary_phones
		t["phone_nos_list"] = phone_nos_list

		# Update HD Ticket fields on dictionary
		if not t.get("custom_customer_mobile_number") or str(t.get("custom_customer_mobile_number")).strip() == "":
			t["custom_customer_mobile_number"] = primary_phone
		if (not t.get("custom__secondary_phone_number") or str(t.get("custom__secondary_phone_number")).strip() == "") and secondary_phones:
			t["custom__secondary_phone_number"] = secondary_phones[0]

		# Address
		if not t.get("custom_address") or str(t.get("custom_address")).strip() == "":
			addr_obj = None
			if cust_doc and cust_doc.get("customer_primary_address"):
				addr_obj = addr_map.get(cust_doc.get("customer_primary_address"))
			elif cust_doc and cust_dyn_addr_map.get(cust_doc.get("name")):
				addr_obj = addr_map.get(cust_dyn_addr_map.get(cust_doc.get("name")))

			if addr_obj:
				t["custom_address"] = addr_obj.get("address_line1") or ""
				if not t.get("custom_city__district_"):
					t["custom_city__district_"] = addr_obj.get("city") or ""
				if not t.get("custom_state"):
					t["custom_state"] = addr_obj.get("state") or ""

	return ticket_list[0] if is_single else ticket_list


@frappe.whitelist()
def get_invoice_init_details(ticket_name: str = None, customer: str = None, phone: str = None) -> dict:
	"""
	Helper method to resolve valid Customer Link ID, Phone number, and Machine Type List before creating a Sales Invoice.
	"""
	customer_id = ""
	customer_name = ""
	phone_num = (phone or "").strip()
	if phone_num == "N/A":
		phone_num = ""

	machines = []
	mode_of_payment = ""
	ticket_type = ""
	gst_bill_required = 0

	if ticket_name and frappe.db.exists("HD Ticket", ticket_name):
		check_ticket_can_be_invoiced(ticket_name)
		t_dict = get_ticket_detail(ticket_name)
		customer_id = t_dict.get("customer") or ""
		customer_name = t_dict.get("custom_customer_name") or ""
		if not phone_num:
			phone_num = t_dict.get("primary_phone") or t_dict.get("custom_customer_mobile_number") or ""
		machines = t_dict.get("custom_machine_type_list") or []
		mode_of_payment = t_dict.get("custom_mode_of_payment") or frappe.db.get_value("HD Ticket", ticket_name, "custom_mode_of_payment") or ""
		ticket_type = t_dict.get("ticket_type") or frappe.db.get_value("HD Ticket", ticket_name, "ticket_type") or ""
		raw_gst = t_dict.get("custom_gst_bill_required") or frappe.db.get_value("HD Ticket", ticket_name, "custom_gst_bill_required") or ""
		gst_bill_required = 1 if str(raw_gst).strip().lower() in ["yes", "1", "true"] else 0

	if customer and customer != "N/A":
		cust_str = str(customer).strip()
		if frappe.db.exists("Customer", cust_str):
			customer_id = cust_str
			customer_name = frappe.db.get_value("Customer", cust_str, "customer_name") or cust_str
		else:
			cust_by_name = frappe.db.get_value(
				"Customer",
				{"customer_name": cust_str},
				["name", "customer_name", "mobile_no"],
				as_dict=True
			)
			if cust_by_name:
				customer_id = cust_by_name.name
				customer_name = cust_by_name.customer_name
				if not phone_num and cust_by_name.mobile_no:
					phone_num = cust_by_name.mobile_no

	if customer_id and not phone_num:
		phone_num = frappe.db.get_value("Customer", customer_id, "mobile_no") or ""

	return {
		"customer_id": customer_id,
		"customer_name": customer_name,
		"phone": phone_num,
		"machines": machines,
		"mode_of_payment": mode_of_payment,
		"ticket_type": ticket_type,
		"nature_of_job": ticket_type,
		"gst_bill_required": gst_bill_required,
		"custom_gst_bill_required": gst_bill_required,
		# Shown on the new invoice form; the same rule sets it again on save
		"branch": get_new_invoice_branch(ticket_name, frappe.session.user),
	}


# ---------------------------------------------------------------------------
# Called from the www page (requires login)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_my_tickets() -> list:
	"""Return all HD Tickets assigned to the logged-in technician."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(_("You must be logged in to view your tickets."), frappe.PermissionError)

	escaped_user = frappe.db.escape(user)
	tickets = frappe.db.sql(
		"""
		SELECT
			name,
			subject,
			status,
			priority,
			custom_customer_name,
			custom_customer_mobile_number,
			custom__secondary_phone_number,
			custom_address,
			custom_city__district_,
			custom_state,
			custom_machine_name,
			custom_machine_problem,
			custom_required_spares,
			custom_date,
			creation,
			modified,
			customer,
			contact
		FROM `tabHD Ticket`
		WHERE JSON_SEARCH(`_assign`, 'one', {user}) IS NOT NULL
		ORDER BY creation DESC
		""".format(user=escaped_user),
		as_dict=True,
	)
	return enrich_tickets_customer_details(tickets)


def _get_latest_employee_checkin(employee_id):
	"""Return the employee's most recent Employee Checkin log (any date), or None.

	The *current* attendance state is the log_type of the latest checkin
	regardless of calendar date — this is exactly how HRMS determines whether an
	employee is currently checked in or out. Restricting this to "today" made the
	technician portal disagree with HRMS whenever the last check-in happened on a
	previous day (an overnight shift, or a missed check-out): HRMS showed IN while
	the portal found no log for today and asked the technician to check in again.
	Basing the state on the latest log fixes that mismatch and also avoids any
	midnight/timezone boundary issue from the hardcoded day window.
	"""
	logs = frappe.get_all(
		"Employee Checkin",
		filters={"employee": employee_id},
		fields=["log_type", "latitude", "longitude", "custom_accuracy", "device_id", "time"],
		order_by="time desc",
		limit=1
	)
	return logs[0] if logs else None


def _verify_day_attendance(user):
	"""Return True if the user is currently checked in (latest Employee Checkin log_type == 'IN'), matching HRMS."""
	if user == "Administrator":
		return True
	employees = frappe.get_all("Employee", filters={"user_id": user, "status": "Active"}, pluck="name")
	if not employees:
		return False

	latest = _get_latest_employee_checkin(employees[0])
	return bool(latest and latest.log_type == "IN")


def _send_otp_sms(ticket, target_mobile_number):
	"""Send service OTP SMS via CO3 SMS integration."""
	if not target_mobile_number:
		return False
	try:
		from vin_chakra.vin_chakra.co3_sms import send_sms
		settings = frappe.get_single("CO3 SMS Settings")
		if settings.enabled:
			dlt_template_id = settings.ticket_creation_dlt_id
			raw_template = settings.ticket_creation_template or \
				"Dear Customer, OTP for your service request: {otp}. Share it with the technician after service completion. - Sree Chakra"
			otp = ticket.custom_service_otp
			if "{#var#}" in raw_template:
				message = raw_template.replace("{#var#}", otp, 1)
				if "{#var#}" in message:
					message = message.replace("{#var#}", "24 hours", 1)
			else:
				message = raw_template.replace("{otp}", otp).replace("{ticket_id}", ticket.name)
			
			return send_sms(target_mobile_number, message, dlt_template_id, ticket=ticket.name)
	except Exception as ex:
		frappe.log_error(f"Error sending ticket OTP SMS: {str(ex)}", "CO3 SMS Send Failure")
	return False


@frappe.whitelist()
def get_ticket_detail(ticket_name: str) -> dict:
	"""Return full ticket detail for the technician, including check log."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(_("Authentication required."), frappe.PermissionError)

	ticket = frappe.get_doc("HD Ticket", ticket_name)

	# Verify this ticket is assigned to the technician
	import json
	assigned = json.loads(ticket._assign or "[]")
	if user != "Administrator" and user not in assigned:
		roles = frappe.get_roles(user)
		if not ({"Agent Manager", "System Manager"} & set(roles)):
			frappe.throw(_("You do not have permission to view this ticket."), frappe.PermissionError)

	check_log = frappe.get_all(
		"HD Ticket Check Log",
		filters={"parent": ticket_name, "parenttype": "HD Ticket"},
		fields=["check_type", "timestamp", "technician", "latitude", "longitude", "location_address"],
		order_by="timestamp asc",
	)

	meta = frappe.get_meta("HD Ticket")
	pending_reason_field = meta.get_field("custom_pending_reason")
	pending_reason_options = pending_reason_field.options.split("\n") if pending_reason_field and pending_reason_field.options else ["Test", "Others"]

	mop_field = meta.get_field("custom_mode_of_payment")
	mop_options = mop_field.options.split("\n") if mop_field and mop_field.options else ["Cash", "UPI", "Bank Transfer"]

	gst_field = meta.get_field("custom_gst_bill_required")
	gst_options = gst_field.options.split("\n") if gst_field and gst_field.options else ["Yes", "No"]

	machine_type_list = frappe.get_all(
		"Machine type list",
		filters={"parent": ticket_name, "parenttype": "HD Ticket"},
		fields=["machine_type", "machine_name", "machine_brand", "machine_quantity", "machine_problem", "purchased_at_scs", "purchase_year", "model_no"],
		order_by="idx asc",
	)

	t_dict = {
		"name": ticket.name,
		"subject": ticket.subject,
		"description": ticket.description,
		"status": ticket.status,
		"priority": ticket.priority,
		"custom_customer_name": ticket.custom_customer_name,
		"custom_customer_mobile_number": ticket.custom_customer_mobile_number,
		"custom__secondary_phone_number": ticket.get("custom__secondary_phone_number") or ticket.get("custom_secondary_phone_number") or "",
		"primary_phone": "",
		"secondary_phones": [],
		"phone_nos_list": [],
		"custom_address": ticket.custom_address,
		"custom_city__district_": ticket.custom_city__district_,
		"custom_state": ticket.custom_state,
		"custom_machine_name": ticket.custom_machine_name,
		"custom_machine_problem": ticket.custom_machine_problem,
		"custom_required_spares": ticket.custom_required_spares,
		"custom_date": str(ticket.custom_date) if ticket.custom_date else "",
		"custom_service_otp": ticket.custom_service_otp,
		"custom_mode_of_payment": ticket.get("custom_mode_of_payment") or "",
		"custom_gst_bill_required": ticket.get("custom_gst_bill_required") or "",
		"ticket_type": ticket.get("ticket_type") or "",
		"custom_branch": ticket.get("custom_branch") or "",
		"custom_machine_type_list": machine_type_list,
		"check_log": check_log,
		"pending_reason_options": pending_reason_options,
		"mop_options": mop_options,
		"gst_options": gst_options,
		"customer": ticket.get("customer"),
		"contact": ticket.get("contact"),
		"sales_invoice": frappe.db.get_value(
			"Sales Invoice",
			{"custom_hd_ticket": ticket_name, "docstatus": ["!=", 2]},
			["name", "grand_total", "docstatus"],
			as_dict=True
		),
	}
	return enrich_tickets_customer_details(t_dict)


@frappe.whitelist()
def check_active_ticket(current_ticket_name: str = None) -> dict:
	"""Check if the technician already has an active working ticket."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(_("Authentication required."), frappe.PermissionError)

	if current_ticket_name:
		active_ticket = frappe.db.sql(
			"""
			SELECT name FROM `tabHD Ticket`
			WHERE status = 'Working'
			  AND JSON_SEARCH(`_assign`, 'one', %s) IS NOT NULL
			  AND name != %s
			LIMIT 1
			""",
			(user, current_ticket_name)
		)
	else:
		active_ticket = frappe.db.sql(
			"""
			SELECT name FROM `tabHD Ticket`
			WHERE status = 'Working'
			  AND JSON_SEARCH(`_assign`, 'one', %s) IS NOT NULL
			LIMIT 1
			""",
			(user,)
		)

	if active_ticket:
		ticket_name = active_ticket[0][0]
		return {
			"has_active": True,
			"ticket_name": ticket_name,
			"message": f"Please close your existing ticket ({ticket_name}) before checking into a new one."
		}

	return {"has_active": False}


@frappe.whitelist()
def technician_checkin(ticket_name: str, latitude: float, longitude: float, location_address: str = "", otp_phone_type: str = "primary", secondary_phone: str = "", selected_phone: str = "", skip_otp: Union[bool, int, str] = False, accuracy: float = None) -> dict:
	"""
	Record check-in for a ticket:
	- Verifies day attendance is marked
	- Updates custom__secondary_phone_number if provided
	- Generates a fresh Service OTP (unless skip_otp is True or ticket status is Pending)
	- Sends OTP SMS to chosen phone number (from phone_nos table or direct selection)
	- Appends a 'Check-in' row to the child table
	- Updates ticket status → 'Working'
	"""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(_("Authentication required."), frappe.PermissionError)

	# 1. Day Attendance Verification
	if not _verify_day_attendance(user):
		return {
			"status": "error",
			"message": _("You must check in your day attendance first before checking into a ticket.")
		}

	ticket = frappe.get_doc("HD Ticket", ticket_name)

	# Permission check
	import json
	assigned = json.loads(ticket._assign or "[]")
	if user != "Administrator" and user not in assigned:
		roles = frappe.get_roles(user)
		if not ({"Agent Manager", "System Manager"} & set(roles)):
			frappe.throw(_("You are not assigned to this ticket."), frappe.PermissionError)

	# Check if technician is already working on another ticket
	active_ticket = frappe.db.sql(
		"""
		SELECT name FROM `tabHD Ticket`
		WHERE status = 'Working'
		  AND JSON_SEARCH(`_assign`, 'one', %s) IS NOT NULL
		  AND name != %s
		LIMIT 1
		""",
		(user, ticket_name)
	)
	if active_ticket:
		return {
			"status": "error",
			"message": f"Please close your existing ticket ({active_ticket[0][0]}) before checking into a new one."
		}

	# Prevent double check-in
	existing_logs = frappe.get_all(
		"HD Ticket Check Log",
		filters={"parent": ticket_name, "parenttype": "HD Ticket"},
		fields=["check_type"],
		order_by="timestamp desc",
	)
	if existing_logs and existing_logs[0].check_type == "Check-in":
		return {"status": "error", "message": "Already checked in. Please check out first."}

	# Parse skip_otp option
	if isinstance(skip_otp, str):
		skip_otp = frappe.parse_json(skip_otp)
	should_skip_otp = bool(skip_otp) or (ticket.status == "Pending")

	# Update secondary phone if provided
	eff_sec_phone = selected_phone or secondary_phone
	if eff_sec_phone and str(otp_phone_type).lower() == "secondary":
		sec_phone_clean = eff_sec_phone.strip()
		if sec_phone_clean and not sec_phone_clean.startswith("+"):
			sec_phone_clean = "+91-" + sec_phone_clean
		ticket.custom__secondary_phone_number = sec_phone_clean
		if hasattr(ticket, "custom_secondary_phone_number"):
			ticket.custom_secondary_phone_number = sec_phone_clean

	sms_sent = False
	sms_msg = ""
	target_phone = None

	if not should_skip_otp:
		phone_info = _get_ticket_phone_numbers(ticket)
		if not ticket.custom_customer_mobile_number and phone_info.get("primary_phone"):
			ticket.custom_customer_mobile_number = phone_info["primary_phone"]
		if not ticket.custom_customer_name and phone_info.get("customer_name"):
			ticket.custom_customer_name = phone_info["customer_name"]
		if phone_info.get("verified_contact") and ticket.contact != phone_info["verified_contact"]:
			ticket.contact = phone_info["verified_contact"]

		if selected_phone and selected_phone.strip():
			target_phone = selected_phone.strip()
		elif str(otp_phone_type).lower() == "secondary":
			target_phone = secondary_phone or (phone_info["secondary_phones"][0] if phone_info["secondary_phones"] else "")
		else:
			target_phone = phone_info["primary_phone"] or ticket.custom_customer_mobile_number

		if target_phone:
			target_phone = target_phone.strip()
			if not target_phone.startswith("+"):
				target_phone = "+91-" + target_phone
		else:
			return {"status": "error", "message": _("Target phone number is empty. Please select or enter a valid mobile number.")}

		# Generate a random 4-digit Service OTP
		import random
		otp = str(random.randint(1000, 9999))
		ticket.custom_service_otp = otp

	now = now_datetime()

	# Append child row
	ticket.append("custom_check_log", {
		"check_type": "Check-in",
		"timestamp": now,
		"technician": user,
		"latitude": float(latitude),
		"longitude": float(longitude),
		"accuracy": float(accuracy) if accuracy is not None else None,
		"location_address": location_address,
	})

	# Update status to Working
	ticket.status = "Working"
	ticket.flags.from_technician_api = True
	ticket.save(ignore_permissions=True)
	frappe.db.commit()

	if not should_skip_otp and target_phone:
		# Trigger OTP SMS
		sms_sent = _send_otp_sms(ticket, target_phone)
		sms_msg = f" OTP sent to {target_phone}." if sms_sent else " (Note: SMS delivery failed or disabled)."

	return {
		"status": "success",
		"message": f"Checked in successfully. Ticket status set to Working.{sms_msg}",
		"timestamp": str(now),
		"otp_sent": sms_sent
	}


@frappe.whitelist()
def resend_otp(ticket_name: str, otp_phone_type: str = "primary", secondary_phone: str = "", selected_phone: str = "") -> dict:
	"""Resend OTP SMS for an active ticket."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(_("Authentication required."), frappe.PermissionError)

	ticket = frappe.get_doc("HD Ticket", ticket_name)

	import json
	assigned = json.loads(ticket._assign or "[]")
	if user != "Administrator" and user not in assigned:
		roles = frappe.get_roles(user)
		if not ({"Agent Manager", "System Manager"} & set(roles)):
			frappe.throw(_("You are not assigned to this ticket."), frappe.PermissionError)

	eff_sec_phone = selected_phone or secondary_phone
	if eff_sec_phone and str(otp_phone_type).lower() == "secondary":
		sec_phone_clean = eff_sec_phone.strip()
		if sec_phone_clean and not sec_phone_clean.startswith("+"):
			sec_phone_clean = "+91-" + sec_phone_clean
		ticket.custom__secondary_phone_number = sec_phone_clean
		if hasattr(ticket, "custom_secondary_phone_number"):
			ticket.custom_secondary_phone_number = sec_phone_clean

	phone_info = _get_ticket_phone_numbers(ticket)
	if not ticket.custom_customer_mobile_number and phone_info.get("primary_phone"):
		ticket.custom_customer_mobile_number = phone_info["primary_phone"]
	if not ticket.custom_customer_name and phone_info.get("customer_name"):
		ticket.custom_customer_name = phone_info["customer_name"]
	if phone_info.get("verified_contact") and ticket.contact != phone_info["verified_contact"]:
		ticket.contact = phone_info["verified_contact"]

	if not ticket.custom_service_otp:
		import random
		ticket.custom_service_otp = str(random.randint(1000, 9999))

	ticket.flags.from_technician_api = True
	ticket.save(ignore_permissions=True)
	frappe.db.commit()

	if selected_phone and selected_phone.strip():
		target_phone = selected_phone.strip()
	elif str(otp_phone_type).lower() == "secondary":
		target_phone = secondary_phone or (phone_info["secondary_phones"][0] if phone_info["secondary_phones"] else "")
	else:
		target_phone = phone_info["primary_phone"] or ticket.custom_customer_mobile_number

	if target_phone:
		target_phone = target_phone.strip()
		if not target_phone.startswith("+"):
			target_phone = "+91-" + target_phone
	else:
		return {"status": "error", "message": _("Mobile number is empty. Please select a phone number.")}

	sms_sent = _send_otp_sms(ticket, target_phone)
	if sms_sent:
		return {"status": "success", "message": f"OTP successfully resent to {target_phone}."}
	else:
		return {"status": "error", "message": "Failed to send OTP SMS. Please check SMS settings or mobile number."}


@frappe.whitelist()
def technician_checkout(ticket_name: str, otp: str, latitude: float, longitude: float, location_address: str = "", mode_of_payment: str = "", gst_bill_required: str = "", accuracy: float = None) -> dict:
	"""
	Validate OTP and record check-out for a ticket:
	- Validates the OTP against custom_service_otp
	- Appends a 'Check-out' row to the child table
	- Updates ticket status → 'Resolved'
	"""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(_("Authentication required."), frappe.PermissionError)

	ticket = frappe.get_doc("HD Ticket", ticket_name)

	# Permission check
	import json
	assigned = json.loads(ticket._assign or "[]")
	if user != "Administrator" and user not in assigned:
		roles = frappe.get_roles(user)
		if not ({"Agent Manager", "System Manager"} & set(roles)):
			frappe.throw(_("You are not assigned to this ticket."), frappe.PermissionError)

	# Ensure there's an open check-in first
	existing_logs = frappe.get_all(
		"HD Ticket Check Log",
		filters={"parent": ticket_name, "parenttype": "HD Ticket"},
		fields=["check_type"],
		order_by="timestamp desc",
	)
	if not existing_logs or existing_logs[0].check_type != "Check-in":
		return {"status": "error", "message": "No active check-in found. Please check in first."}

	# Validate OTP
	stored_otp = (ticket.custom_service_otp or "").strip()
	if not stored_otp:
		return {"status": "error", "message": "No OTP is set for this ticket. Contact administrator."}

	if str(otp).strip() != stored_otp:
		return {"status": "error", "message": "Invalid OTP. Please ask the customer for the correct OTP."}

	now = now_datetime()

	# Append check-out row
	ticket.append("custom_check_log", {
		"check_type": "Check-out",
		"timestamp": now,
		"technician": user,
		"latitude": float(latitude),
		"longitude": float(longitude),
		"accuracy": float(accuracy) if accuracy is not None else None,
		"location_address": location_address,
	})

	# Update ticket status to Resolved and save custom fields
	ticket.status = "Resolved"
	if mode_of_payment:
		ticket.custom_mode_of_payment = mode_of_payment
	if gst_bill_required:
		ticket.custom_gst_bill_required = gst_bill_required
	ticket.flags.from_technician_api = True
	ticket.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"status": "success",
		"message": "OTP verified. Checked out successfully. Ticket marked as Resolved.",
		"timestamp": str(now),
	}

@frappe.whitelist()
def technician_mark_pending(ticket_name: str, reason: str, latitude: float, longitude: float, location_address: str = "", custom_reason: str = "", accuracy: float = None) -> dict:
	"""
	Mark a ticket as Pending:
	- Appends a 'Check-out' row to the child table (since they are leaving)
	- Adds a comment to the ticket with the pending reason
	- Updates ticket status → 'Pending'
	- Sets custom_pending_reason and custom_reason
	"""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(_("Authentication required."), frappe.PermissionError)

	ticket = frappe.get_doc("HD Ticket", ticket_name)

	# Permission check
	import json
	assigned = json.loads(ticket._assign or "[]")
	if user != "Administrator" and user not in assigned:
		roles = frappe.get_roles(user)
		if not ({"Agent Manager", "System Manager"} & set(roles)):
			frappe.throw(_("You are not assigned to this ticket."), frappe.PermissionError)

	if not reason or not str(reason).strip():
		return {"status": "error", "message": "Pending reason is mandatory."}

	# Ensure there's an open check-in first
	existing_logs = frappe.get_all(
		"HD Ticket Check Log",
		filters={"parent": ticket_name, "parenttype": "HD Ticket"},
		fields=["check_type"],
		order_by="timestamp desc",
	)
	if not existing_logs or existing_logs[0].check_type != "Check-in":
		return {"status": "error", "message": "No active check-in found. Please check in first."}

	now = now_datetime()

	# Append check-out row
	ticket.append("custom_check_log", {
		"check_type": "Check-out",
		"timestamp": now,
		"technician": user,
		"latitude": float(latitude),
		"longitude": float(longitude),
		"accuracy": float(accuracy) if accuracy is not None else None,
		"location_address": location_address,
	})

	# Update ticket status to Pending and save reasons
	ticket.status = "Pending"
	ticket.custom_pending_reason = reason
	if reason == "Others" and custom_reason:
		ticket.custom_reason = custom_reason
	else:
		ticket.custom_reason = ""
		
	ticket.flags.from_technician_api = True
	ticket.save(ignore_permissions=True)

	# Add a comment for the pending reason
	comment_reason = f"{reason} - {custom_reason}" if reason == "Others" and custom_reason else reason
	frappe.get_doc({
		"doctype": "Comment",
		"comment_type": "Comment",
		"reference_doctype": "HD Ticket",
		"reference_name": ticket_name,
		"content": f"**Marked as Pending**<br>Reason: {comment_reason}",
	}).insert(ignore_permissions=True)

	frappe.db.commit()

	return {
		"status": "success",
		"message": "Ticket marked as Pending.",
		"timestamp": str(now),
	}

@frappe.whitelist()
def get_day_attendance_status() -> dict:
	"""Get current check-in status for today for the logged-in technician from HRMS, including location metadata."""
	user = frappe.session.user
	if user == "Guest":
		return {"status": "error", "message": "Authentication required."}
	
	employees = frappe.get_all("Employee", filters={"user_id": user, "status": "Active"}, pluck="name")
	if not employees:
		return {"status": "error", "message": "No active Employee found for current user."}
	
	employee_id = employees[0]

	# Current state mirrors HRMS: the log_type of the latest checkin, regardless of date.
	latest_log = _get_latest_employee_checkin(employee_id)

	if latest_log and latest_log.log_type == "IN":
		return {
			"status": "success",
			"state": "IN",
			"device_id": latest_log.device_id or "hrms",
			"latitude": latest_log.latitude,
			"longitude": latest_log.longitude,
			"accuracy": latest_log.custom_accuracy,
			"time": str(latest_log.time)
		}

	return {
		"status": "success",
		"state": "OUT",
		"device_id": latest_log.device_id if latest_log else None,
		"latitude": latest_log.latitude if latest_log else None,
		"longitude": latest_log.longitude if latest_log else None,
		"time": str(latest_log.time) if latest_log else None
	}

@frappe.whitelist()
def mark_day_attendance(log_type: str, latitude: float = None, longitude: float = None, accuracy: float = None, device_id: str = "Technician Portal") -> dict:
	"""Mark Day Attendance (IN or OUT) for the technician with location and device_id."""
	user = frappe.session.user
	if user == "Guest":
		return {"status": "error", "message": "Authentication required."}
		
	if log_type not in ["IN", "OUT"]:
		return {"status": "error", "message": "Invalid log type."}
	
	employees = frappe.get_all("Employee", filters={"user_id": user, "status": "Active"}, pluck="name")
	if not employees:
		return {"status": "error", "message": "No active Employee found for current user."}
	
	employee_id = employees[0]

	# Enforce a valid IN/OUT transition against the employee's actual current state
	# (the latest Employee Checkin log, regardless of date — the same definition HRMS
	# and the portal status use), so a stale client button or a duplicate/retried
	# request can never record a state the employee isn't really in, and so an
	# overnight "IN" is correctly treated as still checked in.
	latest_log = _get_latest_employee_checkin(employee_id)
	current_state = latest_log.log_type if latest_log else "OUT"

	if log_type == current_state:
		if log_type == "IN":
			return {"status": "error", "message": "You are already checked in for the day."}
		else:
			return {"status": "error", "message": "You are already checked out. Please check in first."}

	# Determine device ID: use parameter or default to "Technician Portal"
	effective_device_id = device_id.strip() if device_id and str(device_id).strip() else "Technician Portal"

	try:
		checkin = frappe.get_doc({
			"doctype": "Employee Checkin",
			"employee": employee_id,
			"time": frappe.utils.now_datetime(),
			"log_type": log_type,
			"latitude": float(latitude) if latitude is not None and str(latitude).strip() != "" else None,
			"longitude": float(longitude) if longitude is not None and str(longitude).strip() != "" else None,
			"custom_accuracy": float(accuracy) if accuracy is not None and str(accuracy).strip() != "" else None,
			"device_id": effective_device_id
		})
		checkin.insert(ignore_permissions=True)
		frappe.db.commit()
		
		return {
			"status": "success", 
			"message": f"Successfully checked {log_type.lower()}.",
			"state": log_type,
			"device_id": effective_device_id,
			"latitude": checkin.latitude,
			"longitude": checkin.longitude,
			"accuracy": checkin.custom_accuracy
		}
	except Exception as e:
		frappe.log_error("Day Attendance Checkin Failed", str(e))
		return {"status": "error", "message": str(e)}


@frappe.whitelist()
def get_machine_service_charges(item_codes=None):
	"""Fetch custom_service_charge from Item master for given item codes or names."""
	if isinstance(item_codes, str):
		item_codes = frappe.parse_json(item_codes)
	if not item_codes:
		return {}

	if not isinstance(item_codes, list):
		item_codes = [item_codes]

	clean_codes = [str(x).strip() for x in item_codes if x and str(x).strip()]
	if not clean_codes:
		return {}

	rates = {}
	items = frappe.db.sql(
		"""
		SELECT name, item_code, item_name, custom_service_charge, standard_rate, valuation_rate
		FROM `tabItem`
		WHERE name IN (%s) OR item_code IN (%s) OR item_name IN (%s)
		""" % (
			", ".join([frappe.db.escape(x) for x in clean_codes]),
			", ".join([frappe.db.escape(x) for x in clean_codes]),
			", ".join([frappe.db.escape(x) for x in clean_codes]),
		),
		as_dict=True
	)

	for it in items:
		charge = frappe.utils.flt(it.get("custom_service_charge")) or frappe.utils.flt(it.get("standard_rate")) or frappe.utils.flt(it.get("valuation_rate")) or 0
		rates[it.name] = charge
		if it.item_code:
			rates[it.item_code] = charge
		if it.item_name:
			rates[it.item_name] = charge

	return rates



# --- Expense Claims made in the Technician Portal -----------------------------------------
#
# The portal's Expense Claim popup (page/technician_portal/technician_portal.py) keeps one HRMS
# Expense Claim per service ticket and technician, filled in like the paper "Staff Tour
# Settlement" form. The hidden `custom_ticket_claim_key` names the claim's ticket and employee:
# "<ticket>::<employee>", with a unique index, so an employee has at most one draft or submitted
# claim per ticket. A cancelled claim gets "::<claim>" added: the ticket can be claimed again, and
# the new claim is its amendment. Claims without a key (HR's own claims) are untouched.
# Expense Claim's doc_events in hooks.py point here.

# Claims from before one-claim-per-ticket name their ticket first in each line's description:
# "Ticket 0627 · customer · machine"
LINE_TICKET = re.compile(r"\bTicket\s+([^\s·<]+)")


def ticket_claim_key(ticket, employee):
	return f"{ticket}::{employee}"


def key_ticket(key):
	"""The ticket a claim key names."""
	return cstr(key).split("::")[0] or None


def get_line_ticket(description):
	"""The ticket an Expense Claim line was added for, read from its description."""
	match = LINE_TICKET.search(strip_html(description or ""))
	return match.group(1) if match else None


def validate_expense_claim(doc, method=None):
	"""Expense Claim validate (hooks.py): keep one active claim per ticket and employee (also when
	HR changes the employee), and drop a collected amount the customer didn't have to pay."""
	if doc.get("custom_payable_by_customer") != "Yes":
		doc.custom_amount_collected = 0

	ticket = key_ticket(doc.get("custom_ticket_claim_key"))
	if not ticket:
		return
	key = ticket_claim_key(ticket, doc.employee)
	other = frappe.db.get_value(
		"Expense Claim", {"custom_ticket_claim_key": key, "name": ("!=", doc.name)}, "name"
	)
	if other:
		frappe.throw(
			_("Ticket {0} already has Expense Claim {1} for {2}.").format(
				ticket, other, doc.employee_name or doc.employee
			),
			frappe.DuplicateEntryError,
		)
	doc.custom_ticket_claim_key = key


def on_cancel_expense_claim(doc, method=None):
	"""Expense Claim on_cancel (hooks.py): a cancelled claim frees its ticket for a new claim, but
	still names it, so that claim can be its amendment."""
	ticket = key_ticket(doc.get("custom_ticket_claim_key"))
	if ticket:
		key = f"{ticket_claim_key(ticket, doc.employee)}::{doc.name}"
		doc.db_set("custom_ticket_claim_key", key, update_modified=False)


# One-time migration to one claim per ticket: run once per site from patches.txt (an `execute:`
# line in post_model_sync)

# The paper form's expense lines (name, description): created once on a site that lacks them.
# Technicians can pick every Expense Claim Type, not only these.
EXPENSE_FORM_TYPES = [
	("Bus / Train Fare", "Bus or train tickets"),
	("Hotel Bill", "Hotel stay"),
	("Conveyance", "Taxi / Auto / Local bus"),
	("Food", ""),
	("Others", ""),
]

# Header fields a split-off claim copies from the claim it came from
EXPENSE_HEADER_FIELDS = [
	"employee", "company", "posting_date", "expense_approver", "payable_account", "cost_center",
	"remark", "project", "currency", "exchange_rate", "department",
]
EXPENSE_LINE_FIELDS = [
	"expense_date", "expense_type", "description", "amount", "sanctioned_amount", "cost_center",
	"default_account", "project",
]


def migrate_expense_claims_per_ticket():
	"""Technician Portal Expense Claims move to one claim per service ticket (Staff Tour Settlement).

	1. The paper form's expense lines (Bus / Train Fare, Hotel Bill, Conveyance, Food, Others)
	   are created as Expense Claim Types where missing, with the account the existing types use.
	2. Claims made before this change hold one line per ticket, found by "Ticket NNNN" in the
	   line's description, often several tickets in one draft. Each draft is split into one draft
	   per ticket. A submitted claim of one ticket is linked to it; a submitted claim of several
	   tickets is left as it is (the portal reads its tickets from the descriptions).
	"""
	from frappe.utils.fixtures import sync_fixtures

	# post_model_sync patches run before fixtures are synced, and the Expense Claim fields
	# (fixtures/custom_field.json) are needed now
	sync_fixtures("vin_chakra")
	frappe.clear_cache(doctype="Expense Claim")

	_setup_expense_types()
	_split_ticket_claims()


def _setup_expense_types():
	accounts = {}
	for row in frappe.get_all(
		"Expense Claim Account",
		filters={"parent": ["in", ["Food", "Others", "Travel"]]},
		fields=["company", "default_account"],
		order_by="idx asc",
	):
		accounts.setdefault(row.company, row.default_account)

	for name, description in EXPENSE_FORM_TYPES:
		if not frappe.db.exists("Expense Claim Type", name):
			frappe.get_doc({
				"doctype": "Expense Claim Type",
				"expense_type": name,
				"description": description,
				"accounts": [{"company": c, "default_account": a} for c, a in accounts.items()],
			}).insert(ignore_permissions=True)
		elif description and not frappe.db.get_value("Expense Claim Type", name, "description"):
			frappe.db.set_value("Expense Claim Type", name, "description", description, update_modified=False)


def _split_ticket_claims():
	claims = frappe.db.sql_list(
		"""
		SELECT DISTINCT claim.name
		FROM `tabExpense Claim` claim
		JOIN `tabExpense Claim Detail` detail ON detail.parent = claim.name AND detail.parenttype = 'Expense Claim'
		WHERE claim.docstatus < 2 AND IFNULL(claim.custom_ticket_claim_key, '') = ''
			AND detail.description LIKE %s
		ORDER BY claim.creation
		""",
		"%Ticket %",
	)
	for name in claims:
		doc = frappe.get_doc("Expense Claim", name)
		groups, other_lines = {}, []
		for row in doc.expenses:
			ticket = get_line_ticket(row.description)
			if ticket and frappe.db.exists("HD Ticket", ticket):
				groups.setdefault(ticket, []).append(row)
			else:
				other_lines.append(row)
		if not groups:
			continue

		if doc.docstatus == 1:
			ticket = next(iter(groups))
			key = ticket_claim_key(ticket, doc.employee)
			if len(groups) == 1 and not other_lines and not frappe.db.exists("Expense Claim", {"custom_ticket_claim_key": key}):
				frappe.db.set_value(
					"Expense Claim",
					doc.name,
					{**_ticket_header(ticket), "custom_ticket_claim_key": key},
					update_modified=False,
				)
			continue

		# A draft keeps its first ticket (unless it also has lines of no ticket, or that ticket
		# already has a claim of the employee's); the others move
		tickets = list(groups)
		keep = tickets[0] if not other_lines else None
		if keep and frappe.db.exists("Expense Claim", {"custom_ticket_claim_key": ticket_claim_key(keep, doc.employee)}):
			keep = None  # its lines stay here, as below
		for ticket in tickets:
			if ticket == keep:
				continue
			if frappe.db.exists("Expense Claim", {"custom_ticket_claim_key": ticket_claim_key(ticket, doc.employee)}):
				print(f"Expense Claim {doc.name}: ticket {ticket} already has a claim; its lines stay here")
				continue
			new = frappe.new_doc("Expense Claim")
			new.update({f: doc.get(f) for f in EXPENSE_HEADER_FIELDS})
			new.update({"custom_ticket_claim_key": ticket_claim_key(ticket, doc.employee), **_ticket_header(ticket)})
			for row in groups[ticket]:
				new.append("expenses", {f: row.get(f) for f in EXPENSE_LINE_FIELDS})
				doc.remove(row)
			new.flags.ignore_permissions = True
			new.insert()
			print(f"Expense Claim {doc.name}: ticket {ticket} moved to {new.name}")

		if keep:
			doc.custom_ticket_claim_key = ticket_claim_key(keep, doc.employee)
			doc.update(_ticket_header(keep))
		doc.flags.ignore_permissions = True
		doc.save()


def _ticket_header(ticket):
	"""A ticket's claim fields: its customer name, and its Call Type as the Reason for Trip."""
	customer_name, call_type = frappe.db.get_value("HD Ticket", ticket, ["custom_customer_name", "ticket_type"]) or (None, None)
	return {"custom_customer_name": cstr(customer_name), "custom_trip_reason": cstr(call_type)}


def repair_ticket_customer_contacts():
	"""
	Database repair function / patch to fix mismatched contacts and missing customer details on HD Tickets:
	1. Resolves customer from custom_customer_name or phone if unlinked.
	2. Validates ticket.contact against Customer's valid contacts (Dynamic Link & customer_primary_contact).
	   Replaces foreign/mismatched contacts with Customer's legitimate primary contact.
	3. Populates missing custom_customer_name, custom_customer_mobile_number, custom__secondary_phone_number,
	   and address details from Customer master.
	"""
	all_tickets = frappe.get_all(
		"HD Ticket",
		fields=[
			"name",
			"customer",
			"contact",
			"custom_customer_name",
			"custom_customer_mobile_number",
			"custom__secondary_phone_number",
			"custom_address",
			"custom_city__district_",
			"custom_state"
		]
	)

	cust_rows = frappe.get_all(
		"Customer",
		fields=[
			"name",
			"customer_name",
			"mobile_no",
			"custom_secondary_phone",
			"customer_primary_contact",
			"customer_primary_address"
		]
	)
	cust_by_name = {c.name: c for c in cust_rows}
	cust_by_label = {c.customer_name: c for c in cust_rows if c.customer_name}
	cust_by_phone = {}
	for c in cust_rows:
		c10 = _clean_phone_10(c.mobile_no)
		if c10:
			cust_by_phone[c10] = c

	# Dynamic contacts
	dyn_c = frappe.get_all(
		"Dynamic Link",
		filters={"link_doctype": "Customer", "parenttype": "Contact"},
		fields=["link_name", "parent"]
	)
	cust_contacts = {c.name: set() for c in cust_rows}
	for c in cust_rows:
		if c.customer_primary_contact:
			cust_contacts[c.name].add(c.customer_primary_contact)
	for dc in dyn_c:
		if dc.link_name in cust_contacts:
			cust_contacts[dc.link_name].add(dc.parent)

	# Dynamic addresses
	dyn_a = frappe.get_all(
		"Dynamic Link",
		filters={"link_doctype": "Customer", "parenttype": "Address"},
		fields=["link_name", "parent"]
	)
	cust_dyn_addr = {da.link_name: da.parent for da in dyn_a}

	addr_ids = {c.customer_primary_address for c in cust_rows if c.customer_primary_address}
	addr_ids.update(cust_dyn_addr.values())
	addr_map = {}
	if addr_ids:
		addrs = frappe.get_all(
			"Address",
			filters={"name": ["in", list(addr_ids)]},
			fields=["name", "address_line1", "city", "state"]
		)
		for a in addrs:
			addr_map[a.name] = a

	repaired_count = 0
	for t in all_tickets:
		cust = None
		if t.customer and t.customer in cust_by_name:
			cust = cust_by_name[t.customer]
		elif t.custom_customer_name and t.custom_customer_name in cust_by_name:
			cust = cust_by_name[t.custom_customer_name]
		elif t.custom_customer_name and t.custom_customer_name in cust_by_label:
			cust = cust_by_label[t.custom_customer_name]
		elif t.custom_customer_mobile_number:
			c10 = _clean_phone_10(t.custom_customer_mobile_number)
			if c10 in cust_by_phone:
				cust = cust_by_phone[c10]

		changes = {}
		if cust and not t.customer:
			changes["customer"] = cust.name

		if cust:
			valid_c = cust_contacts.get(cust.name, set())
			primary_c = cust.customer_primary_contact or (list(valid_c)[0] if valid_c else None)
			if t.contact not in valid_c:
				if primary_c:
					changes["contact"] = primary_c

			if not t.custom_customer_name:
				changes["custom_customer_name"] = cust.customer_name or cust.name

			if not t.custom_customer_mobile_number and cust.mobile_no:
				changes["custom_customer_mobile_number"] = _format_display_phone(cust.mobile_no)

			if not t.custom__secondary_phone_number and cust.custom_secondary_phone:
				sec_10 = _clean_phone_10(cust.custom_secondary_phone)
				prim_10 = _clean_phone_10(cust.mobile_no or t.custom_customer_mobile_number)
				if sec_10 and sec_10 != prim_10:
					changes["custom__secondary_phone_number"] = _format_display_phone(cust.custom_secondary_phone)

			if not t.custom_address:
				addr_id = cust.customer_primary_address or cust_dyn_addr.get(cust.name)
				if addr_id and addr_id in addr_map:
					addr = addr_map[addr_id]
					changes["custom_address"] = addr.address_line1 or ""
					if not t.custom_city__district_:
						changes["custom_city__district_"] = addr.city or ""
					if not t.custom_state:
						changes["custom_state"] = addr.state or ""

		if changes:
			frappe.db.set_value("HD Ticket", t.name, changes, update_modified=False)
			repaired_count += 1

	frappe.db.commit()
	print(f"repair_ticket_customer_contacts: successfully repaired {repaired_count} tickets.")
	return repaired_count
