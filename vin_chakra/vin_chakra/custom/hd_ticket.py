import re

import frappe
from frappe import _
from frappe.desk.form.assign_to import add as add_assign


def _is_technician(user=None):
	"""Returns True if the user is a regular agent/technician (not a manager/admin)."""
	if not user:
		user = frappe.session.user
	if user in ["Administrator", "Guest"]:
		return False
	roles = frappe.get_roles(user)
	return not ({"Agent Manager", "System Manager", "Chief Technician"} & set(roles))


def sync_customer_details(doc):
	"""
	Keep HD Ticket synchronized with linked Customer master:
	1. If customer link is missing, resolve it from custom_customer_name or phone.
	2. If contact is mismatched (belongs to another customer or an employee),
	   realign it with Customer's primary contact or linked contact.
	3. Populate blank customer name, mobile, secondary phone, and address from Customer master.
	"""
	customer_name = doc.get("customer")
	if not customer_name and doc.get("custom_customer_name"):
		c_val = doc.get("custom_customer_name")
		if frappe.db.exists("Customer", c_val):
			customer_name = c_val
		else:
			customer_name = frappe.db.get_value("Customer", {"customer_name": c_val}, "name")
		if customer_name:
			doc.customer = customer_name

	if not customer_name and doc.get("custom_customer_mobile_number"):
		raw_digits = re.sub(r"\D", "", str(doc.get("custom_customer_mobile_number")))
		c10 = raw_digits[-10:] if len(raw_digits) >= 10 else raw_digits
		if len(c10) == 10:
			customer_name = frappe.db.get_value("Customer", {"mobile_no": ["like", f"%{c10}%"]}, "name")
			if customer_name:
				doc.customer = customer_name

	if not customer_name or not frappe.db.exists("Customer", customer_name):
		return

	cust_doc = frappe.db.get_value(
		"Customer",
		customer_name,
		["name", "customer_name", "mobile_no", "custom_secondary_phone", "customer_primary_contact", "customer_primary_address"],
		as_dict=True
	)
	if not cust_doc:
		return

	# Set customer name
	if not doc.get("custom_customer_name"):
		doc.custom_customer_name = cust_doc.customer_name or cust_doc.name

	# Verify contact against Customer's valid contacts
	primary_contact = cust_doc.get("customer_primary_contact")
	valid_contacts = set(
		frappe.get_all(
			"Dynamic Link",
			filters={"link_doctype": "Customer", "link_name": customer_name, "parenttype": "Contact"},
			pluck="parent"
		)
	)
	if primary_contact:
		valid_contacts.add(primary_contact)

	current_contact = doc.get("contact")
	if current_contact not in valid_contacts:
		# Mismatched or missing contact -> realign with customer's legitimate primary contact
		doc.contact = primary_contact or (list(valid_contacts)[0] if valid_contacts else None)

	# Set primary mobile
	if not doc.get("custom_customer_mobile_number") and cust_doc.get("mobile_no"):
		m = str(cust_doc.mobile_no).strip()
		if m and not m.startswith("+"):
			m = f"+91-{m}"
		doc.custom_customer_mobile_number = m

	# Set secondary mobile
	if not doc.get("custom__secondary_phone_number") and cust_doc.get("custom_secondary_phone"):
		sec_raw = str(cust_doc.custom_secondary_phone).strip()
		sec_digits = re.sub(r"\D", "", sec_raw)[-10:]
		prim_digits = re.sub(r"\D", "", str(cust_doc.mobile_no or doc.get("custom_customer_mobile_number") or ""))[-10:]
		if sec_digits and sec_digits != prim_digits:
			if sec_raw and not sec_raw.startswith("+"):
				sec_raw = f"+91-{sec_raw}"
			doc.custom__secondary_phone_number = sec_raw

	# Set address
	if not doc.get("custom_address"):
		addr_id = cust_doc.get("customer_primary_address")
		if not addr_id:
			addr_links = frappe.get_all(
				"Dynamic Link",
				filters={"link_doctype": "Customer", "link_name": customer_name, "parenttype": "Address"},
				pluck="parent"
			)
			if addr_links:
				addr_id = addr_links[0]
		if addr_id and frappe.db.exists("Address", addr_id):
			addr = frappe.db.get_value("Address", addr_id, ["address_line1", "city", "state"], as_dict=True)
			if addr:
				doc.custom_address = addr.address_line1 or ""
				if not doc.get("custom_city__district_"):
					doc.custom_city__district_ = addr.city or ""
				if not doc.get("custom_state"):
					doc.custom_state = addr.state or ""


def fix_phone_numbers(doc, method=None):
	"""Ensure phone fields have a country code and customer details are aligned before validation."""
	sync_customer_details(doc)

	phone_fields = [df.fieldname for df in doc.meta.fields if df.fieldtype == "Phone"]
	for field in phone_fields:
		val = doc.get(field)
		if val:
			val = val.strip()
			if val == "+":
				doc.set(field, "")
			elif not val.startswith("+"):
				doc.set(field, f"+91-{val}")


