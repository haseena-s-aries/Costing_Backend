from django.test import SimpleTestCase, override_settings

from api.services.internal_quote_prices import internal_quote_rows, restore_internal_quotes

THREAD = """From: Client Manager <ops@client-ship.example>
Sent: Wednesday, 14 October 2026 4:30 PM
To: Our Person
Subject: RE: SEA STAR - Mooring fittings Modification - Spare Parts
Pls proceed to arrange.
Best Regards,
Client Manager
CLIENT SHIPMANAGEMENT PTE LTD
From: Our Person <sales@ourco.example>
Sent: Wednesday, 14 October 2026 2:45 pm
To: Client Manager <ops@client-ship.example>
Subject: SEA STAR - Mooring fittings Modification - Spare Parts
Dear Sir,
Kindly note that the local transportation using 2 trailers, including Ferry requirement is USD 3,450.00/-
Total order value for calculating the insurance : USD 80,000.00
Awaiting your valuable confirmation.
Kind Regards
Our Person
From: Our Person <sales@ourco.example>
Sent: Monday, 5 October 2026 9:00 AM
To: Client Manager <ops@client-ship.example>
Subject: RE: SEA STAR - Mooring fittings Modification - Spare Parts
As discussed, the transportation charge is USD 3,450.00
Kind Regards
Our Person
From: Our Person <sales@ourco.example>
Sent: Saturday, 3 October 2026 10:28 AM
To: Client Manager <ops@client-ship.example>
Subject: RE: OCEAN MOON Mooring fittings Modification - Spare Parts
The transportation charge for direct delivery to the shipyard is USD 3,450.00.
Packing crates : USD 150.00 / crate x 2 Nos = USD 300.00
Kind Regards
Our Person
"""


@override_settings(INTERNAL_COMPANY_DOMAINS=['ourco.example'])
class InternalQuotePriceTests(SimpleTestCase):
    def test_rows_per_vessel_dated_at_first_quote_with_metadata(self):
        rows = internal_quote_rows(THREAD)
        summary = [(r['purchase_date'], r['product_name'], r['unit_price'], r['quantity'], r['vessel']) for r in rows]
        self.assertEqual(summary, [
            # Same price re-sent for SEA STAR on 14 Oct: one row, first quoted 5 Oct.
            ('2026-10-05', 'Transportation charge - SEA STAR', '3450.00', None, 'SEA STAR'),
            # Same price, different vessel: kept separately.
            ('2026-10-03', 'Transportation charge - OCEAN MOON (direct delivery to the shipyard)', '3450.00', None, 'OCEAN MOON'),
            # Quantity only where it is printed (and verified by its total).
            ('2026-10-03', 'Packing crates - OCEAN MOON', '150.00', '2', 'OCEAN MOON'),
        ])
        self.assertEqual({r['supplier_name'] for r in rows}, {None})  # test thread signs without a company
        self.assertEqual({r['buyer_name'] for r in rows}, {'CLIENT SHIPMANAGEMENT PTE LTD'})
        self.assertIn('Vessel: SEA STAR; Email 2026-10-05', rows[0]['source_reference'])
        self.assertEqual(rows[0]['_source_email']['subject'], 'SEA STAR - Mooring fittings Modification - Spare Parts')

    def test_model_duplicates_are_replaced(self):
        model = [dict(product_name='The local transportation using 2 trailers, including Ferry requirement is',
                      unit_price='3450.00', quantity=1, currency='USD', purchase_date='2026-10-14',
                      supplier_name='CLIENT SHIPMANAGEMENT PTE LTD'),
                 dict(product_name='Unrelated item', unit_price='12', currency='USD', purchase_date='2026-10-14')]
        result = restore_internal_quotes({'items': model, 'supplier_name': 'CLIENT SHIPMANAGEMENT PTE LTD'}, THREAD)
        self.assertEqual(len(result['items']), 4)
        self.assertEqual(result['items'][-1]['product_name'], 'Unrelated item')
        self.assertIsNone(result['supplier_name'])

    def test_supplier_threads_and_confirmations_are_not_internal_quotes(self):
        supplier_thread = THREAD.replace('sales@ourco.example', 'sales@vendor.example')
        self.assertEqual(internal_quote_rows(supplier_thread), [])
        confirmation = THREAD.replace('Dear Sir,', 'Your below offer for shipment are confirmed.')
        self.assertNotIn('2026-10-14', {r['purchase_date'] for r in internal_quote_rows(confirmation)})

    def test_transport_rows_are_services_and_default_domains_apply(self):
        rows = internal_quote_rows(THREAD)
        self.assertEqual([r['row_type'] for r in rows], ['SERVICE', 'SERVICE', 'PRODUCT'])


class DefaultInternalDomainTests(SimpleTestCase):
    @override_settings(INTERNAL_COMPANY_DOMAINS=None)
    def test_missing_setting_falls_back_to_built_in_domains(self):
        from api.services.internal_quote_prices import _internal
        self.assertTrue(_internal('arteqworld.com'))
        self.assertFalse(_internal('nykgroup.com'))


@override_settings(INTERNAL_COMPANY_DOMAINS=['ourco.example'])
class InternalQuoteRoleTests(SimpleTestCase):
    def test_our_company_is_supplier_of_our_quote(self):
        thread = THREAD.replace('Kind Regards\nOur Person\nFrom: Our Person <sales@ourco.example>\nSent: Monday',
                                'Kind Regards\nFrom Our Group, UAE\nOur Person\nFrom: Our Person <sales@ourco.example>\nSent: Monday')
        rows = internal_quote_rows(thread)
        self.assertIn('Our Group, UAE', {r['supplier_name'] for r in rows})

    def test_messages_to_a_quoting_supplier_are_negotiations(self):
        thread = ("From: Vendor <rates@vendor.example>\nSent: 22 October 2026\nTo: Our Person <sales@ourco.example>\n"
                  "Subject: RE: RFQ\nAIR FREIGHT : AED 93370/- (DOOR TO DOOR)\nRegards\nVendor LLC\n"
                  "From: Our Person <sales@ourco.example>\nSent: 21 October 2026\nTo: Vendor <rates@vendor.example>\n"
                  "Subject: RE: RFQ\nCan we reduce the 2nd option ALL IN CHARGES : AED 93370/- to 91000.00 AED\n"
                  "Kind Regards\nFrom Our Group, UAE\n")
        self.assertEqual(internal_quote_rows(thread), [])
