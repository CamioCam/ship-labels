"""
Tests for ups.py.

Every production label is billed the moment it is created, so these lean
toward the failure modes that cost money or ship the wrong thing: billing a
row twice, losing the tracking number for a label that already exists,
silently shipping fewer boxes than ordered, or guessing at an address.

Nothing here touches the network. UpsTestCase replaces requests outright and
fails any test that reaches for it, so a stub that stops matching reality
shows up as a failure rather than a live call against UPS.

    python3 -m unittest discover -s tests -t .
"""

import argparse
import contextlib
import csv
import io
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ups  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

BOXES = {
    "1130A": {"weight": 0.8, "length": 4.745, "width": 3.75, "height": 4.25,
              "description": "Box 1130A"},
    "1110A": {"weight": 0.5, "length": 4, "width": 3, "height": 2},
}

SHIPPER = {
    "name": "Test Shipper Co",
    "attention": "Ops",
    "phone": "5555550100",
    "email": "shipping@example.com",
    "address": ["100 Warehouse Way"],
    "city": "Springfield",
    "state": "CA",
    "zip": "62704",
}

TO = {
    "name": "Recipient Co",
    "phone": "2125551212",
    "address": ["350 Fifth Avenue"],
    "city": "New York",
    "state": "NY",
    "zip": "10118",
}


class UpsTestCase(unittest.TestCase):
    """Isolates every test from the network, the real config, and real output."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        for name, value in (
            ("LABELS", self.tmp / "labels"),
            ("PREVIEWS", self.tmp / "previews"),
            ("LOG", self.tmp / "shipments_log.csv"),
            ("BOXES_FILE", self.tmp / "boxes.json"),
        ):
            self._patch(mock.patch.object(ups, name, value))
        ups.LABELS.mkdir()
        ups.PREVIEWS.mkdir()
        ups.BOXES_FILE.write_text(json.dumps(BOXES))

        self.shipper_path = self.tmp / "shipper.json"
        self.shipper_path.write_text(json.dumps(SHIPPER))

        # A network call from a test is a bug in the test.
        for verb in ("post", "get", "delete"):
            self._patch(mock.patch.object(ups.requests, verb,
                                          side_effect=self._no_network))
        self._patch(mock.patch.object(ups, "get_token", return_value="test-token"))
        self._patch(mock.patch.dict(os.environ, {
            "UPS_ACCOUNT": "TEST01", "UPS_ENV": "cie", "UPS_NOTIFY_EMAIL": "",
        }))

    def _patch(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _no_network(*args, **kwargs):
        raise AssertionError("test attempted a network call")

    # -- helpers ---------------------------------------------------------

    def orders_csv(self, name="orders.csv"):
        """A private copy of the fixture, safe to write tracking numbers into."""
        dest = self.tmp / name
        shutil.copy(FIXTURES / "orders.csv", dest)
        return dest

    def read_fixture(self):
        return ups.read_orders(self.orders_csv(), "03")

    def build_row(self, index, overrides=None):
        """Build one shipment straight from a fixture row, with tweaks."""
        header, body, cols, _ = ups.read_orders(FIXTURES / "orders.csv", "03")
        row = list(body[index])
        for i, value in (overrides or {}).items():
            row[i] = value
        return ups.build_order(row, cols, ups.load_boxes(), "03")

    def fake_labels(self, cost="10.00"):
        """Stand-in for create_label that writes real files and counts packages."""
        counter = itertools.count(1)

        def _create(shipper, shipment, label_format):
            packages = shipment.get("packages") or [shipment["package"]]
            out = []
            for i, _pkg in enumerate(packages):
                tracking = f"1Z000000000000{next(counter):04d}"
                path = ups.LABELS / f"{tracking}.{ups.LABEL_EXT[label_format]}"
                path.write_bytes(b"label-bytes")
                out.append({
                    "tracking": tracking, "shipment_id": "S1", "label_path": path,
                    "format": label_format, "service": "Ground",
                    "to_name": shipment["to"]["name"], "to_city": "Somewhere, NY 10118",
                    # Only the first package carries the shipment total.
                    "cost": cost if i == 0 else "", "env": "cie",
                })
            return out

        return _create

    def run_command(self, func, args):
        """Run a cmd_* function, capturing its output."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = func(args)
        return code, out.getvalue(), err.getvalue()

    def csv_args(self, orders, **overrides):
        args = dict(orders=str(orders), shipper=str(self.shipper_path), format="GIF",
                    service="03", out=None, limit=None, dry_run=False,
                    skip_errors=False, yes=True)
        args.update(overrides)
        return argparse.Namespace(**args)

    @staticmethod
    def rows_of(path):
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.reader(fh))

    def tracking_column(self, path):
        rows = self.rows_of(path)
        idx = rows[0].index("Tracking Number")
        return [r[idx] for r in rows[1:]]


class AddressParsingTests(UpsTestCase):
    """A wrong city or state becomes a UPS address-correction surcharge."""

    def test_comma_run(self):
        self.assertEqual(
            ups.parse_address("1 Byron Way, Suite 4, Springfield, IL, 62704"),
            (["1 Byron Way", "Suite 4"], "Springfield", "IL", "62704"))

    def test_embedded_newline(self):
        self.assertEqual(
            ups.parse_address("2 Enigma Rd.\nPortland, OR 97205"),
            (["2 Enigma Rd."], "Portland", "OR", "97205"))

    def test_state_and_zip_in_one_token(self):
        self.assertEqual(
            ups.parse_address("6 Bridge St, Boulder, CO 80301"),
            (["6 Bridge St"], "Boulder", "CO", "80301"))

    def test_zip_plus_four(self):
        self.assertEqual(ups.parse_address("1 A St, Reno, NV 89501-1234")[3],
                         "89501-1234")

    def test_state_is_upper_cased(self):
        self.assertEqual(ups.parse_address("1 A St, Reno, nv 89501")[2], "NV")

    def test_street_lines_capped_at_three(self):
        lines = ups.parse_address("A, B, C, D, Reno, NV 89501")[0]
        self.assertEqual(lines, ["A", "B", "C"])

    def test_trailing_period_survives(self):
        # "Ave." is part of the street, not punctuation to tidy away.
        self.assertEqual(ups.parse_address("2 Enigma Rd.\nPortland, OR 97205")[0],
                         ["2 Enigma Rd."])

    def test_missing_state_raises(self):
        with self.assertRaisesRegex(ups.UpsError, "two-letter state"):
            ups.parse_address("5 Lunar Blvd, Cambridge, 02139")

    def test_missing_street_raises(self):
        with self.assertRaisesRegex(ups.UpsError, "no street address"):
            ups.parse_address("Cambridge, MA 02139")

    def test_missing_zip_raises(self):
        with self.assertRaisesRegex(ups.UpsError, "no ZIP"):
            ups.parse_address("5 Lunar Blvd, Cambridge, MA")

    def test_blank_raises(self):
        with self.assertRaisesRegex(ups.UpsError, "blank"):
            ups.parse_address("   ")