def assign_to_chief_technician(doc, method):
	chief_technicians = frappe.get_all('Has Role', filters={'role': 'Chief Technician', 'parenttype': 'User'}, fields=['parent'])
	
	users_to_assign = []
	for d in chief_technicians:
		if frappe.db.get_value('User', d.parent, 'enabled'):
			users_to_assign.append(d.parent)
	users_to_assign = list(set(users_to_assign))
	
	frappe.flags.in_auto_assignment = True
	for user in users_to_assign:
		try:
			add_assign({
				'assign_to': [user],
				'doctype': doc.doctype,
				'name': doc.name,
				'description': 'New HD Ticket created via portal'
			})
		except Exception as e:
			frappe.log_error(message=frappe.get_traceback(), title=f"Failed to assign ticket {doc.name} to {user}")
	frappe.flags.in_auto_assignment = False


def get_assignee_restricted_ticket_query(user=None):
	"""
	Custom permission_query_conditions for HD Ticket.

	Rules:
	- Administrator OR Agent Manager / System Manager → see ALL tickets.
	- Regular agents / technicians → only see tickets they are assigned to
	  (i.e., their email appears in the `_assign` JSON column).
	"""
	if not user:
		user = frappe.session.user

	# Admins and managers see everything
	if user == "Administrator":
		return None

	user_roles = frappe.get_roles(user)
	if "Agent Manager" in user_roles or "System Manager" in user_roles or "Chief Technician" in user_roles:
		return None

	# Regular agent / technician: only show their assigned tickets
	escaped_user = frappe.db.escape(user)
	return (
		"JSON_SEARCH(`tabHD Ticket`.`_assign`, 'one', {user}) IS NOT NULL"
	).format(user=escaped_user)


def validate_ticket(doc, method=None):
	"""
	Doctype level validation rules for HD Ticket:
	1. Only one Working ticket at a time per technician assignee.
	2. Restrict technicians from modifying details like assignee, team (agent_group), branch or status in Desk.
	3. Validate check-in/check-out location (lat, long) and timestamp logs.
	"""
	# 1. Enforce one Working ticket at a time per technician assignee
	if doc.status == "Working":
		import json
		assignees = json.loads(doc._assign or "[]")
		for assignee in assignees:
			if _is_technician(assignee):
				active_ticket = frappe.db.sql(
					"""
					SELECT name FROM `tabHD Ticket`
					WHERE status = 'Working'
					  AND JSON_SEARCH(`_assign`, 'one', %s) IS NOT NULL
					  AND name != %s
					LIMIT 1
					""",
					(assignee, doc.name)
				)
				if active_ticket:
					frappe.throw(
						_("Technician {0} is already working on ticket {1}. Please close or pause it first.")
						.format(assignee, active_ticket[0][0])
					)

	# 2. Restrict technicians from modifying key ticket details (team/agent_group, branch, status) directly in Desk
	if _is_technician(frappe.session.user):
		old_doc = doc.get_doc_before_save()
		if old_doc:
			# Prevent team/agent_group modification
			if doc.agent_group != old_doc.agent_group:
				frappe.throw(_("Technicians are not allowed to change the Team (Agent Group)."))

			# Prevent branch modification (no branch is NULL on older tickets, "" once cleared)
			if (doc.get("custom_branch") or "") != (old_doc.get("custom_branch") or ""):
				frappe.throw(_("Technicians are not allowed to change the Branch."))

			# Prevent status modification directly in Desk
			if doc.status != old_doc.status and not doc.flags.from_technician_api:
				frappe.throw(_("Technicians are not allowed to change the ticket status directly. Please use the Technician Portal."))

	# 3. Validate check-in/check-out location (lat, long) and timestamp logs
	if doc.status in ["Working", "Resolved", "Pending"]:
		if _is_technician(frappe.session.user) or doc.flags.from_technician_api:
			logs = doc.get("custom_check_log") or []
			expected_type = "Check-in" if doc.status == "Working" else "Check-out"
			
			if not logs:
				frappe.throw(_("No check log found. Please perform check-in/check-out via the Technician Portal."))
			
			last_log = logs[-1]
			if last_log.check_type != expected_type:
				frappe.throw(_("Invalid check log sequence. Last log must be a {0}.").format(expected_type))
			
			if not last_log.latitude or not last_log.longitude or not last_log.timestamp:
				frappe.throw(_("Check-in/Check-out log must contain latitude, longitude, and timestamp."))


def validate_todo_assignment(todo, method=None):
	"""
	Event hook for ToDo to restrict technicians from assigning or unassigning tickets.
	"""
	if frappe.flags.in_auto_assignment:
		return

	if todo.reference_type == "HD Ticket":
		if _is_technician(frappe.session.user):
			frappe.throw(_("Technicians are not allowed to assign or unassign tickets."))


def clear_dashboard_caches(doc, method=None):
	"""Clears Redis cache keys for dashboards when tickets or check-ins change."""
	try:
		frappe.cache().delete_keys("tech_portal:*")
		frappe.cache().delete_keys("chief_dash:*")
		frappe.cache().delete_keys("tech_map:*")
	except Exception:
		pass

