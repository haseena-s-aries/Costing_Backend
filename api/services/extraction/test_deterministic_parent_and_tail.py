from django.test import SimpleTestCase

from .deterministic_parser import DeterministicSpreadsheetParser


class DeterministicParentAndTailTests(SimpleTestCase):
    def test_section_heading_containing_type_is_not_treated_as_product(self):
        parser = DeterministicSpreadsheetParser()
        columns = {'product_name': 1, 'quantity': 5, 'uom': None, 'unit_price': 6, 'line_total': 7}
        heading = ['MTO FOR POWER CABLE LUGS (COPPER)-INSULATED FERRULE TYPE', '', '', '', '', '', '', '']
        self.assertEqual(parser._detect_section_heading(heading, columns, []), heading[0])

    def test_tail_rows_without_serial_numbers_are_retained(self):
        parser = DeterministicSpreadsheetParser()
        columns = {'product_name': 1, 'quantity': 5, 'uom': None, 'unit_price': 6, 'line_total': 7}
        row = ['', '110 kW - 600x500x250 Busbar', '', '', '', '3', '150', '450']
        self.assertIsNone(parser._detect_section_heading(row, columns, []))
        self.assertTrue(parser._is_nonzero_number(row[6]))
