# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

from __future__ import unicode_literals
import frappe, erpnext
import frappe.defaults
from frappe import _
from frappe.utils import cstr, cint, flt, comma_or, getdate, nowdate, formatdate, format_time
from erpnext.stock.utils import get_incoming_rate
from erpnext.stock.stock_ledger import get_previous_sle, NegativeStockError, get_valuation_rate
from erpnext.stock.get_item_details import get_bin_details, get_default_cost_center, get_conversion_factor, get_reserved_qty_for_so
from erpnext.setup.doctype.item_group.item_group import get_item_group_defaults
from erpnext.setup.doctype.brand.brand import get_brand_defaults
from erpnext.stock.doctype.batch.batch import get_batch_no, set_batch_nos, get_batch_qty
from erpnext.stock.doctype.item.item import get_item_defaults
from erpnext.manufacturing.doctype.bom.bom import validate_bom_no, add_additional_cost
from erpnext.stock.utils import get_bin
from frappe.model.mapper import get_mapped_doc
from erpnext.stock.doctype.serial_no.serial_no import update_serial_nos_after_submit, get_serial_nos
from erpnext.stock.doctype.stock_reconciliation.stock_reconciliation import OpeningEntryAccountError
from erpnext.accounts.general_ledger import process_gl_map
<<<<<<< HEAD
from erpnext.controllers.taxes_and_totals import init_landed_taxes_and_totals
import json
=======
import json, copy
>>>>>>> feat: removed Is Submittable property for the BOM doctype

from six import string_types, itervalues, iteritems

class IncorrectValuationRateError(frappe.ValidationError): pass
class DuplicateEntryForWorkOrderError(frappe.ValidationError): pass
class OperationsNotCompleteError(frappe.ValidationError): pass
class MaxSampleAlreadyRetainedError(frappe.ValidationError): pass

from erpnext.controllers.stock_controller import StockController

form_grid_templates = {
	"items": "templates/form_grid/stock_entry_grid.html"
}

