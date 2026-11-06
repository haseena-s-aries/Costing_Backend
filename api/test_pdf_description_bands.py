from django.test import SimpleTestCase
from api.services.extraction.test_pdf_commercial_geometry import pdf_bytes, text as pdf_text
from api.services.gemini_extraction_service import _recover_pdf_full_description_cells


class DescriptionBandsTests(SimpleTestCase):
    def fixture(self):
        commands = []
        for x in [20, 65, 390, 450, 530, 580]:
            commands.append(f'{x} 300 m {x} 550 l S')
        for y in [300, 520, 550]:
            commands.append(f'20 {y} m 580 {y} l S')
        for x, text in [(25, 'No'), (70, 'Description'), (395, 'Qty'), (455, 'Price'), (535, 'Total')]:
            commands.append(pdf_text(x, 700-530, text))
        for y, serial, name, price in [(490, '1', 'Relay alpha', '12'), (420, '2', 'Gland beta', '15'), (350, '3', 'Cable gamma', '21')]:
            for x, text in [(25, serial), (70, name), (395, '2'), (455, price), (535, str(int(price)*2))]:
                commands.append(pdf_text(x, 700-y, text))
        commands.append(pdf_text(70, 700-475, 'with mounting base'))
        return pdf_bytes([commands])

    def test_unruled_rows_do_not_become_one_description(self):
        rows = [dict(row_type='PRODUCT', product_name=name, quantity='2', unit_price=price,
                     line_total=str(int(price)*2), page_number=1)
                for name, price in [('Relay alpha', '12'), ('Gland beta', '15'), ('Cable gamma', '21')]]
        result = _recover_pdf_full_description_cells(self.fixture(), {'items': rows})
        self.assertEqual([r['product_name'] for r in result['items']],
                         ['Relay alpha with mounting base', 'Gland beta', 'Cable gamma'])
        self.assertEqual([r['unit_price'] for r in result['items']], ['12', '15', '21'])
        self.assertEqual(len(result['items']), 3)

    def test_does_not_match_another_page(self):
        result = _recover_pdf_full_description_cells(self.fixture(), {'items': [
            dict(row_type='PRODUCT', product_name='Relay alpha', page_number=2)]})
        self.assertEqual(result['items'][0]['product_name'], 'Relay alpha')
