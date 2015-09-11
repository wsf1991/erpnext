# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

from __future__ import unicode_literals

import frappe
import datetime
from frappe import _, msgprint, scrub
from frappe.defaults import get_user_permissions
from frappe.utils import add_days, getdate, formatdate, get_first_day, date_diff
from erpnext.utilities.doctype.address.address import get_address_display
from erpnext.utilities.doctype.contact.contact import get_contact_details

class InvalidCurrency(frappe.ValidationError): pass
class InvalidAccountCurrency(frappe.ValidationError): pass

@frappe.whitelist()
def get_party_details(party=None, account=None, party_type="Customer", company=None,
	posting_date=None, price_list=None, currency=None, doctype=None):

	if not party:
		return {}
		
	if not frappe.db.exists(party_type, party):
		frappe.throw(_("{0}: {1} does not exists").format(party_type, party))

	return _get_party_details(party, account, party_type,
		company, posting_date, price_list, currency, doctype)

def _get_party_details(party=None, account=None, party_type="Customer", company=None,
	posting_date=None, price_list=None, currency=None, doctype=None, ignore_permissions=False):
	
	out = frappe._dict(set_account_and_due_date(party, account, party_type, company, posting_date, doctype))

	party = out[party_type.lower()]

	if not ignore_permissions and not frappe.has_permission(party_type, "read", party):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	party = frappe.get_doc(party_type, party)

	set_address_details(out, party, party_type)
	set_contact_details(out, party, party_type)
	set_other_values(out, party, party_type)
	set_price_list(out, party, party_type, price_list)
	out["taxes_and_charges"] = set_taxes(party.name, party_type, posting_date, company, out.customer_group, out.supplier_type)

	if not out.get("currency"):
		out["currency"] = currency

	# sales team
	if party_type=="Customer":
		out["sales_team"] = [{
			"sales_person": d.sales_person,
			"sales_designation": d.sales_designation,
			"allocated_percentage": d.allocated_percentage
		} for d in party.get("sales_team")]

	return out

def set_address_details(out, party, party_type):
	billing_address_field = "customer_address" if party_type == "Lead" \
		else party_type.lower() + "_address"
	out[billing_address_field] = frappe.db.get_value("Address",
		{party_type.lower(): party.name, "is_primary_address":1}, "name")

	# address display
	out.address_display = get_address_display(out[billing_address_field])

	# shipping address
	if party_type in ["Customer", "Lead"]:
		out.shipping_address_name = frappe.db.get_value("Address",
			{party_type.lower(): party.name, "is_shipping_address":1}, "name")
		out.shipping_address = get_address_display(out["shipping_address_name"])

def set_contact_details(out, party, party_type):
	out.contact_person = frappe.db.get_value("Contact",
		{party_type.lower(): party.name, "is_primary_contact":1}, "name")

	if not out.contact_person:
		out.update({
			"contact_person": None,
			"contact_display": None,
			"contact_email": None,
			"contact_mobile": None,
			"contact_phone": None,
			"contact_designation": None,
			"contact_department": None
		})
	else:
		out.update(get_contact_details(out.contact_person))

def set_other_values(out, party, party_type):
	# copy
	if party_type=="Customer":
		to_copy = ["customer_name", "customer_group", "territory"]
	else:
		to_copy = ["supplier_name", "supplier_type"]
	for f in to_copy:
		out[f] = party.get(f)

	# fields prepended with default in Customer doctype
	for f in ['currency'] \
		+ (['sales_partner', 'commission_rate'] if party_type=="Customer" else []):
		if party.get("default_" + f):
			out[f] = party.get("default_" + f)

def set_price_list(out, party, party_type, given_price_list):
	# price list
	price_list = filter(None, get_user_permissions().get("Price List", []))
	if isinstance(price_list, list):
		price_list = price_list[0] if len(price_list)==1 else None

	if not price_list:
		price_list = party.default_price_list

	if not price_list and party_type=="Customer":
		price_list =  frappe.db.get_value("Customer Group",
			party.customer_group, "default_price_list")

	if not price_list:
		price_list = given_price_list

	if price_list:
		out.price_list_currency = frappe.db.get_value("Price List", price_list, "currency")

	out["selling_price_list" if party.doctype=="Customer" else "buying_price_list"] = price_list


def set_account_and_due_date(party, account, party_type, company, posting_date, doctype):
	if doctype not in ["Sales Invoice", "Purchase Invoice"]:
		# not an invoice
		return {
			party_type.lower(): party
		}

	if party:
		account = get_party_account(company, party, party_type)

	account_fieldname = "debit_to" if party_type=="Customer" else "credit_to"

	out = {
		party_type.lower(): party,
		account_fieldname : account,
		"due_date": get_due_date(posting_date, party_type, party, company)
	}
	return out
	
def validate_accounting_currency(party):
	company_currency = get_company_currency()
	
	# set party account currency
	if not party.party_account_currency:
		if party.default_currency:
			party.party_account_currency = party.default_currency
		elif len(set(company_currency.values())) == 1:
				party.party_account_currency = company_currency.values()[0]
			
	party_account_currency_in_db = frappe.db.get_value(party.doctype, party.name, "party_account_currency")
	if party_account_currency_in_db != party.party_account_currency:
		existing_gle = frappe.db.get_value("GL Entry", {"party_type": party.doctype, 
			"party": party.name}, ["name", "account_currency"], as_dict=1)
		if existing_gle:
			if party_account_currency_in_db:
				frappe.throw(_("Accounting Currency cannot be changed, as GL Entry exists for this {0}")
					.format(party.doctype), InvalidCurrency)
			else:
				party.party_account_currency = existing_gle.account_currency
		
		