class StockEntry(StockController):
	def get_feed(self):
		return self.stock_entry_type

	def onload(self):
		for item in self.get("items"):
			item.update(get_bin_details(item.item_code, item.s_warehouse))

	def before_validate(self):
		from erpnext.stock.doctype.putaway_rule.putaway_rule import apply_putaway_rule
		apply_rule = self.apply_putaway_rule and (self.purpose in ["Material Transfer", "Material Receipt"])

		if self.get("items") and apply_rule:
			apply_putaway_rule(self.doctype, self.get("items"), self.company,
				purpose=self.purpose)

	def validate(self):
		self.wo_doc = frappe._dict()
		if self.work_order:
			self.wo_doc = frappe.get_doc('Work Order', self.work_order)

		self.validate_posting_time()
		self.validate_purpose()
		self.validate_item()
		self.validate_customer_provided_item()
		self.validate_qty()
		self.set_transfer_qty()
		self.validate_uom_is_integer("uom", "qty")
		self.validate_uom_is_integer("stock_uom", "transfer_qty")
		self.validate_warehouse()
		self.validate_work_order()
		self.validate_bom()
		self.mark_finished_and_scrap_items()
		self.validate_finished_goods()
		self.validate_with_material_request()
		self.validate_batch()
		self.validate_inspection()
		self.validate_fg_completed_qty()
		self.validate_difference_account()
		self.set_job_card_data()
		self.set_purpose_for_stock_entry()

		if not self.from_bom:
			self.fg_completed_qty = 0.0

		if self._action == 'submit':
			self.make_batches('t_warehouse')
		else:
			set_batch_nos(self, 's_warehouse')

		self.validate_serialized_batch()
		self.set_actual_qty()
		self.calculate_rate_and_amount()
		self.validate_putaway_capacity()

	def on_submit(self):
		self.update_stock_ledger()

		update_serial_nos_after_submit(self, "items")
		self.update_work_order()
		self.validate_purchase_order()
		if self.purchase_order and self.purpose == "Send to Subcontractor":
			self.update_purchase_order_supplied_items()

		self.make_gl_entries()

		self.repost_future_sle_and_gle()
		self.update_cost_in_project()
		self.validate_reserved_serial_no_consumption()
		self.update_transferred_qty()
		self.update_quality_inspection()

		if self.work_order and self.purpose == "Manufacture":
			self.update_so_in_serial_number()

		if self.purpose == 'Material Transfer' and self.add_to_transit:
			self.set_material_request_transfer_status('In Transit')
		if self.purpose == 'Material Transfer' and self.outgoing_stock_entry:
			self.set_material_request_transfer_status('Completed')

	def on_cancel(self):

		if self.purchase_order and self.purpose == "Send to Subcontractor":
			self.update_purchase_order_supplied_items()

		if self.work_order and self.purpose == "Material Consumption for Manufacture":
			self.validate_work_order_status()

		self.update_work_order()
		self.update_stock_ledger()

		self.ignore_linked_doctypes = ('GL Entry', 'Stock Ledger Entry', 'Repost Item Valuation')

		self.make_gl_entries_on_cancel()
		self.repost_future_sle_and_gle()
		self.update_cost_in_project()
		self.update_transferred_qty()
		self.update_quality_inspection()
		self.delete_auto_created_batches()
		self.delete_linked_stock_entry()

		if self.purpose == 'Material Transfer' and self.add_to_transit:
			self.set_material_request_transfer_status('Not Started')
		if self.purpose == 'Material Transfer' and self.outgoing_stock_entry:
			self.set_material_request_transfer_status('In Transit')

	def set_job_card_data(self):
		if self.job_card and not self.work_order:
			data = frappe.db.get_value('Job Card',
				self.job_card, ['for_quantity', 'work_order', 'bom_no'], as_dict=1)
			self.fg_completed_qty = data.for_quantity
			self.work_order = data.work_order
			self.from_bom = 1
			self.bom_no = data.bom_no

	def validate_work_order_status(self):
		wo_doc = frappe.get_doc("Work Order", self.work_order)
		if wo_doc.status == 'Completed':
			frappe.throw(_("Cannot cancel transaction for Completed Work Order."))

	def validate_purpose(self):
		valid_purposes = ["Material Issue", "Material Receipt", "Material Transfer",
			"Material Transfer for Manufacture", "Manufacture", "Repack", "Send to Subcontractor",
			"Material Consumption for Manufacture"]

		if self.purpose not in valid_purposes:
			frappe.throw(_("Purpose must be one of {0}").format(comma_or(valid_purposes)))

		if self.job_card and self.purpose != 'Material Transfer for Manufacture':
			frappe.throw(_("For job card {0}, you can only make the 'Material Transfer for Manufacture' type stock entry")
				.format(self.job_card))

	def delete_linked_stock_entry(self):
		if self.purpose == "Send to Warehouse":
			for d in frappe.get_all("Stock Entry", filters={"docstatus": 0,
				"outgoing_stock_entry": self.name, "purpose": "Receive at Warehouse"}):
				frappe.delete_doc("Stock Entry", d.name)

	def set_transfer_qty(self):
		for item in self.get("items"):
			if not flt(item.qty):
				frappe.throw(_("Row {0}: Qty is mandatory").format(item.idx))
			if not flt(item.conversion_factor):
				frappe.throw(_("Row {0}: UOM Conversion Factor is mandatory").format(item.idx))
			item.transfer_qty = flt(flt(item.qty) * flt(item.conversion_factor),
				self.precision("transfer_qty", item))

	def update_cost_in_project(self):
		if (self.work_order and not frappe.db.get_value("Work Order",
			self.work_order, "update_consumed_material_cost_in_project")):
			return

		if self.project:
			amount = frappe.db.sql(""" select ifnull(sum(sed.amount), 0)
				from
					`tabStock Entry` se, `tabStock Entry Detail` sed
				where
					se.docstatus = 1 and se.project = %s and sed.parent = se.name
					and (sed.t_warehouse is null or sed.t_warehouse = '')""", self.project, as_list=1)

			amount = amount[0][0] if amount else 0
			additional_costs = frappe.db.sql(""" select ifnull(sum(sed.base_amount), 0)
				from
					`tabStock Entry` se, `tabLanded Cost Taxes and Charges` sed
				where
					se.docstatus = 1 and se.project = %s and sed.parent = se.name
					and se.purpose = 'Manufacture'""", self.project, as_list=1)

			additional_cost_amt = additional_costs[0][0] if additional_costs else 0

			amount += additional_cost_amt
			frappe.db.set_value('Project', self.project, 'total_consumed_material_cost', amount)

	def validate_item(self):
		stock_items = self.get_stock_items()
		serialized_items = self.get_serialized_items()
		for item in self.get("items"):
			if not self.is_return and flt(item.qty) and flt(item.qty) < 0:
				frappe.throw(_("Row {0}: The item {1}, quantity must be positive number")
					.format(item.idx, frappe.bold(item.item_code)))

			if item.item_code not in stock_items:
				frappe.throw(_("{0} is not a stock Item").format(item.item_code))

			item_details = self.get_item_details(frappe._dict(
				{"item_code": item.item_code, "company": self.company,
				"project": self.project, "uom": item.uom, 's_warehouse': item.s_warehouse}),
				for_update=True)

			for f in ("uom", "stock_uom", "description", "item_name", "expense_account",
				"cost_center", "conversion_factor"):
					if f == "stock_uom" or not item.get(f):
						item.set(f, item_details.get(f))
					if f == 'conversion_factor' and item.uom == item_details.get('stock_uom'):
						item.set(f, item_details.get(f))

			if not item.transfer_qty and item.qty:
				item.transfer_qty = flt(flt(item.qty) * flt(item.conversion_factor),
				self.precision("transfer_qty", item))

			if (self.purpose in ("Material Transfer", "Material Transfer for Manufacture")
				and not item.serial_no
				and item.item_code in serialized_items):
				frappe.throw(_("Row #{0}: Please specify Serial No for Item {1}").format(item.idx, item.item_code),
					frappe.MandatoryError)

	def validate_qty(self):
		manufacture_purpose = ["Manufacture", "Material Consumption for Manufacture"]

		if self.purpose in manufacture_purpose and self.work_order:
			if not frappe.get_value('Work Order', self.work_order, 'skip_transfer'):
				item_code = []
				for item in self.items:
					if cstr(item.t_warehouse) == '':
						req_items = frappe.get_all('Work Order Item',
										filters={'parent': self.work_order, 'item_code': item.item_code}, fields=["item_code"])

						transferred_materials = frappe.db.sql("""
									select
										sum(qty) as qty
									from `tabStock Entry` se,`tabStock Entry Detail` sed
									where
										se.name = sed.parent and se.docstatus=1 and
										(se.purpose='Material Transfer for Manufacture' or se.purpose='Manufacture')
										and sed.item_code=%s and se.work_order= %s and ifnull(sed.t_warehouse, '') != ''
								""", (item.item_code, self.work_order), as_dict=1)

						stock_qty = flt(item.qty)
						trans_qty = flt(transferred_materials[0].qty)
						if req_items:
							if stock_qty > trans_qty:
								item_code.append(item.item_code)

	def validate_fg_completed_qty(self):
		item_wise_qty = {}
		if self.purpose == "Manufacture" and self.work_order:
			for d in self.items:
				if d.is_finished_item:
					item_wise_qty.setdefault(d.item_code, []).append(d.qty)

		for item_code, qty_list in iteritems(item_wise_qty):
			if self.fg_completed_qty != sum(qty_list):
				frappe.throw(_("The finished product {0} quantity {1} and For Quantity {2} cannot be different")
					.format(frappe.bold(item_code), frappe.bold(sum(qty_list)), frappe.bold(self.fg_completed_qty)))

	def validate_difference_account(self):
		if not cint(erpnext.is_perpetual_inventory_enabled(self.company)):
			return

		for d in self.get("items"):
			if not d.expense_account:
				frappe.throw(_("Please enter <b>Difference Account</b> or set default <b>Stock Adjustment Account</b> for company {0}")
					.format(frappe.bold(self.company)))

			elif self.is_opening == "Yes" and frappe.db.get_value("Account", d.expense_account, "report_type") == "Profit and Loss":
				frappe.throw(_("Difference Account must be a Asset/Liability type account, since this Stock Entry is an Opening Entry"), OpeningEntryAccountError)

	def validate_warehouse(self):
		"""perform various (sometimes conditional) validations on warehouse"""

		source_mandatory = ["Material Issue", "Material Transfer", "Send to Subcontractor", "Material Transfer for Manufacture",
			"Material Consumption for Manufacture"]

		target_mandatory = ["Material Receipt", "Material Transfer", "Send to Subcontractor",
			"Material Transfer for Manufacture"]

		validate_for_manufacture = any([d.bom_no for d in self.get("items")])

		if self.purpose in source_mandatory and self.purpose not in target_mandatory:
			self.to_warehouse = None
			for d in self.get('items'):
				d.t_warehouse = None
		elif self.purpose in target_mandatory and self.purpose not in source_mandatory:
			self.from_warehouse = None
			for d in self.get('items'):
				d.s_warehouse = None

		for d in self.get('items'):
			if not d.s_warehouse and not d.t_warehouse:
				d.s_warehouse = self.from_warehouse
				d.t_warehouse = self.to_warehouse

			if not (d.s_warehouse or d.t_warehouse):
				frappe.throw(_("Atleast one warehouse is mandatory"))

			if self.purpose in source_mandatory and not d.s_warehouse:
				if self.from_warehouse:
					d.s_warehouse = self.from_warehouse
				else:
					frappe.throw(_("Source warehouse is mandatory for row {0}").format(d.idx))

			if self.purpose in target_mandatory and not d.t_warehouse:
				if self.to_warehouse:
					d.t_warehouse = self.to_warehouse
				else:
					frappe.throw(_("Target warehouse is mandatory for row {0}").format(d.idx))

			if self.purpose == "Manufacture":
				if validate_for_manufacture:
					if d.is_finished_item or d.is_scrap_item:
						d.s_warehouse = None
						if not d.t_warehouse:
							frappe.throw(_("Target warehouse is mandatory for row {0}").format(d.idx))
					else:
						d.t_warehouse = None
						if not d.s_warehouse:
							frappe.throw(_("Source warehouse is mandatory for row {0}").format(d.idx))

			if cstr(d.s_warehouse) == cstr(d.t_warehouse) and not self.purpose == "Material Transfer for Manufacture":
				frappe.throw(_("Source and target warehouse cannot be same for row {0}").format(d.idx))

	def validate_work_order(self):
		if self.purpose in ("Manufacture", "Material Transfer for Manufacture", "Material Consumption for Manufacture"):
			# check if work order is entered

			if (self.purpose=="Manufacture" or self.purpose=="Material Consumption for Manufacture") \
					and self.work_order:
				if not self.fg_completed_qty:
					frappe.throw(_("For Quantity (Manufactured Qty) is mandatory"))
				self.check_if_operations_completed()
				self.check_duplicate_entry_for_work_order()
		elif self.purpose != "Material Transfer":
			self.work_order = None

	def check_if_operations_completed(self):
		"""Check if Time Sheets are completed against before manufacturing to capture operating costs."""
		if not self.work_order: return

		work_order_doc = frappe.get_cached_doc("Work Order", self.work_order)
		allowance_percentage = flt(frappe.db.get_single_value("Manufacturing Settings",
			"overproduction_percentage_for_work_order"))

		for d in work_order_doc.get("operations"):
			total_completed_qty = flt(self.fg_completed_qty) + flt(work_order_doc.produced_qty)
			completed_qty = d.completed_qty + (allowance_percentage/100 * d.completed_qty)
			if total_completed_qty > flt(completed_qty):
				job_card = frappe.db.get_value('Job Card', {'operation_id': d.name}, 'name')
				if not job_card:
					frappe.throw(_("Work Order {0}: Job Card not found for the operation {1}")
						.format(self.work_order, d.operation))

				work_order_link = frappe.utils.get_link_to_form('Work Order', self.work_order)
				job_card_link = frappe.utils.get_link_to_form('Job Card', job_card)
				frappe.throw(_("Row #{0}: Operation {1} is not completed for {2} qty of finished goods in Work Order {3}. Please update operation status via Job Card {4}.")
					.format(d.idx, frappe.bold(d.operation), frappe.bold(total_completed_qty), work_order_link, job_card_link), OperationsNotCompleteError)

	def check_duplicate_entry_for_work_order(self):
		other_ste = [t[0] for t in frappe.db.get_values("Stock Entry",  {
			"work_order": self.work_order,
			"purpose": self.purpose,
			"docstatus": ["!=", 2],
			"name": ["!=", self.name]
		}, "name")]

		if other_ste:
			production_item, qty = frappe.db.get_value("Work Order",
				self.work_order, ["production_item", "qty"])
			args = other_ste + [production_item]
			fg_qty_already_entered = frappe.db.sql("""select sum(transfer_qty)
				from `tabStock Entry Detail`
				where parent in (%s)
					and item_code = %s
					and ifnull(s_warehouse,'')='' """ % (", ".join(["%s" * len(other_ste)]), "%s"), args)[0][0]
			if fg_qty_already_entered and fg_qty_already_entered >= qty:
				frappe.throw(_("Stock Entries already created for Work Order ")
					+ self.work_order + ":" + ", ".join(other_ste), DuplicateEntryForWorkOrderError)

	def set_actual_qty(self):
		allow_negative_stock = cint(frappe.db.get_value("Stock Settings", None, "allow_negative_stock"))

		for d in self.get('items'):
			previous_sle = get_previous_sle({
				"item_code": d.item_code,
				"warehouse": d.s_warehouse or d.t_warehouse,
				"posting_date": self.posting_date,
				"posting_time": self.posting_time
			})

			# get actual stock at source warehouse
			d.actual_qty = previous_sle.get("qty_after_transaction") or 0

			# validate qty during submit
			if d.docstatus==1 and d.s_warehouse and not allow_negative_stock and flt(d.actual_qty, d.precision("actual_qty")) < flt(d.transfer_qty, d.precision("actual_qty")):
				frappe.throw(_("Row {0}: Quantity not available for {4} in warehouse {1} at posting time of the entry ({2} {3})").format(d.idx,
					frappe.bold(d.s_warehouse), formatdate(self.posting_date),
					format_time(self.posting_time), frappe.bold(d.item_code))
					+ '<br><br>' + _("Available quantity is {0}, you need {1}").format(frappe.bold(d.actual_qty),
						frappe.bold(d.transfer_qty)),
					NegativeStockError, title=_('Insufficient Stock'))

	def set_serial_nos(self, work_order):
		previous_se = frappe.db.get_value("Stock Entry", {"work_order": work_order,
				"purpose": "Material Transfer for Manufacture"}, "name")

		for d in self.get('items'):
			transferred_serial_no = frappe.db.get_value("Stock Entry Detail",{"parent": previous_se,
				"item_code": d.item_code}, "serial_no")

			if transferred_serial_no:
				d.serial_no = transferred_serial_no

	def get_stock_and_rate(self):
		"""
			Updates rate and availability of all the items.
			Called from Update Rate and Availability button.
		"""
		self.set_work_order_details()
		self.set_transfer_qty()
		self.set_actual_qty()
		self.calculate_rate_and_amount()

	def calculate_rate_and_amount(self, reset_outgoing_rate=True, raise_error_if_no_rate=True):
		self.set_basic_rate(reset_outgoing_rate, raise_error_if_no_rate)
		init_landed_taxes_and_totals(self)
		self.distribute_additional_costs()
		self.update_valuation_rate()
		self.set_total_incoming_outgoing_value()
		self.set_total_amount()

	def set_basic_rate(self, reset_outgoing_rate=True, raise_error_if_no_rate=True):
		"""
			Set rate for outgoing, scrapped and finished items
		"""
		# Set rate for outgoing items
		outgoing_items_cost = self.set_rate_for_outgoing_items(reset_outgoing_rate)
		finished_item_qty = sum([flt(d.transfer_qty) for d in self.items if d.is_finished_item])

		# Set basic rate for incoming items
		for d in self.get('items'):
			if d.s_warehouse or d.set_basic_rate_manually: continue

			if d.allow_zero_valuation_rate:
				d.basic_rate = 0.0
			elif d.is_finished_item:
				if self.purpose == "Manufacture":
					d.basic_rate = self.get_basic_rate_for_manufactured_item(finished_item_qty, outgoing_items_cost)
				elif self.purpose == "Repack":
					d.basic_rate = self.get_basic_rate_for_repacked_items(d.transfer_qty, outgoing_items_cost)

			if not d.basic_rate and not d.allow_zero_valuation_rate:
				d.basic_rate = get_valuation_rate(d.item_code, d.t_warehouse,
					self.doctype, self.name, d.allow_zero_valuation_rate,
					currency=erpnext.get_company_currency(self.company), company=self.company,
					raise_error_if_no_rate=raise_error_if_no_rate)

			d.basic_rate = flt(d.basic_rate, d.precision("basic_rate"))
			d.basic_amount = flt(flt(d.transfer_qty) * flt(d.basic_rate), d.precision("basic_amount"))

	def set_rate_for_outgoing_items(self, reset_outgoing_rate=True):
		outgoing_items_cost = 0.0
		for d in self.get('items'):
			if d.s_warehouse:
				if reset_outgoing_rate:
					args = self.get_args_for_incoming_rate(d)
					rate = get_incoming_rate(args)
					if rate > 0:
						d.basic_rate = rate

				d.basic_amount = flt(flt(d.transfer_qty) * flt(d.basic_rate), d.precision("basic_amount"))
				if not d.t_warehouse:
					outgoing_items_cost += flt(d.basic_amount)
		return outgoing_items_cost

	def get_args_for_incoming_rate(self, item):
		return frappe._dict({
			"item_code": item.item_code,
			"warehouse": item.s_warehouse or item.t_warehouse,
			"posting_date": self.posting_date,
			"posting_time": self.posting_time,
			"qty": item.s_warehouse and -1*flt(item.transfer_qty) or flt(item.transfer_qty),
			"serial_no": item.serial_no,
			"voucher_type": self.doctype,
			"voucher_no": self.name,
			"company": self.company,
			"allow_zero_valuation": item.allow_zero_valuation_rate,
		})

	def get_basic_rate_for_repacked_items(self, finished_item_qty, outgoing_items_cost):
		finished_items = [d.item_code for d in self.get("items") if d.is_finished_item]
		if len(finished_items) == 1:
			return flt(outgoing_items_cost / finished_item_qty)
		else:
			unique_finished_items = set(finished_items)
			if len(unique_finished_items) == 1:
				total_fg_qty = sum([flt(d.transfer_qty) for d in self.items if d.is_finished_item])
				return flt(outgoing_items_cost / total_fg_qty)

	def get_basic_rate_for_manufactured_item(self, finished_item_qty, outgoing_items_cost=0):
		scrap_items_cost = sum([flt(d.basic_amount) for d in self.get("items") if d.is_scrap_item])

		# Get raw materials cost from BOM if multiple material consumption entries
		if frappe.db.get_single_value("Manufacturing Settings", "material_consumption"):
			self.set_work_order_details()
			wo_items = self.wo_doc.required_items
			outgoing_items_cost = sum([flt(row.required_qty)*flt(row.rate) for row in wo_items])

		return flt(flt(outgoing_items_cost - scrap_items_cost) / flt(finished_item_qty))

	def distribute_additional_costs(self):
		# If no incoming items, set additional costs blank
		if not any([d.item_code for d in self.items if d.t_warehouse]):
			self.additional_costs = []

		self.total_additional_costs = sum([flt(t.base_amount) for t in self.get("additional_costs")])

		if self.purpose in ("Repack", "Manufacture"):
			incoming_items_cost = sum([flt(t.basic_amount) for t in self.get("items") if t.is_finished_item])
		else:
			incoming_items_cost = sum([flt(t.basic_amount) for t in self.get("items") if t.t_warehouse])

		if incoming_items_cost:
			for d in self.get("items"):
				if (self.purpose in ("Repack", "Manufacture") and d.is_finished_item) or d.t_warehouse:
					d.additional_cost = (flt(d.basic_amount) / incoming_items_cost) * self.total_additional_costs
				else:
					d.additional_cost = 0

	def update_valuation_rate(self):
		for d in self.get("items"):
			if d.transfer_qty:
				d.amount = flt(flt(d.basic_amount) + flt(d.additional_cost), d.precision("amount"))
				d.valuation_rate = flt(flt(d.basic_rate) + (flt(d.additional_cost) / flt(d.transfer_qty)),
					d.precision("valuation_rate"))

	def set_total_incoming_outgoing_value(self):
		self.total_incoming_value = self.total_outgoing_value = 0.0
		for d in self.get("items"):
			if d.t_warehouse:
				self.total_incoming_value += flt(d.amount)
			if d.s_warehouse:
				self.total_outgoing_value += flt(d.amount)

		self.value_difference = self.total_incoming_value - self.total_outgoing_value

	def set_total_amount(self):
		self.total_amount = None
		if self.purpose not in ['Manufacture', 'Repack']:
			self.total_amount = sum([flt(item.amount) for item in self.get("items")])

	def set_stock_entry_type(self):
		if self.purpose:
			self.stock_entry_type = frappe.get_cached_value('Stock Entry Type',
				{'purpose': self.purpose}, 'name')

	def set_purpose_for_stock_entry(self):
		if self.stock_entry_type and not self.purpose:
			self.purpose = frappe.get_cached_value('Stock Entry Type',
				self.stock_entry_type, 'purpose')

	def validate_purchase_order(self):
		"""Throw exception if more raw material is transferred against Purchase Order than in
		the raw materials supplied table"""
		backflush_raw_materials_based_on = frappe.db.get_single_value("Buying Settings",
			"backflush_raw_materials_of_subcontract_based_on")

		qty_allowance = flt(frappe.db.get_single_value("Buying Settings",
			"over_transfer_allowance"))

		if not (self.purpose == "Send to Subcontractor" and self.purchase_order): return

		if (backflush_raw_materials_based_on == 'BOM'):
			purchase_order = frappe.get_doc("Purchase Order", self.purchase_order)
			for se_item in self.items:
				item_code = se_item.original_item or se_item.item_code
				precision = cint(frappe.db.get_default("float_precision")) or 3
				required_qty = sum([flt(d.required_qty) for d in purchase_order.supplied_items \
					if d.rm_item_code == item_code])

				total_allowed = required_qty + (required_qty * (qty_allowance/100))

				if not required_qty:
					bom_no = frappe.db.get_value("Purchase Order Item",
						{"parent": self.purchase_order, "item_code": se_item.subcontracted_item},
						"bom")

					if se_item.allow_alternative_item:
						original_item_code = frappe.get_value("Item Alternative", {"alternative_item_code": item_code}, "item_code")

						required_qty = sum([flt(d.required_qty) for d in purchase_order.supplied_items \
							if d.rm_item_code == original_item_code])

						total_allowed = required_qty + (required_qty * (qty_allowance/100))

				if not required_qty:
					frappe.throw(_("Item {0} not found in 'Raw Materials Supplied' table in Purchase Order {1}")
						.format(se_item.item_code, self.purchase_order))
				total_supplied = frappe.db.sql("""select sum(transfer_qty)
					from `tabStock Entry Detail`, `tabStock Entry`
					where `tabStock Entry`.purchase_order = %s
						and `tabStock Entry`.docstatus = 1
						and `tabStock Entry Detail`.item_code = %s
						and `tabStock Entry Detail`.parent = `tabStock Entry`.name""",
							(self.purchase_order, se_item.item_code))[0][0]

				if flt(total_supplied, precision) > flt(total_allowed, precision):
					frappe.throw(_("Row {0}# Item {1} cannot be transferred more than {2} against Purchase Order {3}")
						.format(se_item.idx, se_item.item_code, total_allowed, self.purchase_order))
		elif backflush_raw_materials_based_on == "Material Transferred for Subcontract":
			for row in self.items:
				if not row.subcontracted_item:
					frappe.throw(_("Row {0}: Subcontracted Item is mandatory for the raw material {1}")
						.format(row.idx, frappe.bold(row.item_code)))
				elif not row.po_detail:
					filters = {
						"parent": self.purchase_order, "docstatus": 1,
						"rm_item_code": row.item_code, "main_item_code": row.subcontracted_item
					}

					po_detail = frappe.db.get_value("Purchase Order Item Supplied", filters, "name")
					if po_detail:
						row.db_set("po_detail", po_detail)

	def validate_bom(self):
		for d in self.get('items'):
			if d.bom_no and (d.t_warehouse != getattr(self, "wo_doc", frappe._dict()).scrap_warehouse):
				item_code = d.original_item or d.item_code
				validate_bom_no(item_code, d.bom_no)

	def mark_finished_and_scrap_items(self):
		if self.purpose in ("Repack", "Manufacture"):
			if any([d.item_code for d in self.items if (d.is_finished_item and d.t_warehouse)]):
				return

			finished_item = self.get_finished_item()

			for d in self.items:
				if d.t_warehouse and not d.s_warehouse:
					if self.purpose=="Repack" or d.item_code == finished_item:
						d.is_finished_item = 1
					else:
						d.is_scrap_item = 1
				else:
					d.is_finished_item = 0
					d.is_scrap_item = 0

	def get_finished_item(self):
		finished_item = None
		if self.work_order:
			finished_item = frappe.db.get_value("Work Order", self.work_order, "production_item")
		elif self.bom_no:
			finished_item = frappe.db.get_value("BOM", self.bom_no, "item")

		return finished_item

	def validate_finished_goods(self):
		"""validation: finished good quantity should be same as manufacturing quantity"""
		if not self.work_order: return

		production_item, wo_qty = frappe.db.get_value("Work Order",
			self.work_order, ["production_item", "qty"])

		finished_items = []
		for d in self.get('items'):
			if d.is_finished_item:
				if d.item_code != production_item:
					frappe.throw(_("Finished Item {0} does not match with Work Order {1}")
						.format(d.item_code, self.work_order))
				elif flt(d.transfer_qty) > flt(self.fg_completed_qty):
					frappe.throw(_("Quantity in row {0} ({1}) must be same as manufactured quantity {2}"). \
						format(d.idx, d.transfer_qty, self.fg_completed_qty))
				finished_items.append(d.item_code)

		if len(set(finished_items)) > 1:
			frappe.throw(_("Multiple items cannot be marked as finished item"))

		if self.purpose == "Manufacture":
			allowance_percentage = flt(frappe.db.get_single_value("Manufacturing Settings",
				"overproduction_percentage_for_work_order"))

			allowed_qty = wo_qty + (allowance_percentage/100 * wo_qty)
			if self.fg_completed_qty > allowed_qty:
				frappe.throw(_("For quantity {0} should not be greater than work order quantity {1}")
					.format(flt(self.fg_completed_qty), wo_qty))

	def update_stock_ledger(self):
		sl_entries = []
		finished_item_row = self.get_finished_item_row()

		# make sl entries for source warehouse first
		self.get_sle_for_source_warehouse(sl_entries, finished_item_row)

		# SLE for target warehouse
		self.get_sle_for_target_warehouse(sl_entries, finished_item_row)

		# reverse sl entries if cancel
		if self.docstatus == 2:
			sl_entries.reverse()

		self.make_sl_entries(sl_entries)

	def get_finished_item_row(self):
		finished_item_row = None
		if self.purpose in ("Manufacture", "Repack"):
			for d in self.get('items'):
				if d.is_finished_item:
					finished_item_row = d

		return finished_item_row

	def get_sle_for_source_warehouse(self, sl_entries, finished_item_row):
		for d in self.get('items'):
			if cstr(d.s_warehouse):
				sle = self.get_sl_entries(d, {
					"warehouse": cstr(d.s_warehouse),
					"actual_qty": -flt(d.transfer_qty),
					"incoming_rate": 0
				})
				if cstr(d.t_warehouse):
					sle.dependant_sle_voucher_detail_no = d.name
				elif finished_item_row and (finished_item_row.item_code != d.item_code or finished_item_row.t_warehouse != d.s_warehouse):
					sle.dependant_sle_voucher_detail_no = finished_item_row.name

				sl_entries.append(sle)

	def get_sle_for_target_warehouse(self, sl_entries, finished_item_row):
		for d in self.get('items'):
			if cstr(d.t_warehouse):
				sle = self.get_sl_entries(d, {
					"warehouse": cstr(d.t_warehouse),
					"actual_qty": flt(d.transfer_qty),
					"incoming_rate": flt(d.valuation_rate)
				})
				if cstr(d.s_warehouse) or (finished_item_row and d.name == finished_item_row.name):
					sle.recalculate_rate = 1

				sl_entries.append(sle)

	def get_gl_entries(self, warehouse_account):
		gl_entries = super(StockEntry, self).get_gl_entries(warehouse_account)

		total_basic_amount = sum([flt(t.basic_amount) for t in self.get("items") if t.t_warehouse])
		divide_based_on = total_basic_amount

		if self.get("additional_costs") and not total_basic_amount:
			# if total_basic_amount is 0, distribute additional charges based on qty
			divide_based_on = sum(item.qty for item in list(self.get("items")))

		item_account_wise_additional_cost = {}

		for t in self.get("additional_costs"):
			for d in self.get("items"):
				if d.t_warehouse:
					item_account_wise_additional_cost.setdefault((d.item_code, d.name), {})
					item_account_wise_additional_cost[(d.item_code, d.name)].setdefault(t.expense_account, {
						"amount": 0.0,
						"base_amount": 0.0
					})

					multiply_based_on = d.basic_amount if total_basic_amount else d.qty

					item_account_wise_additional_cost[(d.item_code, d.name)][t.expense_account]["amount"] += \
						flt(t.amount * multiply_based_on) / divide_based_on

					item_account_wise_additional_cost[(d.item_code, d.name)][t.expense_account]["base_amount"] += \
						flt(t.base_amount * multiply_based_on) / divide_based_on

		if item_account_wise_additional_cost:
			for d in self.get("items"):
				for account, amount in iteritems(item_account_wise_additional_cost.get((d.item_code, d.name), {})):
					if not amount: continue

					gl_entries.append(self.get_gl_dict({
						"account": account,
						"against": d.expense_account,
						"cost_center": d.cost_center,
						"remarks": self.get("remarks") or _("Accounting Entry for Stock"),
						"credit_in_account_currency": flt(amount["amount"]),
						"credit": flt(amount["base_amount"])
					}, item=d))

					gl_entries.append(self.get_gl_dict({
						"account": d.expense_account,
						"against": account,
						"cost_center": d.cost_center,
						"remarks": self.get("remarks") or _("Accounting Entry for Stock"),
						"credit": -1 * amount['base_amount'] # put it as negative credit instead of debit purposefully
					}, item=d))

		return process_gl_map(gl_entries)

	def update_work_order(self):
		def _validate_work_order(wo_doc):
			if flt(wo_doc.docstatus) != 1:
				frappe.throw(_("Work Order {0} must be submitted").format(self.work_order))

			if wo_doc.status == 'Stopped':
				frappe.throw(_("Transaction not allowed against stopped Work Order {0}").format(self.work_order))

		if self.job_card:
			job_doc = frappe.get_doc('Job Card', self.job_card)
			job_doc.set_transferred_qty(update_status=True)

		if self.work_order:
			wo_doc = frappe.get_doc("Work Order", self.work_order)
			_validate_work_order(wo_doc)
			wo_doc.update_status()
			wo_doc.update_required_items(se_doc=self)

			if self.fg_completed_qty:
				wo_doc.run_method("update_work_order_qty")
				if self.purpose == "Manufacture":
					wo_doc.run_method("update_planned_qty")

			if not wo_doc.operations:
				wo_doc.set_actual_dates()

	def get_item_details(self, args=None, for_update=False):
		item = frappe.db.sql("""select i.name, i.stock_uom, i.description, i.image, i.item_name, i.item_group,
				i.has_batch_no, i.sample_quantity, i.has_serial_no, i.allow_alternative_item,
				id.expense_account, id.buying_cost_center
			from `tabItem` i LEFT JOIN `tabItem Default` id ON i.name=id.parent and id.company=%s
			where i.name=%s
				and i.disabled=0
				and (i.end_of_life is null or i.end_of_life='0000-00-00' or i.end_of_life > %s)""",
			(self.company, args.get('item_code'), nowdate()), as_dict = 1)

		if not item:
			frappe.throw(_("Item {0} is not active or end of life has been reached").format(args.get("item_code")))

		item = item[0]
		item_group_defaults = get_item_group_defaults(item.name, self.company)
		brand_defaults = get_brand_defaults(item.name, self.company)

		ret = frappe._dict({
			'uom'			      	: item.stock_uom,
			'stock_uom'				: item.stock_uom,
			'description'		  	: item.description,
			'image'					: item.image,
			'item_name' 		  	: item.item_name,
			'cost_center'			: get_default_cost_center(args, item, item_group_defaults, brand_defaults, self.company),
			'qty'					: args.get("qty"),
			'transfer_qty'			: args.get('qty'),
			'conversion_factor'		: 1,
			'batch_no'				: '',
			'actual_qty'			: 0,
			'basic_rate'			: 0,
			'serial_no'				: '',
			'has_serial_no'			: item.has_serial_no,
			'has_batch_no'			: item.has_batch_no,
			'sample_quantity'		: item.sample_quantity,
			'expense_account'		: item.expense_account
		})

		if self.purpose == 'Send to Subcontractor':
			ret["allow_alternative_item"] = item.allow_alternative_item

		# update uom
		if args.get("uom") and for_update:
			ret.update(get_uom_details(args.get('item_code'), args.get('uom'), args.get('qty')))

		if self.purpose == 'Material Issue':
			ret["expense_account"] = (item.get("expense_account") or
				item_group_defaults.get("expense_account") or
				frappe.get_cached_value('Company',  self.company,  "default_expense_account"))

		for company_field, field in {'stock_adjustment_account': 'expense_account',
			'cost_center': 'cost_center'}.items():
			if not ret.get(field):
				ret[field] = frappe.get_cached_value('Company',  self.company,  company_field)

		args['posting_date'] = self.posting_date
		args['posting_time'] = self.posting_time

		stock_and_rate = get_warehouse_details(args) if args.get('warehouse') else {}
		ret.update(stock_and_rate)

		# automatically select batch for outgoing item
		if (args.get('s_warehouse', None) and args.get('qty') and
			ret.get('has_batch_no') and not args.get('batch_no')):
			args.batch_no = get_batch_no(args['item_code'], args['s_warehouse'], args['qty'])

		if self.purpose == "Send to Subcontractor" and self.get("purchase_order") and args.get('item_code'):
			subcontract_items = frappe.get_all("Purchase Order Item Supplied",
				{"parent": self.purchase_order, "rm_item_code": args.get('item_code')}, "main_item_code")

			if subcontract_items and len(subcontract_items) == 1:
				ret["subcontracted_item"] = subcontract_items[0].main_item_code

		return ret

	def set_items_for_stock_in(self):
		self.items = []

		if self.outgoing_stock_entry and self.purpose == 'Material Transfer':
			doc = frappe.get_doc('Stock Entry', self.outgoing_stock_entry)

			if doc.per_transferred == 100:
				frappe.throw(_("Goods are already received against the outward entry {0}")
					.format(doc.name))

			for d in doc.items:
				self.append('items', {
					's_warehouse': d.t_warehouse,
					'item_code': d.item_code,
					'qty': d.qty,
					'uom': d.uom,
					'against_stock_entry': d.parent,
					'ste_detail': d.name,
					'stock_uom': d.stock_uom,
					'conversion_factor': d.conversion_factor,
					'serial_no': d.serial_no,
					'batch_no': d.batch_no
				})

	def get_items(self):
		if self.from_bom and not self.is_return and not (self.bom_no and self.fg_completed_qty):
			frappe.throw(_("The fields {0} and {1} are required to fetch the items").
				format(frappe.bold(_("BOM No")), frappe.bold(_("For Quantity"))))

		self.set_work_order_details()

		if self.work_order and self.purpose in ["Manufacture",
			"Material Consumption for Manufacture", "Repack", "Material Transfer for Manufacture"]:
			self.set_items_from_work_order()

		elif self.bom_no:
			self.set_items_from_bom()

		elif self.purchase_order and self.purpose == "Send to Subcontractor":
			self.set_items_from_purchase_order()

		if self.purpose in ["Manufacture", "Repack"]:
			self.check_if_operations_completed()
			self.set_fg_materials()
			self.set_scrap_materials()

			if self.get("wo_doc"):
				add_additional_cost(self, self.wo_doc)

		self.calculate_rate_and_amount(raise_error_if_no_rate=False)

	def set_items_from_work_order(self):
		remain_qty_to_produce = self.get_remain_qty_to_produce()

		for row in self.wo_doc.required_items:
			if (self.purpose == "Material Transfer for Manufacture"
				and (self.wo_doc.skip_transfer or row.skip_material_transfer)): continue

			if (row.transferred_qty and row.transferred_qty >= row.required_qty
				and row.consumed_qty >= row.transferred_qty):
				continue

			transferred_qty = row.required_qty
			if row.transferred_qty > row.required_qty:
				transferred_qty = row.transferred_qty

			qty = ((transferred_qty - row.consumed_qty)
				* flt(self.fg_completed_qty)) / remain_qty_to_produce

			if self.is_return and not qty:
				qty = transferred_qty - row.consumed_qty

			args = self.prepare_raw_materials(row, qty)

			self.add_items(args)

	def get_remain_qty_to_produce(self):
		remain_qty_to_produce = (
			max(flt(self.wo_doc.material_transferred_for_manufacturing), self.wo_doc.qty) - self.wo_doc.produced_qty
		)

		# if work order has completed but still there is some stock pending
		if not remain_qty_to_produce and self.is_return:
			remain_qty_to_produce = 1

		return remain_qty_to_produce

	def set_items_from_bom(self):
		from erpnext.manufacturing.doctype.bom.bom import get_bom_items_as_dict

		item_dict = get_bom_items_as_dict(self.bom_no, self.company, qty=self.fg_completed_qty,
			fetch_exploded = self.use_multi_level_bom, fetch_qty_in_stock_uom=False)

		for key in item_dict:
			row = item_dict.get(key)
			args = self.prepare_raw_materials(row)
			self.add_items(args)

	def set_items_from_purchase_order(self):
		self.to_warehouse = frappe.get_cached_value("Purchase Order",
			self.purchase_order, "supplier_warehouse")

		fields = ["rm_item_code as item_code", "main_item_code as subcontracted_item",
			"reserve_warehouse as source_warehouse", "name as po_detail", "required_qty as qty", "stock_uom", "conversion_factor"]

		item_wh = frappe.get_all("Purchase Order Item Supplied", fields = fields,
			filters = {"docstatus": 1, "parent": self.purchase_order})

		for row in item_wh:
			args = self.prepare_raw_materials(row)
			self.add_items(args)

	def set_fg_materials(self):
		doc = self.wo_doc if self.get("wo_doc") else frappe.get_cached_doc("BOM", self.bom_no)

		# UOM fieldname is different in work order and bom
		uom = doc.get("uom") or doc.get("stock_uom")
		args = {
			"uom": uom, "stock_uom": uom, "is_finished_item": 1,
			"qty": self.fg_completed_qty, "transfer_qty": self.fg_completed_qty
		}

		for field in ["description", "item_name", "stock_uom", "uom"]:
			if doc.get(field):
				args[field] = doc.get(field)

		for item_code_field in ["production_item", "item"]:
			if doc.get(item_code_field):
				args["item_code"] = doc.get(item_code_field)

		if self.get("wo_doc"):
			args["t_warehouse"] = self.wo_doc.fg_warehouse

		self.add_items(args)

	def set_scrap_materials(self):
		bom_doc = frappe.get_cached_doc("BOM", self.bom_no)

		tot_qty = bom_doc.get("quantity")
		if self.wo_doc and self.wo_doc.get("scrap_items"):
			bom_doc = self.wo_doc
			tot_qty = bom_doc.get("qty")

		print(tot_qty)
		for row in (bom_doc.get("scrap_items") or []):
			qty = (flt(self.fg_completed_qty) * flt(row.stock_qty)) / flt(tot_qty)

			args = {
				"qty": qty, "transfer_qty": qty, "description": row.description,
				"item_code": row.item_code, "item_name": row.item_name, "is_scrap_item": 1,
				"stock_uom": row.stock_uom, "uom": row.stock_uom, "basic_rate": row.rate
			}

			if self.get("wo_doc"):
				args["t_warehouse"] = self.wo_doc.scrap_warehouse

			self.add_items(args)

	def prepare_raw_materials(self, row, qty=0):
		if not qty and row.qty:
			qty = row.qty

		uom = row.stock_uom or row.uom
		if uom and cint(frappe.get_cached_value('UOM', uom, 'must_be_whole_number')):
			qty = frappe.utils.ceil(qty)

		warehouse = self.from_warehouse or row.source_warehouse or row.get("default_warehouse")
		if self.get("wo_doc") and self.purpose in ["Manufacture", "Material Consumption for Manufacture", "Repack"]:
			warehouse = (row.source_warehouse
				if self.wo_doc.skip_transfer and not self.wo_doc.from_wip_warehouse else self.wo_doc.wip_warehouse)

		if not row.uom:
			row.uom = frappe.get_cached_value("Item", row.item_code, "stock_uom")

		args = frappe._dict({
			"uom": row.uom or row.stock_uom, "basic_rate": row.rate, "work_order_item": row.name,
			"item_code": (row.alternative_item or row.item_code), "item_name": row.item_name,
			"stock_uom": row.uom or row.stock_uom, "description": row.description,
			"qty": qty, "conversion_factor": row.conversion_factor or 1.0,
			"transfer_qty": (flt(qty) * (row.conversion_factor or 1.0)), "cost_center": row.cost_center,
			"s_warehouse": warehouse, "allow_alternative_item": row.allow_alternative_item
		})

		if row.get("po_detail"):
			args["po_detail"] = row.get("po_detail")

		if self.to_warehouse and self.purpose not in ["Manufacture", "Repack"]:
			args["t_warehouse"] = self.to_warehouse

		if self.is_return and self.purpose in ["Material Transfer for Manufacture"]:
			args["t_warehouse"] = row.source_warehouse or row.get("default_warehouse")

		if row.batch_no:
			args = get_unconsumed_batches(args, row)

		if row.serial_no:
			get_unconsumed_serial_nos(args, row)

		return args

	def add_items(self, args):
		if not isinstance(args, list):
			args = [args]

		# To add new row in the stock entry detail
		for row in args:
			if isinstance(row, dict):
				row = frappe._dict(row)

			if not row.cost_center:
				row.cost_center = get_default_cost_center(row, company = self.company)

			self.append("items", row)

	def set_work_order_details(self):
		if not getattr(self, "wo_doc", None):
			self.wo_doc = frappe._dict()

		if self.work_order:
			# common validations
			if not self.wo_doc:
				self.wo_doc = frappe.get_doc('Work Order', self.work_order)

			if self.wo_doc:
				self.bom_no = self.wo_doc.bom_no
			else:
				# invalid work order
				self.work_order = None

	def get_bom_raw_materials(self, qty):
		from erpnext.manufacturing.doctype.bom.bom import get_bom_items_as_dict

		# item dict = { item_code: {qty, description, stock_uom} }
		item_dict = get_bom_items_as_dict(self.bom_no, self.company, qty=qty,
			fetch_exploded = self.use_multi_level_bom, fetch_qty_in_stock_uom=False)

		used_alternative_items = get_used_alternative_items(work_order = self.work_order)
		for item in itervalues(item_dict):
			# if source warehouse presents in BOM set from_warehouse as bom source_warehouse
			if item["allow_alternative_item"]:
				item["allow_alternative_item"] = frappe.db.get_value('Work Order',
					self.work_order, "allow_alternative_item")

			item.from_warehouse = self.from_warehouse or item.source_warehouse or item.default_warehouse
			if item.item_code in used_alternative_items:
				alternative_item_data = used_alternative_items.get(item.item_code)
				item.item_code = alternative_item_data.item_code
				item.item_name = alternative_item_data.item_name
				item.stock_uom = alternative_item_data.stock_uom
				item.uom = alternative_item_data.uom
				item.conversion_factor = alternative_item_data.conversion_factor
				item.description = alternative_item_data.description

		return item_dict

	def validate_with_material_request(self):
		for item in self.get("items"):
			material_request = item.material_request or None
			material_request_item = item.material_request_item or None
			if self.purpose == 'Material Transfer' and self.outgoing_stock_entry:
				parent_se = frappe.get_value("Stock Entry Detail", item.ste_detail, ['material_request','material_request_item'],as_dict=True)
				if parent_se:
					material_request = parent_se.material_request
					material_request_item = parent_se.material_request_item

			if material_request:
				mreq_item = frappe.db.get_value("Material Request Item",
					{"name": material_request_item, "parent": material_request},
					["item_code", "warehouse", "idx"], as_dict=True)
				if mreq_item.item_code != item.item_code:
					frappe.throw(_("Item for row {0} does not match Material Request").format(item.idx),
						frappe.MappingMismatchError)
				elif self.purpose == "Material Transfer" and self.add_to_transit:
					continue

	def validate_batch(self):
		if self.purpose in ["Material Transfer for Manufacture", "Manufacture", "Repack", "Send to Subcontractor"]:
			for item in self.get("items"):
				if item.batch_no:
					disabled = frappe.db.get_value("Batch", item.batch_no, "disabled")
					if disabled == 0:
						expiry_date = frappe.db.get_value("Batch", item.batch_no, "expiry_date")
						if expiry_date:
							if getdate(self.posting_date) > getdate(expiry_date):
								frappe.throw(_("Batch {0} of Item {1} has expired.")
									.format(item.batch_no, item.item_code))
					else:
						frappe.throw(_("Batch {0} of Item {1} is disabled.")
							.format(item.batch_no, item.item_code))

	def update_purchase_order_supplied_items(self):
		#Get PO Supplied Items Details
		item_wh = frappe._dict(frappe.db.sql("""
			select rm_item_code, reserve_warehouse
			from `tabPurchase Order` po, `tabPurchase Order Item Supplied` poitemsup
			where po.name = poitemsup.parent
			and po.name = %s""", self.purchase_order))

		#Update Supplied Qty in PO Supplied Items

		frappe.db.sql("""UPDATE `tabPurchase Order Item Supplied` pos
			SET
				pos.supplied_qty = IFNULL((SELECT ifnull(sum(transfer_qty), 0)
					FROM
						`tabStock Entry Detail` sed, `tabStock Entry` se
					WHERE
						pos.name = sed.po_detail AND pos.rm_item_code = sed.item_code
						AND pos.parent = se.purchase_order AND sed.docstatus = 1
						AND se.name = sed.parent and se.purchase_order = %(po)s
				), 0)
			WHERE pos.docstatus = 1 and pos.parent = %(po)s""", {"po": self.purchase_order})

		#Update reserved sub contracted quantity in bin based on Supplied Item Details and
		for d in self.get("items"):
			item_code = d.get('original_item') or d.get('item_code')
			reserve_warehouse = item_wh.get(item_code)
			stock_bin = get_bin(item_code, reserve_warehouse)
			stock_bin.update_reserved_qty_for_sub_contracting()

	def update_so_in_serial_number(self):
		so_name, item_code = frappe.db.get_value("Work Order", self.work_order, ["sales_order", "production_item"])
		if so_name and item_code:
			qty_to_reserve = get_reserved_qty_for_so(so_name, item_code)
			if qty_to_reserve:
				reserved_qty = frappe.db.sql("""select count(name) from `tabSerial No` where item_code=%s and
					sales_order=%s""", (item_code, so_name))
				if reserved_qty and reserved_qty[0][0]:
					qty_to_reserve -= reserved_qty[0][0]
				if qty_to_reserve > 0:
					for item in self.items:
						if item.item_code == item_code:
							serial_nos = (item.serial_no).split("\n")
							for serial_no in serial_nos:
								if qty_to_reserve > 0:
									frappe.db.set_value("Serial No", serial_no, "sales_order", so_name)
									qty_to_reserve -=1

	def validate_reserved_serial_no_consumption(self):
		for item in self.items:
			if item.s_warehouse and not item.t_warehouse and item.serial_no:
				for sr in get_serial_nos(item.serial_no):
					sales_order = frappe.db.get_value("Serial No", sr, "sales_order")
					if sales_order:
						msg = (_("(Serial No: {0}) cannot be consumed as it's reserverd to fullfill Sales Order {1}.")
							.format(sr, sales_order))

						frappe.throw(_("Item {0} {1}").format(item.item_code, msg))

	def update_transferred_qty(self):
		if self.purpose == 'Material Transfer' and self.outgoing_stock_entry:
			stock_entries = {}
			stock_entries_child_list = []
			for d in self.items:
				if not (d.against_stock_entry and d.ste_detail):
					continue

				stock_entries_child_list.append(d.ste_detail)
				transferred_qty = frappe.get_all("Stock Entry Detail", fields = ["sum(qty) as qty"],
					filters = { 'against_stock_entry': d.against_stock_entry,
						'ste_detail': d.ste_detail,'docstatus': 1})

				stock_entries[(d.against_stock_entry, d.ste_detail)] = (transferred_qty[0].qty
					if transferred_qty and transferred_qty[0] else 0.0) or 0.0

			if not stock_entries: return None

			cond = ''
			for data, transferred_qty in stock_entries.items():
				cond += """ WHEN (parent = %s and name = %s) THEN %s
					""" %(frappe.db.escape(data[0]), frappe.db.escape(data[1]), transferred_qty)

			if cond and stock_entries_child_list:
				frappe.db.sql(""" UPDATE `tabStock Entry Detail`
					SET
						transferred_qty = CASE {cond} END
					WHERE
						name in ({ste_details}) """.format(cond=cond,
					ste_details = ','.join(['%s'] * len(stock_entries_child_list))),
				tuple(stock_entries_child_list))

			args = {
				'source_dt': 'Stock Entry Detail',
				'target_field': 'transferred_qty',
				'target_ref_field': 'qty',
				'target_dt': 'Stock Entry Detail',
				'join_field': 'ste_detail',
				'target_parent_dt': 'Stock Entry',
				'target_parent_field': 'per_transferred',
				'source_field': 'qty',
				'percent_join_field': 'against_stock_entry'
			}

			self._update_percent_field_in_targets(args, update_modified=True)

	def update_quality_inspection(self):
		if self.inspection_required:
			reference_type = reference_name = ''
			if self.docstatus == 1:
				reference_name = self.name
				reference_type = 'Stock Entry'

			for d in self.items:
				if d.quality_inspection:
					frappe.db.set_value("Quality Inspection", d.quality_inspection, {
						'reference_type': reference_type,
						'reference_name': reference_name
					})
	def set_material_request_transfer_status(self, status):
		material_requests = []
		if self.outgoing_stock_entry:
			parent_se = frappe.get_value("Stock Entry", self.outgoing_stock_entry, 'add_to_transit')

		for item in self.items:
			material_request = item.material_request or None
			if self.purpose == "Material Transfer" and material_request not in material_requests:
				if self.outgoing_stock_entry and parent_se:
					material_request = frappe.get_value("Stock Entry Detail", item.ste_detail, 'material_request')

			if material_request and material_request not in material_requests:
				material_requests.append(material_request)
				frappe.db.set_value('Material Request', material_request, 'transfer_status', status)

