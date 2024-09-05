# Copyright 2021 ForgeFlow S.L. (https://www.forgeflow.com)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools.float_utils import float_compare, float_is_zero

import io
import csv
from collections import Counter

class CsvSerialsMatrix:
    def __init__(self, raw_txt):
        self.serials = {}
        serials_file = io.StringIO(raw_txt)
        reader = csv.reader(serials_file, delimiter=';')
        head = next(reader)
        self.product_fin = head[0]
        self.products_raw = [ prod_code for prod_code in head[1:] ]
        for line in reader:
            serial_fin = line[0]
            serials_raw = line[1:]
            if len(serials_raw) != self.n_raw_products:
                raise RuntimeError(f"Number of serials must be identical in each line (found {serials_raw}, expected {self.n_raw_products})")
            self.serials[serial_fin] = serials_raw

    @property
    def n_raw_products(self):
        return len(self.products_raw)

    @property
    def n_lines(self):
        return len(self.serials)

    @property
    def is_valid(self):
        return self.serials and self.products_raw

    def raw_serials_for_finished(self, fin_serial):
        return zip(self.products_raw, self.serials[fin_serial])

    def __repr__(self):
        return f"{self.products_raw} => {self.product_fin}: {self.n_lines} lines"


class MrpProductionSerialMatrix(models.TransientModel):
    _name = "mrp.production.serial.matrix"
    _description = "Mrp Production Serial Matrix"

    production_id = fields.Many2one(
        comodel_name="mrp.production",
        string="Manufacturing Order",
        readonly=True,
    )
    product_id = fields.Many2one(
        related="production_id.product_id",
        readonly=True,
    )
    company_id = fields.Many2one(
        related="production_id.company_id",
        readonly=True,
    )
    csv_import = fields.Text(
        help="Copy + Paste directly into this field, do not edit here as it may be very slow!",
    )
    csv_warning_msg = fields.Char(
        compute="_compute_ready_for_import"
    )
    is_ready_for_import = fields.Boolean(
        compute="_compute_ready_for_import"
    )
    finished_lot_ids = fields.Many2many(
        string="Finished Product Serial Numbers",
        comodel_name="stock.production.lot",
        domain="[('product_id', '=', product_id)]",
    )
    line_ids = fields.One2many(
        string="Matrix Cell",
        comodel_name="mrp.production.serial.matrix.line",
        inverse_name="wizard_id",
    )
    lot_selection_warning_msg = fields.Char(compute="_compute_lot_selection_warning")
    lot_selection_warning_ids = fields.Many2many(
        comodel_name="stock.production.lot", compute="_compute_lot_selection_warning"
    )
    lot_selection_warning_count = fields.Integer(
        compute="_compute_lot_selection_warning"
    )
    include_lots = fields.Boolean(
        string="Include Lots?",
        help="Include products tracked by Lots in matrix. Product tracket by "
        "serial numbers are always included.",
    )

    @api.depends("line_ids", "line_ids.component_lot_id")
    def _compute_lot_selection_warning(self):
        for rec in self:
            warning_lots = self.env["stock.production.lot"]
            warning_msgs = []
            # Serials:
            serial_lines = rec.line_ids.filtered(
                lambda l: l.component_id.tracking == "serial"
            )
            serial_counter = {}
            for sl in serial_lines:
                if not sl.component_lot_id:
                    continue
                serial_counter.setdefault(sl.component_lot_id, 0)
                serial_counter[sl.component_lot_id] += 1
            for lot, counter in serial_counter.items():
                if counter > 1:
                    warning_lots += lot
                    warning_msgs.append(
                        "Serial number %s selected several times" % lot.name
                    )
            # Lots
            lot_lines = rec.line_ids.filtered(
                lambda l: l.component_id.tracking == "lot"
            )
            lot_consumption = {}
            for ll in lot_lines:
                if not ll.component_lot_id:
                    continue
                lot_consumption.setdefault(ll.component_lot_id, 0)
                free_qty, reserved_qty = ll._get_available_and_reserved_quantities()
                available_quantity = free_qty + reserved_qty
                if (
                    available_quantity - lot_consumption[ll.component_lot_id]
                    < ll.lot_qty
                ):
                    warning_lots += ll.component_lot_id
                    warning_msgs.append(
                        "Lot %s not available at the needed qty (%s/%s)"
                        % (ll.component_lot_id.name, available_quantity, ll.lot_qty)
                    )
                lot_consumption[ll.component_lot_id] += ll.lot_qty

            not_filled_lines = rec.line_ids.filtered(
                lambda l: l.finished_lot_id and not l.component_lot_id
            )
            if not_filled_lines:
                not_filled_finshed_lots = not_filled_lines.mapped("finished_lot_id")
                warning_lots += not_filled_finshed_lots
                warning_msgs.append(
                    "Some cells are not filled for some finished serial number (%s)"
                    % ", ".join(not_filled_finshed_lots.mapped("name"))
                )
            rec.lot_selection_warning_msg = ", ".join(warning_msgs)
            rec.lot_selection_warning_ids = warning_lots
            rec.lot_selection_warning_count = len(warning_lots)

    @api.model
    def default_get(self, fields):
        res = super().default_get(fields)
        production_id = self.env.context["active_id"]
        active_model = self.env.context["active_model"]
        if not production_id:
            return res
        assert active_model == "mrp.production", "Bad context propagation"
        production = self.env["mrp.production"].browse(production_id)
        if not production.show_serial_matrix:
            raise UserError(
                _("The finished product of this MO is not tracked by serial numbers.")
            )

        finished_lots = self.env["stock.production.lot"]
        if production.lot_producing_id:
            finished_lots = production.lot_producing_id

        matrix_lines = self._get_matrix_lines(production, finished_lots)

        res.update(
            {
                "line_ids": [(0, 0, x) for x in matrix_lines],
                "production_id": production_id,
                "finished_lot_ids": [(4, lot_id, 0) for lot_id in finished_lots.ids],
                "csv_import": ""
            }
        )
        return res

    def _get_matrix_lines(self, production, finished_lots):
        tracked_components = []
        for move in production.move_raw_ids:
            rounding = move.product_id.uom_id.rounding
            if float_is_zero(move.product_qty, precision_rounding=rounding):
                # Component moves cannot be deleted in in-progress MO's; however,
                # they can be set to 0 units to consume. In such case, we ignore
                # the move.
                continue
            boml = move.bom_line_id
            # TODO: UoM (MO/BoM using different UoMs than product's defaults).
            if boml:
                qty_per_finished_unit = boml.product_qty / boml.bom_id.product_qty
            else:
                # The product could have been added for the specific MO but not
                # be part of the BoM.
                qty_per_finished_unit = move.product_qty / production.product_qty
            if move.product_id.tracking == "serial":
                for i in range(1, int(qty_per_finished_unit) + 1):
                    tracked_components.append((move.product_id, i, 1))
            elif move.product_id.tracking == "lot" and self.include_lots:
                tracked_components.append((move.product_id, 0, qty_per_finished_unit))

        matrix_lines = []
        current_lot = False
        new_lot_number = 0
        for _i in range(int(production.product_qty)):
            if finished_lots:
                current_lot = finished_lots[0]
            else:
                new_lot_number += 1
            for component_tuple in tracked_components:
                line = self._prepare_matrix_line(
                    component_tuple, finished_lot=current_lot, number=new_lot_number
                )
                matrix_lines.append(line)
            if current_lot:
                finished_lots -= current_lot
                current_lot = False
        return matrix_lines

    def _prepare_matrix_line(self, component_tuple, finished_lot=None, number=None):
        component, lot_no, lot_qty = component_tuple
        column_name = component.display_name
        if lot_no > 0:
            column_name += " (%s)" % lot_no
        res = {
            "component_id": component.id,
            "component_column_name": column_name,
            "lot_qty": lot_qty,
        }
        if finished_lot:
            if isinstance(finished_lot.id, models.NewId):
                # NewId instances are not handled correctly later, this is a
                # small workaround. In future versions it might not be needed.
                lot_id = finished_lot.id.origin
            else:
                lot_id = finished_lot.id
            res.update(
                {
                    "finished_lot_id": lot_id,
                    "finished_lot_name": finished_lot.name,
                }
            )
        elif isinstance(number, int):
            res.update(
                {
                    "finished_lot_name": _("(New Lot %s)") % number,
                }
            )
        return res

    def _validate(self, csv_data):
        try:
            csv = CsvSerialsMatrix(csv_data)
        except RuntimeError as err:
            return str(err)
        except:
            return "CSV can't be parsed"

        if csv.product_fin != self.product_id.default_code:
            return f"Finished product is {csv.product_fin}, expected {self.product_id.default_code}"

        expected_codes = []

        if self.line_ids:
            first_lot = self.line_ids[0].finished_lot_name
            expected_codes = sorted(line.component_id.default_code for line in self.line_ids if line.finished_lot_name == first_lot)

        codes_in_csv = sorted(csv.products_raw)
        if expected_codes != codes_in_csv:
            return f"Raw products in CSV: {codes_in_csv}, expected: {expected_codes}"

        return ""


    @api.depends("csv_import")
    def _compute_ready_for_import(self):
        self.ensure_one()
        if not self.csv_import:
            self.is_ready_for_import = False
            self.csv_warning_msg = ""
            return

        warning = self._validate(self.csv_import)

        self.csv_warning_msg = warning
        self.is_ready_for_import = not warning


    def button_csv_import(self):
        self.ensure_one()
        do_nothing = {  # return value that keeps open our wizard
            'view_mode': 'form',
            'res_model': self._name,
            'res_id': self.id,
            'type': 'ir.actions.act_window',
            'target': 'new'
        }

        if not self.csv_import:
            raise ValidationError("Nothing to import")

        csv = CsvSerialsMatrix(self.csv_import)

        # 1) Fill in finished_lot_ids. Use existing serials if possible, or create new ones if necessary.
        lots = self.env["stock.production.lot"]

        finished_lots = [(5,0,0)]
        for serial in csv.serials:
            serial_obj = lots.search([('name','=',serial),('product_id','=',self.product_id.id)])
            if serial_obj:
                finished_lots.append((4,serial_obj.id,0))
            else:
                vals = {'name': serial, 'product_id': self.product_id.id}
                finished_lots.append((0, 0, vals))

        self.write({"finished_lot_ids": finished_lots})
        self._onchange_finished_lot_ids()
        self.line_ids._compute_allowed_component_lot_ids()

        # 2) Fill in Matrix Cells, finding matching serial-objects. If no matching object is found, error out.
        for serial in csv.serials:
            lines = [line for line in self.line_ids if line.finished_lot_id and line.finished_lot_name == serial]
            for prod, raw_serial in csv.raw_serials_for_finished(serial):
                matching_line = next((l for l in lines if l.component_id.default_code == prod), None)
                if matching_line is None:
                    raise ValidationError(f"Cannot find matrix entry for {prod} in line {serial}")
                matching_lot_id = next((lot_id for lot_id in matching_line.allowed_component_lot_ids if lot_id.name == raw_serial), None)
                if matching_lot_id is None:
                    raise ValidationError(f"Serial {raw_serial} for {prod} is not available")
                matching_line.write({"component_lot_id": matching_lot_id})
                lines.remove(matching_line)

        return do_nothing


    @api.onchange("finished_lot_ids", "include_lots")
    def _onchange_finished_lot_ids(self):
        for rec in self:
            matrix_lines = self._get_matrix_lines(
                rec.production_id,
                rec.finished_lot_ids,
            )
            rec.line_ids = False
            rec.write({"line_ids": [(0, 0, x) for x in matrix_lines]})

    def button_validate(self):
        self.ensure_one()
        if self.lot_selection_warning_count > 0:
            raise UserError(
                _("Some issues has been detected in your selection: %s")
                % self.lot_selection_warning_msg
            )
        mos = self.env["mrp.production"]
        current_mo = self.production_id
        for fp_lot in self.finished_lot_ids:
            # Apply selected lots in matrix and set the qty producing
            current_mo.lot_producing_id = fp_lot
            current_mo.qty_producing = 1.0
            current_mo._set_qty_producing()
            for move in current_mo.move_raw_ids:
                rounding = move.product_id.uom_id.rounding
                if float_is_zero(move.product_qty, precision_rounding=rounding):
                    # Component moves cannot be deleted in in-progress MO's; however,
                    # they can be set to 0 units to consume. In such case, we ignore
                    # the move.
                    continue
                if move.product_id.tracking in ["serial", "lot"]:
                    # We filter using the lot nane because the ORM sometimes
                    # is not storing correctly the finished_lot_id in the lines
                    # after passing through the `_onchange_finished_lot_ids`
                    # method.
                    matrix_lines = self.line_ids.filtered(
                        lambda l: (
                            l.finished_lot_id == fp_lot
                            or l.finished_lot_name == fp_lot.name
                        )
                        and l.component_id == move.product_id
                    )
                    if matrix_lines:
                        self._amend_reservations(move, matrix_lines)
                        self._consume_selected_lots(move, matrix_lines)

            # Complete MO and create backorder if needed.
            mos += current_mo
            res = current_mo.button_mark_done()
            backorder_wizard = self.env["mrp.production.backorder"]
            if isinstance(res, dict) and res.get("res_model") == backorder_wizard._name:
                # create backorders...
                lines = res.get("context", {}).get(
                    "default_mrp_production_backorder_line_ids"
                )
                wizard = backorder_wizard.create(
                    {
                        "mrp_production_ids": current_mo.ids,
                        "mrp_production_backorder_line_ids": lines,
                    }
                )
                wizard.action_backorder()

                backorder_ids = (
                    current_mo.procurement_group_id.mrp_production_ids.filtered(
                        lambda mo: mo.state not in ["done", "cancel"]
                    )
                )
                current_mo = backorder_ids[0] if backorder_ids else False
                if not current_mo:
                    break
            else:
                break

        # TODO: not specified lots: auto create lots?
        if not mos:
            mos = self.production_id
        res = {
            "domain": [("id", "in", mos.ids)],
            "name": _("Manufacturing Orders"),
            "src_model": "mrp.production.serial.matrix",
            "view_type": "form",
            "view_mode": "tree,form",
            "view_id": False,
            "views": False,
            "res_model": "mrp.production",
            "type": "ir.actions.act_window",
        }
        return res

    def _amend_reservations(self, move, matrix_lines):
        lots_to_consume = matrix_lines.mapped("component_lot_id")
        lots_in_move = move.move_line_ids.mapped("lot_id")
        lots_to_reserve = lots_to_consume - lots_in_move
        if lots_to_reserve:
            to_unreserve_lots = lots_in_move - lots_to_consume
            move.move_line_ids.filtered(
                lambda l: l.lot_id in to_unreserve_lots
            ).unlink()
            for lot in lots_to_reserve:
                if move.product_id.tracking == "lot":
                    qty = sum(
                        matrix_lines.filtered(
                            lambda l: l.component_lot_id == lot
                        ).mapped("lot_qty")
                    )
                    qty = qty
                else:
                    qty = 1.0
                self._reserve_lot_in_move(move, lot, qty=qty)

        return True

    def _consume_selected_lots(self, move, matrix_lines):
        lots_to_consume = matrix_lines.mapped("component_lot_id")
        precision_digits = self.env["decimal.precision"].precision_get(
            "Product Unit of Measure"
        )
        for ml in move.move_line_ids:
            if ml.lot_id in lots_to_consume:
                if move.product_id.tracking == "lot":
                    qty = sum(
                        matrix_lines.filtered(
                            lambda l: l.component_lot_id == ml.lot_id
                        ).mapped("lot_qty")
                    )
                    ml.qty_done = qty
                else:
                    ml.qty_done = ml.product_qty
            elif float_is_zero(ml.product_qty, precision_digits=precision_digits):
                ml.unlink()
            else:
                ml.qty_done = 0.0

    def _reserve_lot_in_move(self, move, lot, qty):
        precision_digits = self.env["decimal.precision"].precision_get(
            "Product Unit of Measure"
        )
        available_quantity = self.env["stock.quant"]._get_available_quantity(
            move.product_id,
            move.location_id,
            lot_id=lot,
        )
        if (
            float_compare(available_quantity, 0.0, precision_digits=precision_digits)
            <= 0
        ):
            raise ValidationError(
                _("Serial/Lot number '%s' not available at source location.") % lot.name
            )
        move._update_reserved_quantity(
            qty,
            available_quantity,
            move.location_id,
            lot_id=lot,
            strict=True,
        )