class ColumnMappingTests(UpsTestCase):
    """The form's headers repeat and near-collide; both mistakes ship wrong."""

    def setUp(self):
        super().setUp()
        self.header, self.body, self.cols, _ = self.read_fixture()

    def test_customer_email_is_not_the_submitter(self):
        self.assertEqual(self.header[self.cols["email"]], "Email")
        self.assertEqual(self.header[self.cols["submitter"]], "Email Address")
        self.assertNotEqual(self.cols["email"], self.cols["submitter"])

    def test_email_columns_survive_being_reordered(self):
        # "email" is a prefix of "email address", so a loose match only looks
        # correct while Email happens to come first. If the form ever reorders
        # its questions, prefix matching would mail labels to the submitter.
        header = list(self.header)
        customer, submitter = self.cols["email"], self.cols["submitter"]
        header[customer], header[submitter] = header[submitter], header[customer]
        cols = ups.map_columns(header)
        self.assertEqual(cols["email"], submitter, "must follow the header, not the position")
        self.assertEqual(cols["submitter"], customer)

    def test_repeated_box_header_is_not_collapsed(self):
        # "How many Box 1110A" appears twice; DictReader would drop one.
        found = [i for i, code in self.cols["boxes"] if code == "1110A"]
        self.assertEqual(len(found), 2, "both 1110A columns must be found")

    def test_every_box_column_is_found(self):
        codes = sorted({code for _, code in self.cols["boxes"]})
        self.assertEqual(codes, ["1110A", "1130A", "1172A", "1372A", "1510A",
                                 "1530A", "1732R", "1775R"])

    def test_missing_tracking_column_raises(self):
        header = [h for h in self.header if h != "Tracking Number"]
        with self.assertRaisesRegex(ups.UpsError, "Tracking Number"):
            ups.map_columns(header)

    def test_missing_address_column_raises(self):
        header = [h for h in self.header if not h.startswith("Ship to address")]
        with self.assertRaisesRegex(ups.UpsError, "address"):
            ups.map_columns(header)

    def test_cell_tolerates_short_rows(self):
        self.assertEqual(ups.cell(["a"], 5), "")
        self.assertEqual(ups.cell(["a"], None), "")


class BoxQuantityTests(UpsTestCase):
    """Truncating a quantity under-ships an order with no error."""

    QTY_COLUMN = 16  # "How many Box 1130A?"

    def quantity(self, raw):
        return self.build_row(0, {self.QTY_COLUMN: raw})

    def test_whole_number(self):
        shipment, _ = self.quantity("1")
        self.assertEqual(len(shipment["packages"]), 1)

    def test_float_that_is_a_whole_number(self):
        shipment, _ = self.quantity("2.0")
        self.assertEqual(len(shipment["packages"]), 2)

    def test_blank_and_zero_are_not_orders(self):
        for raw in ("", "0"):
            shipment, skip = self.quantity(raw)
            self.assertIsNone(shipment)
            self.assertEqual(skip, "no boxes ordered")

    def test_fractional_quantity_raises(self):
        with self.assertRaisesRegex(ups.UpsError, "whole number"):
            self.quantity("1.9")

    def test_negative_quantity_raises(self):
        with self.assertRaisesRegex(ups.UpsError, "negative"):
            self.quantity("-1")

    def test_non_numeric_quantity_raises(self):
        with self.assertRaisesRegex(ups.UpsError, "not a number"):
            self.quantity("abc")

    def test_repeated_columns_are_summed(self):
        # Row 5 orders 1110A once in each of the two identical columns.
        shipment, _ = self.build_row(5)
        self.assertEqual(len(shipment["packages"]), 3)

    def test_unknown_box_stops_the_run(self):
        with self.assertRaisesRegex(ups.UpsError, "1732R"):
            self.build_row(3)

    def test_each_box_is_its_own_package(self):
        shipment, _ = self.build_row(1)  # orders two 1130A
        self.assertEqual(len(shipment["packages"]), 2)
        self.assertEqual({p["weight"] for p in shipment["packages"]}, {0.8})