@frappe.whitelist()
def move_sample_to_retention_warehouse(company, items):
	if isinstance(items, string_types):
		items = json.loads(items)
	retention_warehouse = frappe.db.get_single_value('Stock Settings', 'sample_retention_warehouse')
	stock_entry = frappe.new_doc("Stock Entry")
	stock_entry.company = company
	stock_entry.purpose = "Material Transfer"
	stock_entry.set_stock_entry_type()
	for item in items:
		if item.get('sample_quantity') and item.get('batch_no'):
			sample_quantity = validate_sample_quantity(item.get('item_code'), item.get('sample_quantity'),
				item.get('transfer_qty') or item.get('qty'), item.get('batch_no'))
			if sample_quantity:
				sample_serial_nos = ''
				if item.get('serial_no'):
					serial_nos = (item.get('serial_no')).split()
					if serial_nos and len(serial_nos) > item.get('sample_quantity'):
						serial_no_list = serial_nos[:-(len(serial_nos)-item.get('sample_quantity'))]
						sample_serial_nos = '\n'.join(serial_no_list)

				stock_entry.append("items", {
					"item_code": item.get('item_code'),
					"s_warehouse": item.get('t_warehouse'),
					"t_warehouse": retention_warehouse,
					"qty": item.get('sample_quantity'),
					"basic_rate": item.get('valuation_rate'),
					'uom': item.get('uom'),
					'stock_uom': item.get('stock_uom'),
					"conversion_factor": 1.0,
					"serial_no": sample_serial_nos,
					'batch_no': item.get('batch_no')
				})
	if stock_entry.get('items'):
		return stock_entry.as_dict()