def validate_party_account(party):
	company_currency = get_company_currency()
	if party.party_account_currency:
		companies_with_different_currency = []
		for company, currency in company_currency.items():
			if currency != party.party_account_currency:
				companies_with_different_currency.append(company)
				
		for d in party.get("accounts"):
			if d.company in companies_with_different_currency:
				companies_with_different_currency.remove(d.company)
			
			selected_account_currency = frappe.db.get_value("Account", d.account, "account_currency")
			if selected_account_currency != party.party_account_currency:
				frappe.throw(_("Account {0} is invalid, account currency must be {1}")
					.format(d.account, selected_account_currency), InvalidAccountCurrency)
				
		if companies_with_different_currency:
			frappe.msgprint(_("Please mention Default {0} Account for the following companies, as accounting currency is different from company's default currency: {1}")
				.format(
					"Receivable" if party.doctype=="Customer" else "Payable", 
					"\n" + "\n".join(companies_with_different_currency)
				)
			)
			
def get_company_currency():
	company_currency = frappe._dict()
	for d in frappe.get_all("Company", fields=["name", "default_currency"]):
		company_currency.setdefault(d.name, d.default_currency)
		
	return company_currency
				
@frappe.whitelist()
def get_party_account(company, party, party_type):
	"""Returns the account for the given `party`.
		Will first search in party (Customer / Supplier) record, if not found,
		will search in group (Customer Group / Supplier Type),
		finally will return default."""
	if not company:
		frappe.throw(_("Please select company first."))

	if party:
		account = frappe.db.get_value("Party Account",
			{"parenttype": party_type, "parent": party, "company": company}, "account")

		if not account:
			party_group_doctype = "Customer Group" if party_type=="Customer" else "Supplier Type"
			group = frappe.db.get_value(party_type, party, scrub(party_group_doctype))
			account = frappe.db.get_value("Party Account",
				{"parenttype": party_group_doctype, "parent": group, "company": company}, "account")

		if not account:
			default_account_name = "default_receivable_account" if party_type=="Customer" else "default_payable_account"
			account = frappe.db.get_value("Company", company, default_account_name)

		return account

@frappe.whitelist()
def get_due_date(posting_date, party_type, party, company):
	"""Set Due Date = Posting Date + Credit Days"""
	due_date = None
	if posting_date and party:
		due_date = posting_date
		if party_type=="Customer":
			credit_days_based_on, credit_days = get_credit_days(party_type, party, company)
			if credit_days_based_on == "Fixed Days" and credit_days:
				due_date = add_days(posting_date, credit_days)
			elif credit_days_based_on == "Last Day of the Next Month":
				due_date = (get_first_day(posting_date, 0, 2) + datetime.timedelta(-1)).strftime("%Y-%m-%d")
		else:
			credit_days = get_credit_days(party_type, party, company)
			if credit_days:
				due_date = add_days(posting_date, credit_days)
				
	return due_date

def get_credit_days(party_type, party, company):
	if party_type and party:
		if party_type == "Customer":
			credit_days_based_on, credit_days, customer_group = \
				frappe.db.get_value(party_type, party, ["credit_days_based_on", "credit_days", "customer_group"])
			
			if not credit_days_based_on:
				credit_days_based_on, credit_days = \
					frappe.db.get_value("Customer Group", customer_group, ["credit_days_based_on", "credit_days"]) \
					or frappe.db.get_value("Company", company, ["credit_days_based_on", "credit_days"])
				
			return credit_days_based_on, credit_days
		else:
			credit_days, supplier_type = frappe.db.get_value(party_type, party, ["credit_days", "supplier_type"])
			if not credit_days:
				credit_days = frappe.db.get_value("Supplier Type", supplier_type, "credit_days") \
					or frappe.db.get_value("Company", company, "credit_days")
					
			return credit_days
			
def validate_due_date(posting_date, due_date, party_type, party, company):
	if getdate(due_date) < getdate(posting_date):
		frappe.throw(_("Due Date cannot be before Posting Date"))
	else:
		default_due_date = get_due_date(posting_date, party_type, party, company)
		if default_due_date != posting_date and getdate(due_date) > getdate(default_due_date):
			is_credit_controller = frappe.db.get_single_value("Accounts Settings", "credit_controller") in frappe.get_roles()
			if is_credit_controller:
				msgprint(_("Note: Due / Reference Date exceeds allowed customer credit days by {0} day(s)")
					.format(date_diff(due_date, default_due_date)))
			else:
				frappe.throw(_("Due / Reference Date cannot be after {0}").format(formatdate(default_due_date)))
				
@frappe.whitelist()
def set_taxes(party, party_type, posting_date, company, customer_group=None, supplier_type=None, billing_address=None, shipping_address=None, for_shopping_cart=None):
	from erpnext.accounts.doctype.tax_rule.tax_rule import get_tax_template, get_party_details
	args = {
		party_type: 		party,
		"customer_group":	customer_group,
		"supplier_type":	supplier_type,
		"company":			company
	}
	
	if billing_address or shipping_address:
		args.update(get_party_details(party, party_type, {"billing_address": billing_address, "shipping_address": shipping_address }))
	else:
		args.update(get_party_details(party, party_type))
	
	if party_type=="Customer":
		args.update({"tax_type": "Sales"})
	else:
		args.update({"tax_type": "Purchase"})
		
	if for_shopping_cart:
		args.update({"use_for_shopping_cart": for_shopping_cart})
		
	return get_tax_template(posting_date, args)