class BoxCatalogTests(UpsTestCase):
    """boxes.json is hand-edited, so bad entries must fail loudly on load."""

    def load(self, spec):
        ups.BOXES_FILE.write_text(json.dumps({"9999X": spec}))
        return ups.load_boxes()

    def test_valid_entry_loads(self):
        boxes = self.load({"weight": 1, "length": 2, "width": 3, "height": 4})
        self.assertEqual(boxes["9999X"]["weight"], 1)

    def test_comment_keys_are_ignored(self):
        ups.BOXES_FILE.write_text(json.dumps({"_comment": ["hi"], "9999X": {"weight": 1}}))
        self.assertEqual(list(ups.load_boxes()), ["9999X"])

    def test_missing_weight_raises(self):
        with self.assertRaisesRegex(ups.UpsError, "numeric weight"):
            self.load({"length": 2, "width": 3, "height": 4})

    def test_non_numeric_weight_raises(self):
        with self.assertRaisesRegex(ups.UpsError, "numeric weight"):
            self.load({"weight": "heavy"})

    def test_zero_or_negative_weight_raises(self):
        for bad in (0, -1):
            with self.assertRaisesRegex(ups.UpsError, "weight of"):
                self.load({"weight": bad})

    def test_partial_dimensions_raise(self):
        # The quiet one: package_block only sends Dimensions when all three
        # are present, so a box missing height would ship with none at all.
        with self.assertRaisesRegex(ups.UpsError, "missing height"):
            self.load({"weight": 1, "length": 2, "width": 3})

    def test_no_dimensions_is_allowed(self):
        self.assertIn("9999X", self.load({"weight": 1}))

    def test_bad_dimension_value_raises(self):
        with self.assertRaisesRegex(ups.UpsError, "width"):
            self.load({"weight": 1, "length": 2, "width": 0, "height": 4})

    def test_entry_must_be_an_object(self):
        with self.assertRaisesRegex(ups.UpsError, "must be an object"):
            self.load("0.8 lb")

    def test_missing_file_raises(self):
        ups.BOXES_FILE.unlink()
        with self.assertRaisesRegex(ups.UpsError, "not found"):
            ups.load_boxes()


class ServiceTests(UpsTestCase):
    def test_blank_uses_the_default(self):
        self.assertEqual(ups.parse_service("", "03"), "03")

    def test_named_methods(self):
        for text, code in (("Ground", "03"), ("2nd Day Air", "02"),
                           ("Next Day Air Saver", "13"), ("overnight", "01"),
                           ("3 Day Select", "12")):
            self.assertEqual(ups.parse_service(text, "03"), code, text)

    def test_raw_code_passes_through(self):
        self.assertEqual(ups.parse_service("02", "03"), "02")

    def test_unrecognized_method_raises(self):
        with self.assertRaisesRegex(ups.UpsError, "unrecognized shipping method"):
            ups.parse_service("Teleport", "03")


class NotificationTests(UpsTestCase):
    """An address in the ShipTo block notifies nobody; these blocks do."""

    def test_all_customer_contacts_are_parsed(self):
        self.assertEqual(ups.parse_emails("a@x.com, b@x.com; c@x.com"),
                         ["a@x.com", "b@x.com", "c@x.com"])

    def test_customer_submitter_and_standing_address(self):
        os.environ["UPS_NOTIFY_EMAIL"] = "sales@example.com"
        shipment, _ = self.build_row(0)
        self.assertEqual(ups.notify_addresses(shipment["to"]),
                         ["sales@example.com", "ada@example.com",
                          "grace@example.com", "submitter-a@example.com"])

    def test_submitter_is_notified_but_stays_off_the_label(self):
        shipment, _ = self.build_row(0)
        self.assertEqual(shipment["to"]["email"], "ada@example.com")
        self.assertIn("submitter-a@example.com", ups.notify_addresses(shipment["to"]))

    def test_duplicates_are_dropped_case_insensitively(self):
        os.environ["UPS_NOTIFY_EMAIL"] = "Sales@Example.com"
        found = ups.notify_addresses({"emails": ["sales@example.com", "a@x.com"]})
        self.assertEqual(found, ["Sales@Example.com", "a@x.com"])

    def test_standing_address_survives_a_crowded_row(self):
        os.environ["UPS_NOTIFY_EMAIL"] = "sales@example.com"
        found = ups.notify_addresses({"emails": [f"c{n}@x.com" for n in range(9)]})
        self.assertEqual(len(found), ups.MAX_NOTIFY_ADDRESSES)
        self.assertIn("sales@example.com", found)

    def test_from_name_appears_once_per_shipment(self):
        # UPS rejects a repeat with [120661].
        blocks = ups.notification_blocks(SHIPPER, {"emails": ["a@x.com"]})
        with_from = [b for b in blocks if "FromName" in b["EMail"]]
        self.assertEqual(len(with_from), 1)

    def test_ship_and_delivery_are_both_requested(self):
        blocks = ups.notification_blocks(SHIPPER, {"emails": ["a@x.com"]})
        self.assertEqual([b["NotificationCode"] for b in blocks],
                         [ups.NOTIFY_SHIP, ups.NOTIFY_DELIVERY])

    def test_no_recipients_means_no_notification_block(self):
        self.assertIsNone(ups.notification_blocks(SHIPPER, {"emails": []}))
        request = ups.build_request(SHIPPER, {"to": TO, "package": {"weight": 1}}, "GIF")
        self.assertNotIn("ShipmentServiceOptions",
                         request["ShipmentRequest"]["Shipment"])


class RequestBuildingTests(UpsTestCase):
    def request_for(self, shipment):
        return ups.build_request(SHIPPER, shipment, "GIF")["ShipmentRequest"]

    def test_single_package_shape_still_works(self):
        req = self.request_for({"to": TO, "package": {"weight": 3}})
        self.assertEqual(len(req["Shipment"]["Package"]), 1)

    def test_multiple_packages(self):
        req = self.request_for({"to": TO, "packages": [{"weight": 1}] * 3})
        self.assertEqual(len(req["Shipment"]["Package"]), 3)

    def test_dimensions_are_sent_when_complete(self):
        req = self.request_for({"to": TO, "packages": [BOXES["1130A"]]})
        dims = req["Shipment"]["Package"][0]["Dimensions"]
        self.assertEqual((dims["Length"], dims["Width"], dims["Height"]),
                         ("4.745", "3.75", "4.25"))

    def test_label_is_four_by_six(self):
        req = self.request_for({"to": TO, "package": {"weight": 1}})
        self.assertEqual(req["LabelSpecification"]["LabelStockSize"],
                         {"Height": "6", "Width": "4"})

    def test_label_specification_is_a_sibling_of_shipment(self):
        req = self.request_for({"to": TO, "package": {"weight": 1}})
        self.assertIn("LabelSpecification", req)
        self.assertNotIn("LabelSpecification", req["Shipment"])

    def test_weightless_package_raises_rather_than_key_error(self):
        with self.assertRaisesRegex(ups.UpsError, "no weight"):
            ups.package_block({"description": "Box 9999X"}, "d")

    def test_incomplete_address_raises(self):
        with self.assertRaisesRegex(ups.UpsError, "missing: phone"):
            ups.address_block({"name": "X", "city": "Y", "state": "CA", "zip": "90001"})


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