@frappe.whitelist()
def make_stock_in_entry(source_name, target_doc=None):

	def set_missing_values(source, target):
		target.set_stock_entry_type()

	def update_item(source_doc, target_doc, source_parent):
		target_doc.t_warehouse = ''

		if source_doc.material_request_item and source_doc.material_request :
			add_to_transit = frappe.db.get_value('Stock Entry', source_name, 'add_to_transit')
			if add_to_transit:
				warehouse = frappe.get_value('Material Request Item', source_doc.material_request_item, 'warehouse')
				target_doc.t_warehouse = warehouse

		target_doc.s_warehouse = source_doc.t_warehouse
		target_doc.qty = source_doc.qty - source_doc.transferred_qty

	doclist = get_mapped_doc("Stock Entry", source_name, 	{
		"Stock Entry": {
			"doctype": "Stock Entry",
			"field_map": {
				"name": "outgoing_stock_entry"
			},
			"validation": {
				"docstatus": ["=", 1]
			}
		},
		"Stock Entry Detail": {
			"doctype": "Stock Entry Detail",
			"field_map": {
				"name": "ste_detail",
				"parent": "against_stock_entry",
				"serial_no": "serial_no",
				"batch_no": "batch_no"
			},
			"postprocess": update_item,
			"condition": lambda doc: flt(doc.qty) - flt(doc.transferred_qty) > 0.01
		},
	}, target_doc, set_missing_values)

	return doclist

@frappe.whitelist()
def get_work_order_details(work_order, company):
	work_order = frappe.get_doc("Work Order", work_order)
	pending_qty_to_produce = flt(work_order.qty) - flt(work_order.produced_qty)

	return {
		"from_bom": 1,
		"bom_no": work_order.bom_no,
		"use_multi_level_bom": work_order.use_multi_level_bom,
		"wip_warehouse": work_order.wip_warehouse,
		"fg_warehouse": work_order.fg_warehouse,
		"fg_completed_qty": pending_qty_to_produce
	}

def get_operating_cost_per_unit(work_order=None, bom_no=None):
	operating_cost_per_unit = 0
	if work_order:
		if not bom_no:
			bom_no = work_order.bom_no

		for d in work_order.get("operations"):
			if flt(d.completed_qty):
				operating_cost_per_unit += flt(d.actual_operating_cost) / flt(d.completed_qty)
			elif work_order.qty:
				operating_cost_per_unit += flt(d.planned_operating_cost) / flt(work_order.qty)

	# Get operating cost from BOM if not found in work_order.
	if not operating_cost_per_unit and bom_no:
		bom = frappe.db.get_value("BOM", bom_no, ["operating_cost", "quantity"], as_dict=1)
		if bom.quantity:
			operating_cost_per_unit = flt(bom.operating_cost) / flt(bom.quantity)

	return operating_cost_per_unit