class CreateLabelTests(UpsTestCase):
    def respond(self, package_results, charges="30.00"):
        payload = {"ShipmentResponse": {"ShipmentResults": {
            "ShipmentIdentificationNumber": "S1",
            "PackageResults": package_results,
            "ShipmentCharges": {"TotalCharges": {"MonetaryValue": charges}},
        }}}
        return mock.patch.object(ups.requests, "post",
                                 return_value=FakeResponse(payload))

    @staticmethod
    def package(n):
        import base64
        return {"TrackingNumber": f"1Z0000000000000{n}",
                "ShippingLabel": {"GraphicImage": base64.b64encode(b"bytes").decode()}}

    def test_shipment_total_is_not_repeated_per_package(self):
        # Summing the log must give the real total, not a multiple of it.
        with self.respond([self.package(n) for n in (1, 2, 3)]):
            rows = ups.create_label(SHIPPER, {"to": TO, "packages": [{"weight": 1}] * 3},
                                    "GIF")
        self.assertEqual([r["cost"] for r in rows], ["30.00", "", ""])
        self.assertEqual(sum(float(r["cost"]) for r in rows if r["cost"]), 30.00)

    def test_single_package_result_is_normalized_to_a_list(self):
        with self.respond(self.package(1)):
            rows = ups.create_label(SHIPPER, {"to": TO, "package": {"weight": 1}}, "GIF")
        self.assertEqual(len(rows), 1)

    def test_label_bytes_are_written_untouched(self):
        with self.respond(self.package(1)):
            rows = ups.create_label(SHIPPER, {"to": TO, "package": {"weight": 1}}, "GIF")
        self.assertEqual(rows[0]["label_path"].read_bytes(), b"bytes")

    def test_ups_error_message_is_surfaced(self):
        payload = {"response": {"errors": [
            {"code": "120661", "message": "Too many FromName"}]}}
        with mock.patch.object(ups.requests, "post",
                               return_value=FakeResponse(payload, 400)):
            with self.assertRaisesRegex(ups.UpsError, r"120661.*Too many FromName"):
                ups.create_label(SHIPPER, {"to": TO, "package": {"weight": 1}}, "GIF")


class WriteOrdersTests(UpsTestCase):
    """The sheet is the only record of labels that are already billed."""

    def test_round_trip_preserves_embedded_newlines(self):
        path = self.orders_csv()
        header, body, cols, _ = ups.read_orders(path, "03")
        before = self.rows_of(path)
        ups.write_orders(path, header, body)
        after = self.rows_of(path)
        self.assertEqual(before, after)
        self.assertIn("\n", after[2][5], "the multi-line address must survive")

    def test_failed_write_leaves_the_original_intact(self):
        path = self.tmp / "sheet.csv"
        path.write_text("a,b\r\n1,2\r\n")
        before = path.read_text()

        class Exploding:
            def __init__(self, *args, **kwargs):
                pass

            def writerow(self, row):
                pass

            def writerows(self, rows):
                raise RuntimeError("disk full")

        with mock.patch.object(ups.csv, "writer", Exploding):
            with self.assertRaises(RuntimeError):
                ups.write_orders(path, ["a", "b"], [["1", "2"]])

        self.assertEqual(path.read_text(), before)
        leftovers = [p.name for p in self.tmp.iterdir() if p.name.startswith(".ups-")]
        self.assertEqual(leftovers, [], "temp file must be cleaned up")


class ReadOrdersTests(UpsTestCase):
    def test_rows_are_classified(self):
        _, _, _, orders = self.read_fixture()
        self.assertEqual(len(orders), 7)
        self.assertIsNotNone(orders[0]["shipment"])
        self.assertIn("already shipped", orders[2]["skip"])
        self.assertIn("1732R", orders[3]["error"])
        self.assertIn("two-letter state", orders[4]["error"])
        self.assertEqual(orders[6]["skip"], "no boxes ordered")

    def test_shipped_rows_are_never_rebuilt(self):
        _, _, _, orders = self.read_fixture()
        self.assertIsNone(orders[2]["shipment"])

    def test_company_is_the_name_and_contact_is_the_attention(self):
        _, _, _, orders = self.read_fixture()
        to = orders[0]["shipment"]["to"]
        self.assertEqual(to["name"], "Analytical Engine Co")
        self.assertEqual(to["attention"], "Ada Lovelace")