def get_used_alternative_items(purchase_order=None, work_order=None):
	cond = ""

	if purchase_order:
		cond = "and ste.purpose = 'Send to Subcontractor' and ste.purchase_order = '{0}'".format(purchase_order)
	elif work_order:
		cond = "and ste.purpose = 'Material Transfer for Manufacture' and ste.work_order = '{0}'".format(work_order)

	if not cond: return {}

	used_alternative_items = {}
	data = frappe.db.sql(""" select sted.original_item, sted.uom, sted.conversion_factor,
			sted.item_code, sted.item_name, sted.conversion_factor,sted.stock_uom, sted.description
		from
			`tabStock Entry` ste, `tabStock Entry Detail` sted
		where
			sted.parent = ste.name and ste.docstatus = 1 and sted.original_item !=  sted.item_code
			{0} """.format(cond), as_dict=1)

	for d in data:
		used_alternative_items[d.original_item] = d

	return used_alternative_items

def get_valuation_rate_for_finished_good_entry(work_order):
	work_order_qty = flt(frappe.get_cached_value("Work Order",
		work_order, 'material_transferred_for_manufacturing'))

	field = "(SUM(total_outgoing_value) / %s) as valuation_rate" % (work_order_qty)

	stock_data = frappe.get_all("Stock Entry",
		fields = field,
		filters = {
			"docstatus": 1,
			"purpose": "Material Transfer for Manufacture",
			"work_order": work_order
		}
	)

	if stock_data:
		return stock_data[0].valuation_rate

@frappe.whitelist()
def get_uom_details(item_code, uom, qty):
	"""Returns dict `{"conversion_factor": [value], "transfer_qty": qty * [value]}`

	:param args: dict with `item_code`, `uom` and `qty`"""
	conversion_factor = get_conversion_factor(item_code, uom).get("conversion_factor")

	if not conversion_factor:
		frappe.msgprint(_("UOM coversion factor required for UOM: {0} in Item: {1}")
			.format(uom, item_code))
		ret = {'uom' : ''}
	else:
		ret = {
			'conversion_factor'		: flt(conversion_factor),
			'transfer_qty'			: flt(qty) * flt(conversion_factor)
		}
	return ret

@frappe.whitelist()
def get_expired_batch_items():
	return frappe.db.sql("""select b.item, sum(sle.actual_qty) as qty, sle.batch_no, sle.warehouse, sle.stock_uom\
	from `tabBatch` b, `tabStock Ledger Entry` sle
	where b.expiry_date <= %s
	and b.expiry_date is not NULL
	and b.batch_id = sle.batch_no
	group by sle.warehouse, sle.item_code, sle.batch_no""",(nowdate()), as_dict=1)

@frappe.whitelist()
def get_warehouse_details(args):
	if isinstance(args, string_types):
		args = json.loads(args)

	args = frappe._dict(args)

	ret = {}
	if args.warehouse and args.item_code:
		args.update({
			"posting_date": args.posting_date,
			"posting_time": args.posting_time,
		})
		ret = {
			"actual_qty" : get_previous_sle(args).get("qty_after_transaction") or 0,
			"basic_rate" : get_incoming_rate(args)
		}
	return ret