class ShipCsvTests(UpsTestCase):
    """End-to-end, with label creation stubbed but the CSV work real."""

    def setUp(self):
        super().setUp()
        # A Mock, not a bare function: "nothing was billed" has to be asserted
        # against the billing call itself. Inferring it from an unchanged
        # tracking column is exactly how a billed label goes missing.
        self.create_label = mock.Mock(side_effect=self.fake_labels())
        self._patch(mock.patch.object(ups, "create_label", self.create_label))
        self._patch(mock.patch.object(ups, "render_preview", return_value=None))

    def assertNothingBilled(self):
        self.create_label.assert_not_called()
        self.assertEqual(list(ups.LABELS.iterdir()), [], "no label files either")

    def test_dry_run_creates_nothing(self):
        path = self.orders_csv()
        code, out, _ = self.run_command(
            ups.cmd_ship_csv, self.csv_args(path, dry_run=True, skip_errors=True))
        self.assertEqual(code, 0)
        self.assertIn("Dry run", out)
        self.assertEqual(self.tracking_column(path)[0], "")
        self.assertNothingBilled()

    def test_parse_errors_stop_the_run_before_billing(self):
        path = self.orders_csv()
        with self.assertRaisesRegex(ups.UpsError, "could not be parsed"):
            self.run_command(ups.cmd_ship_csv, self.csv_args(path))
        # Nothing shipped: the only tracking number is the one already there.
        self.assertEqual(self.tracking_column(path),
                         ["", "", "1Z999AA10123456784", "", "", "", ""])
        self.assertNothingBilled()

    def test_missing_output_directory_fails_before_billing(self):
        # write_orders() puts its temp file beside the destination, so a
        # missing parent used to raise only after a label was already paid for.
        path = self.orders_csv()
        out = self.tmp / "no-such-dir" / "updated.csv"
        with self.assertRaisesRegex(ups.UpsError, "does not exist"):
            self.run_command(ups.cmd_ship_csv,
                             self.csv_args(path, out=str(out), skip_errors=True))
        self.assertNothingBilled()
        self.assertEqual(self.tracking_column(path)[0], "")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root ignores directory permissions")
    def test_unwritable_output_directory_fails_before_billing(self):
        path = self.orders_csv()
        locked = self.tmp / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        with self.assertRaisesRegex(ups.UpsError, "cannot write"):
            self.run_command(ups.cmd_ship_csv,
                             self.csv_args(path, out=str(locked / "updated.csv"),
                                           skip_errors=True))
        self.assertNothingBilled()

    def test_skip_errors_ships_the_good_rows(self):
        path = self.orders_csv()
        code, _, _ = self.run_command(
            ups.cmd_ship_csv, self.csv_args(path, skip_errors=True))
        tracking = self.tracking_column(path)
        self.assertEqual(code, 1, "still non-zero: some rows failed to parse")
        self.assertTrue(tracking[0].startswith("1Z"))
        self.assertEqual(tracking[2], "1Z999AA10123456784", "must not be re-shipped")
        self.assertEqual(tracking[3], "", "unknown box must not ship")
        self.assertEqual(tracking[4], "", "bad address must not ship")

    def test_multi_box_row_records_every_tracking_number(self):
        path = self.orders_csv()
        self.run_command(ups.cmd_ship_csv,
                         self.csv_args(path, skip_errors=True))
        self.assertEqual(len(self.tracking_column(path)[1].split(", ")), 2)

    def test_limit_ships_only_the_first_rows(self):
        path = self.orders_csv()
        self.run_command(ups.cmd_ship_csv,
                         self.csv_args(path, limit=1, skip_errors=True))
        tracking = self.tracking_column(path)
        self.assertTrue(tracking[0])
        self.assertEqual(tracking[1], "")

    def test_rerunning_does_not_ship_a_row_twice(self):
        path = self.orders_csv()
        self.run_command(ups.cmd_ship_csv, self.csv_args(path, skip_errors=True))
        first = self.tracking_column(path)
        self.run_command(ups.cmd_ship_csv, self.csv_args(path, skip_errors=True))
        self.assertEqual(self.tracking_column(path), first)

    def test_existing_out_file_refuses_rather_than_rebilling(self):
        path = self.orders_csv()
        out = self.tmp / "updated.csv"
        out.write_text("stale\n")
        with self.assertRaisesRegex(ups.UpsError, "already exists"):
            self.run_command(ups.cmd_ship_csv,
                             self.csv_args(path, out=str(out), skip_errors=True))
        self.assertNothingBilled()

    def test_out_file_leaves_the_input_untouched(self):
        path = self.orders_csv()
        out = self.tmp / "updated.csv"
        self.run_command(ups.cmd_ship_csv,
                         self.csv_args(path, out=str(out), skip_errors=True))
        self.assertEqual(self.tracking_column(path)[0], "")
        self.assertTrue(self.tracking_column(out)[0].startswith("1Z"))

    def test_backup_is_written_before_the_sheet_changes(self):
        path = self.orders_csv()
        original = path.read_bytes()
        self.run_command(ups.cmd_ship_csv, self.csv_args(path, skip_errors=True))
        backup = path.with_suffix(path.suffix + ".bak")
        self.assertEqual(backup.read_bytes(), original)

    def test_preview_failure_cannot_lose_a_billed_label(self):
        path = self.orders_csv()
        with mock.patch.object(ups, "render_preview",
                               side_effect=RuntimeError("pillow exploded")):
            code, _, err = self.run_command(
                ups.cmd_ship_csv, self.csv_args(path, limit=1, skip_errors=True))
        self.assertTrue(self.tracking_column(path)[0].startswith("1Z"))
        self.assertIn("no preview", err)
        self.assertEqual(code, 1, "parse errors remain, but the label was recorded")

    def test_ups_failure_leaves_the_row_shippable(self):
        path = self.orders_csv()
        with mock.patch.object(ups, "create_label",
                               side_effect=ups.UpsError("UPS said no")):
            code, _, err = self.run_command(
                ups.cmd_ship_csv, self.csv_args(path, limit=1, skip_errors=True))
        self.assertEqual(code, 1)
        self.assertIn("UPS said no", err)
        self.assertEqual(self.tracking_column(path)[0], "")

    def test_log_records_one_row_per_label(self):
        path = self.orders_csv()
        self.run_command(ups.cmd_ship_csv,
                         self.csv_args(path, limit=2, skip_errors=True))
        with open(ups.LOG, newline="") as fh:
            logged = list(csv.DictReader(fh))
        self.assertEqual(len(logged), 3, "one single-box row plus one two-box row")
        self.assertEqual(sum(float(r["cost"]) for r in logged if r["cost"]), 20.00)


class CmdShipTests(UpsTestCase):
    """The JSON path predates ship-csv and must keep working."""

    def setUp(self):
        super().setUp()
        self._patch(mock.patch.object(ups, "render_preview", return_value=None))

    def shipments_file(self, payload):
        path = self.tmp / "shipments.json"
        path.write_text(json.dumps(payload))
        return argparse.Namespace(shipments=str(path), shipper=str(self.shipper_path),
                                  format="GIF", yes=True)

    def test_one_shipment_of_three_packages_succeeds(self):
        # Counting packages instead of shipments used to make this exit 1.
        args = self.shipments_file({"to": TO, "packages": [{"weight": 1}] * 3})
        with mock.patch.object(ups, "create_label", self.fake_labels()):
            code, out, _ = self.run_command(ups.cmd_ship, args)
        self.assertEqual(code, 0)
        self.assertEqual(out.count("tracking 1Z"), 3)

    def test_failure_exits_non_zero(self):
        args = self.shipments_file([{"to": TO, "package": {"weight": 1}}])
        with mock.patch.object(ups, "create_label",
                               side_effect=ups.UpsError("nope")):
            code, _, err = self.run_command(ups.cmd_ship, args)
        self.assertEqual(code, 1)
        self.assertIn("nope", err)

    def test_missing_shipper_file_is_explained(self):
        args = self.shipments_file({"to": TO, "package": {"weight": 1}})
        args.shipper = str(self.tmp / "absent.json")
        with self.assertRaisesRegex(ups.UpsError, "shipper.example.json"):
            self.run_command(ups.cmd_ship, args)


class PrinterDetectionTests(UpsTestCase):
    """
    The CUPS queue name is nothing like the model name, and guessing it wrong
    produces "lpr: No such file or directory", which points at the file.
    """

    LPSTAT_P = ("printer Brother_HL_L2340D_series is idle.  enabled since Mon\n"
                "printer Zebra_Technologies_ZTC_GC420d__EPL_ is idle.  enabled since Wed\n")

    def lpstat(self, listed=LPSTAT_P, default="system default destination: Brother_HL_L2340D_series"):
        def run(cmd, **kwargs):
            text = listed if cmd[1] == "-p" else default
            return subprocess.CompletedProcess(cmd, 0, stdout=text, stderr="")
        return mock.patch.multiple(
            ups,
            shutil=mock.MagicMock(which=mock.Mock(return_value="/usr/bin/lpstat")),
            subprocess=mock.MagicMock(run=mock.Mock(side_effect=run),
                                      SubprocessError=subprocess.SubprocessError))

    def test_zebra_wins_over_the_default_printer(self):
        with self.lpstat():
            self.assertEqual(ups.detect_printer(), "Zebra_Technologies_ZTC_GC420d__EPL_")

    def test_generic_label_printer_does_not_shadow_the_zebra(self):
        # A DYMO_LabelWriter on the same desk matches the generic "label"
        # hint. Raw EPL sent to it prints pages of garbage.
        zebra, dymo = "Zebra_Technologies_ZTC_GC420d__EPL_", "DYMO_LabelWriter_450"
        for first, second in ((dymo, zebra), (zebra, dymo)):
            with self.subTest(order=(first, second)):
                self.assertEqual(ups.pick_queue([first, second], None), zebra)

    def test_two_equally_plausible_printers_pick_nothing(self):
        self.assertIsNone(
            ups.pick_queue(["Zebra_GC420d__EPL_", "Zebra_ZD410"], None))

    def test_the_default_breaks_a_tie_only_if_it_is_a_candidate(self):
        pair = ["Zebra_GC420d__EPL_", "Zebra_ZD410"]
        self.assertEqual(ups.pick_queue(pair, "Zebra_ZD410"), "Zebra_ZD410")
        self.assertIsNone(ups.pick_queue(pair, "Office_Laser"))

    def test_a_lone_generic_label_printer_is_still_used(self):
        self.assertEqual(ups.pick_queue(["DYMO_LabelWriter_450", "Office_Laser"],
                                        None), "DYMO_LabelWriter_450")

    def test_falls_back_to_the_default_when_nothing_matches(self):
        listed = "printer Office_Laser is idle.\nprinter Front_Desk is idle.\n"
        with self.lpstat(listed=listed,
                         default="system default destination: Front_Desk"):
            self.assertEqual(ups.detect_printer(), "Front_Desk")

    def test_a_lone_printer_is_used(self):
        with self.lpstat(listed="printer Office_Laser is idle.\n", default=""):
            self.assertEqual(ups.detect_printer(), "Office_Laser")

    def test_ambiguous_printers_return_nothing(self):
        listed = "printer Office_Laser is idle.\nprinter Front_Desk is idle.\n"
        with self.lpstat(listed=listed, default=""):
            self.assertIsNone(ups.detect_printer())

    def test_missing_lpstat_is_not_an_error(self):
        with mock.patch.object(ups, "shutil",
                               mock.MagicMock(which=mock.Mock(return_value=None))):
            self.assertIsNone(ups.detect_printer())

    def test_lpstat_blowing_up_is_not_an_error(self):
        # Printer discovery must never take down a shipping run.
        with mock.patch.multiple(
                ups,
                shutil=mock.MagicMock(which=mock.Mock(return_value="/usr/bin/lpstat")),
                subprocess=mock.MagicMock(
                    run=mock.Mock(side_effect=OSError("boom")),
                    SubprocessError=subprocess.SubprocessError)):
            self.assertIsNone(ups.detect_printer())