@frappe.whitelist()
def validate_sample_quantity(item_code, sample_quantity, qty, batch_no = None):
	if cint(qty) < cint(sample_quantity):
		frappe.throw(_("Sample quantity {0} cannot be more than received quantity {1}").format(sample_quantity, qty))
	retention_warehouse = frappe.db.get_single_value('Stock Settings', 'sample_retention_warehouse')
	retainted_qty = 0
	if batch_no:
		retainted_qty = get_batch_qty(batch_no, retention_warehouse, item_code)
	max_retain_qty = frappe.get_value('Item', item_code, 'sample_quantity')
	if retainted_qty >= max_retain_qty:
		frappe.msgprint(_("Maximum Samples - {0} have already been retained for Batch {1} and Item {2} in Batch {3}.").
			format(retainted_qty, batch_no, item_code, batch_no), alert=True)
		sample_quantity = 0
	qty_diff = max_retain_qty-retainted_qty
	if cint(sample_quantity) > cint(qty_diff):
		frappe.msgprint(_("Maximum Samples - {0} can be retained for Batch {1} and Item {2}.").
			format(max_retain_qty, batch_no, item_code), alert=True)
		sample_quantity = qty_diff
	return sample_quantity

def get_transferred_materials(work_order):
	itemwise_transfer_details = {}

	transferred_data = frappe.db.sql('''SELECT IFNULL(sed.original_item, sed.item_code) as item_code,
			sed.item_name, sed.s_warehouse as source_warehouse,
			sed.basic_rate as rate, sed.uom, sed.stock_uom, sed.description, sed.conversion_factor,
			sed.transfer_qty as stock_qty, sed.serial_no, sed.batch_no, sed.item_code as alternative_item,
			sum(CASE WHEN se.is_return = 1 THEN (qty * -1) ELSE qty END) as transferred_qty
		FROM `tabStock Entry` se, `tabStock Entry Detail` sed
		WHERE
			se.work_order = %(name)s and se.purpose = "Material Transfer for Manufacture"
			and se.docstatus = 1 and sed.parent = se.name
		GROUP BY
			sed.item_code, sed.original_item, sed.batch_no
	''', {'name': work_order}, as_dict=1)

	for d in transferred_data:
		batch_no = ''
		key = d.item_code
		if d.batch_no:
			batch_no = get_batch_details(key, d, itemwise_transfer_details)

		d.update({"batch_no": batch_no, "amount": flt(d.rate) * flt(d.transferred_qty)})
		itemwise_transfer_details.setdefault(key, {}).update(d)

	return itemwise_transfer_details

def get_batch_details(key, row, itemwise_transfer_details):
	batch_no = {row.batch_no: [row.transferred_qty, row.rate]}
	if key in itemwise_transfer_details:
		data = itemwise_transfer_details.get(key)
		batch_no.update(data["batch_no"])
		row.transferred_qty += data["transferred_qty"]

	return batch_no

def get_unconsumed_batches(item_data, row):
	batch_args = []

	transferred_batch = json.loads(row.batch_no)
	consumed_batch = json.loads(row.consumed_batch_no or '{}')

	if cint(frappe.get_cached_value('UOM', item_data.get("stock_uom"), "must_be_whole_number")):
		item_data["qty"] = frappe.utils.ceil(item_data.get("qty"))

	qty_to_consume = item_data.get("qty")

	for batch_no, data in transferred_batch.items():
		# Check the batch is already consumed or not
		available_qty = flt(data[0]) - consumed_batch.get(batch_no, 0)

		if available_qty <= 0 or qty_to_consume <= 0: continue

		args = copy.copy(item_data)
		if available_qty > qty_to_consume:
			args["qty"] = qty_to_consume
			qty_to_consume = 0
		else:
			args["qty"] = available_qty
			qty_to_consume -= available_qty

		args["batch_no"] = batch_no
		args["basic_rate"] = data[1]
		batch_args.append(args)

	return batch_args

def get_unconsumed_serial_nos(item_data, row):
	available_serial_nos = set(json.loads(row.serial_no)) - set(json.loads(row.consumed_serial_no or "[]"))
	qty_to_consume = item_data.get("qty")

	serial_nos = []
	for sn in available_serial_nos:
		if qty_to_consume <= 0: break

		qty_to_consume -= 1
		serial_nos.append(sn)

	item_data["serial_no"] = '\n'.join(serial_nos)