class PrintInstructionTests(UpsTestCase):
    def rows(self, count, suffix="epl"):
        out = []
        for n in range(count):
            path = ups.LABELS / f"1Z00000000000000{n}.{suffix}"
            path.write_bytes(b"x")
            out.append({"label_path": path})
        return out

    def instructions(self, rows, label_format="EPL", queue="Zebra_GC420d__EPL_"):
        buf = io.StringIO()
        with mock.patch.object(ups, "detect_printer", return_value=queue), \
             contextlib.redirect_stdout(buf):
            ups.print_instructions(rows, label_format)
        return buf.getvalue()

    def test_single_label_is_a_plain_command(self):
        out = self.instructions(self.rows(1))
        self.assertIn("lpr -o raw", out)
        self.assertNotIn("for f in", out)

    def test_many_labels_are_one_pasteable_loop(self):
        out = self.instructions(self.rows(7))
        self.assertIn("Print 7 label(s)", out)
        self.assertIn("for f in", out)
        self.assertIn('do lpr -o raw -P Zebra_GC420d__EPL_ "$f"; done', out)
        self.assertEqual(out.count(".epl"), 7)

    def test_paths_are_absolute(self):
        # The relative form only works from the repo directory.
        out = self.instructions(self.rows(2))
        for line in out.splitlines():
            if "lpr" in line or "for f in" in line:
                self.assertNotIn(" labels/", line)
        self.assertIn(str(ups.LABELS.resolve()), out)

    @staticmethod
    def as_a_shell_sees_it(output):
        """
        Run the generated command through a real shell, with lpr swapped for
        printf, and return the arguments the shell actually produced.

        shlex.split cannot stand in here: it expands nothing, so it reports
        "$HOME" and '$HOME' identically and would pass either way. Only a
        shell shows whether a pasted command means what it says.
        """
        command = [l for l in output.splitlines() if "lpr" in l][0].strip()
        proof = command.replace("lpr -o raw", "printf '%s\\n'", 1)
        result = subprocess.run(["/bin/sh", "-c", proof],
                                capture_output=True, text=True, timeout=10)
        return result.stdout.splitlines()

    def test_hostile_paths_survive_a_real_shell(self):
        # Double quotes would expand $HOME and run $(...) on paste.
        for name in ("with space", "with'quote", "with$HOME", "with$(echo x)",
                     "with`echo x`", "with\\backslash", "with;semicolon"):
            with self.subTest(name=name):
                folder = ups.LABELS / name
                folder.mkdir()
                path = folder / "1Z1.epl"
                path.write_bytes(b"x")
                printed = self.as_a_shell_sees_it(
                    self.instructions([{"label_path": path}]))
                self.assertEqual(printed[-1], str(path.resolve()),
                                 f"{name!r} does not survive being pasted")

    def test_hostile_queue_name_survives_a_real_shell(self):
        queue = "Zebra $(echo substituted) Printer"
        printed = self.as_a_shell_sees_it(self.instructions(self.rows(1), queue=queue))
        self.assertEqual(printed[printed.index("-P") + 1], queue)

    def test_undetected_printer_still_shows_the_command(self):
        out = self.instructions(self.rows(2), queue=None)
        self.assertIn("lpstat -p", out, "tell them how to find the queue")
        command = [l for l in out.splitlines() if "lpr -o raw" in l][0]
        self.assertNotIn('-P "', command, "no queue to name, so no -P")

    def test_raster_formats_get_no_raw_command(self):
        # `lpr -o raw` is for printer languages; a GIF prints via the driver.
        for fmt, suffix in (("GIF", "gif"), ("PNG", "png"), ("PDF", "pdf")):
            self.assertEqual(self.instructions(self.rows(1, suffix), fmt), "", fmt)

    def test_no_labels_no_output(self):
        self.assertEqual(self.instructions([], "EPL"), "")


class ReturnAddressTests(UpsTestCase):
    """
    UPS prints exactly one street line in the return-address block.

    Verified against cie: ["100 Warehouse Way", "Unit 7"] prints only the
    street, and reversing them prints only the unit. Both lines reach UPS,
    so nothing errors - the line just vanishes from the label.
    """

    def warn_for(self, address):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            ups.warn_unprinted_shipper_lines(dict(SHIPPER, address=address),
                                             "shipper.json")
        return err.getvalue()

    def test_second_line_is_flagged(self):
        warning = self.warn_for(["100 Warehouse Way", "Unit 7"])
        self.assertIn("only the first return-address line", warning)
        self.assertIn("'Unit 7'", warning)

    def test_suggestion_is_the_combined_line(self):
        self.assertIn('"100 Warehouse Way Unit 7"',
                      self.warn_for(["100 Warehouse Way", "Unit 7"]))

    def test_single_line_is_silent(self):
        self.assertEqual(self.warn_for(["100 Warehouse Way Unit 7"]), "")

    def test_blank_padding_does_not_trigger_it(self):
        self.assertEqual(self.warn_for(["100 Warehouse Way", ""]), "")

    def test_both_lines_are_still_sent_to_ups(self):
        # The warning is about the printed label, not the request. UPS should
        # still receive everything for its own records.
        shipper = dict(SHIPPER, address=["100 Warehouse Way", "Unit 7"])
        req = ups.build_request(shipper, {"to": TO, "package": {"weight": 1}}, "GIF")
        for block in ("Shipper", "ShipFrom"):
            self.assertEqual(
                req["ShipmentRequest"]["Shipment"][block]["Address"]["AddressLine"],
                ["100 Warehouse Way", "Unit 7"], block)

    def test_recipient_keeps_a_second_line(self):
        # Only the return block is limited; ShipTo prints both.
        to = dict(TO, address=["350 Fifth Avenue", "Floor 20"])
        req = ups.build_request(SHIPPER, {"to": to, "package": {"weight": 1}}, "GIF")
        self.assertEqual(
            req["ShipmentRequest"]["Shipment"]["ShipTo"]["Address"]["AddressLine"],
            ["350 Fifth Avenue", "Floor 20"])

    def test_ship_csv_warns_before_shipping(self):
        self.shipper_path.write_text(json.dumps(
            dict(SHIPPER, address=["100 Warehouse Way", "Unit 7"])))
        path = self.orders_csv()
        with mock.patch.object(ups, "create_label", self.fake_labels()), \
             mock.patch.object(ups, "render_preview", return_value=None):
            _, _, err = self.run_command(
                ups.cmd_ship_csv, self.csv_args(path, limit=1, skip_errors=True))
        self.assertIn("only the first return-address line", err)


class TokenVerificationTests(UpsTestCase):
    """
    `token` is the one command people trust before shipping.

    It has to fail closed: OAuth succeeding proves nothing, Rating succeeding
    proves nothing about Shipping, and a Shipping response that is merely not
    a 401 proves nothing at all.
    """

    RATE_OK = {"RateResponse": {"RatedShipment": {
        "TotalCharges": {"MonetaryValue": "24.85", "CurrencyCode": "USD"}}}}
    SHIP_REJECTED = {"response": {"errors": [
        {"code": "9120004", "message": "Missing shipment information."}]}}
    UNAUTHORIZED = {"response": {"errors": [
        {"code": "250002", "message": "Invalid Authentication Information."}]}}

    def args(self, **overrides):
        args = dict(shipper=str(self.shipper_path), no_verify=False)
        args.update(overrides)
        return argparse.Namespace(**args)

    def probes(self, rating, shipping):
        """Route /Rate and /ship to their own canned responses, recording calls."""
        self.calls = []

        def post(url, **kwargs):
            self.calls.append((url, kwargs.get("json")))
            return shipping if "/shipments/" in url else rating

        return mock.patch.object(ups.requests, "post", side_effect=post)

    def run_token(self, rating, shipping, **overrides):
        with self.probes(rating, shipping):
            return self.run_command(ups.cmd_token, self.args(**overrides))

    def test_both_products_reachable(self):
        code, out, _ = self.run_token(FakeResponse(self.RATE_OK),
                                      FakeResponse(self.SHIP_REJECTED, 400))
        self.assertEqual(code, 0)
        self.assertIn("Rating   OK", out)
        self.assertIn("Shipping OK", out)
        self.assertIn("24.85 USD", out)

    def test_shipping_probe_carries_no_shipment(self):
        # The probe is only safe because there is nothing in it to ship.
        self.run_token(FakeResponse(self.RATE_OK), FakeResponse(self.SHIP_REJECTED, 400))
        ship_calls = [body for url, body in self.calls if "/shipments/" in url]
        self.assertEqual(len(ship_calls), 1)
        self.assertNotIn("Shipment", ship_calls[0]["ShipmentRequest"])
        self.assertNotIn("LabelSpecification", ship_calls[0]["ShipmentRequest"])

    def test_rating_failure_stops_before_probing_shipping(self):
        code, _, err = self.run_token(FakeResponse(self.UNAUTHORIZED, 401),
                                      FakeResponse(self.SHIP_REJECTED, 400))
        self.assertEqual(code, 1)
        self.assertIn("250002", err)
        self.assertEqual([u for u, _ in self.calls if "/shipments/" in u], [])

    def test_shipping_unauthorized_is_a_failure(self):
        code, out, err = self.run_token(FakeResponse(self.RATE_OK),
                                        FakeResponse(self.UNAUTHORIZED, 401))
        self.assertEqual(code, 1)
        self.assertIn("Rating   OK", out, "rating really did work")
        self.assertIn("Shipping FAILED", err)
        self.assertIn("Subscription APIs", err)
        self.assertNotIn("can rate and ship", out)

    def test_unexpected_shipping_status_is_not_success(self):
        # A 500 says nothing about entitlement; reporting OK here is how the
        # old OAuth-only check misled us.
        for status in (404, 429, 500, 503):
            with self.subTest(status=status):
                code, out, err = self.run_token(
                    FakeResponse(self.RATE_OK), FakeResponse({"x": 1}, status))
                self.assertEqual(code, 1)
                self.assertIn("Shipping UNPROVEN", err)
                self.assertNotIn("can rate and ship", out)

    def test_success_is_not_claimed_on_a_200(self):
        # UPS should never accept the empty probe, but if it ever did, that is
        # not proof of anything and must not read as success.
        code, out, _ = self.run_token(FakeResponse(self.RATE_OK),
                                      FakeResponse({"ShipmentResponse": {}}, 200))
        self.assertEqual(code, 1)
        self.assertNotIn("can rate and ship", out)

    def test_rated_shipment_list_form_is_handled(self):
        payload = {"RateResponse": {"RatedShipment": [
            {"TotalCharges": {"MonetaryValue": "9.99", "CurrencyCode": "USD"}}]}}
        code, out, _ = self.run_token(FakeResponse(payload),
                                      FakeResponse(self.SHIP_REJECTED, 400))
        self.assertEqual(code, 0)
        self.assertIn("9.99", out)

    def test_no_verify_makes_no_calls(self):
        # requests is booby-trapped by the base case, so reaching the network
        # here would fail the test rather than pass silently.
        code, _, _ = self.run_command(ups.cmd_token, self.args(no_verify=True))
        self.assertEqual(code, 0)

    def test_explicit_missing_shipper_raises(self):
        with self.assertRaisesRegex(ups.UpsError, "not found"):
            self.run_command(ups.cmd_token,
                             self.args(shipper=str(self.tmp / "absent.json")))

    def test_rating_probe_uses_the_shipper_address(self):
        with self.probes(FakeResponse(self.RATE_OK),
                         FakeResponse(self.SHIP_REJECTED, 400)):
            self.run_command(ups.cmd_token, self.args())
        rate = [body for url, body in self.calls if "/rating/" in url][0]
        shipper = rate["RateRequest"]["Shipment"]["Shipper"]
        self.assertEqual(shipper["Address"]["PostalCode"], SHIPPER["zip"])
        self.assertEqual(shipper["ShipperNumber"], "TEST01")


class ErrorReportingTests(UpsTestCase):
    """Every UPS failure should show UPS's own code and message."""

    def test_ups_errors_are_unpacked(self):
        payload = {"response": {"errors": [
            {"code": "250002", "message": "Invalid Authentication Information."}]}}
        self.assertEqual(ups.explain_error(FakeResponse(payload, 401)),
                         "[250002] Invalid Authentication Information.")

    def test_non_json_body_falls_back_to_text(self):
        class Plain:
            status_code = 500
            text = "gateway exploded"

            def json(self):
                raise ValueError

        self.assertEqual(ups.explain_error(Plain()), "gateway exploded")


if __name__ == "__main__":
    unittest.main()
