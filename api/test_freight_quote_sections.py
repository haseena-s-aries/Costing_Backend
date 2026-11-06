from django.test import SimpleTestCase

from api.services.freight_quote_sections import freight_offer_rows, restore_freight_offers

THREAD = """From: Buyer Person <buyer@buyer-group.example>
Sent: Friday, 10 July 2026 10:14 AM
Subject: RE: Shipment of containers
Kindly revalidate the price.
Kind Regards
From Buyer Group, UAE
From: Seller Agent <agent@exampleshipping.example>
Sent: Monday, 29 June 2026 11:03 AM
Subject: RE: Shipment of containers
Please find below Door-to-port charges.
SEA - FCL
Option -1
DOOR TO PORT CHARGES :USD 9350/- 40'
POL : Qingdao
POD : Abu Dhabi
T/T : 27-35days approx.
Option -2
DOOR TO PORT CHARGES : USD 7680/- 20'
POL : Qingdao
POD : Abu Dhabi
Notes :
Rates excludes Insurance Charges
Thanks & Best Regards
Seller Agent | Branch Manager
agent@exampleshipping.example
From: Buyer Person <buyer@buyer-group.example>
Sent: Monday, June 22, 2026 9:08 AM
Subject: RE: Shipment of containers
CAUTION: This email is NOT originated from Example Shipping LLC.
Can you revalidate the price?
From: Seller Agent
Sent: Wednesday, April 22, 2026 10:40 AM
Subject: RE: Shipment of containers
Option -1
DOOR TO PORT CHARGES :USD 9350/-
POL : Qingdao
POD : Abu Dhabi
Option -2
DOOR TO PORT CHARGES : USD 8326/-
POL : Qingdao
POD : Abu Dhabi
Thanks & Best Regards
agent@exampleshipping.example
"""


class FreightQuoteSectionTests(SimpleTestCase):
    def test_descriptions_use_route_size_or_stated_option(self):
        rows = freight_offer_rows(THREAD)
        self.assertEqual([(r['purchase_date'], r['product_name'], r['unit_price']) for r in rows], [
            ('2026-06-29', 'Door to Port Charges - Qingdao to Abu Dhabi - 40FT', '9350'),
            ('2026-06-29', 'Door to Port Charges - Qingdao to Abu Dhabi - 20FT', '7680'),
            # Same price on an older quote stays a separate historical row;
            # no size is printed, so the stated option is used, not a guess.
            ('2026-04-22', 'Door to Port Charges - Qingdao to Abu Dhabi - Option 1', '9350'),
            ('2026-04-22', 'Door to Port Charges - Qingdao to Abu Dhabi - Option 2', '8326'),
        ])
        # The seller is the printed company that spells its email domain,
        # never the buyer's signature; supplier/date/price/currency separate.
        self.assertEqual({r['supplier_name'] for r in rows}, {'Example Shipping LLC'})
        self.assertEqual({r['currency'] for r in rows}, {'USD'})
        self.assertIn({'label': 'Transit time', 'value': '27-35days approx.'}, rows[0]['extra_attributes'])
        self.assertIsNone(rows[2]['uom'])

    def test_model_rows_with_wrong_date_or_buyer_supplier_are_replaced(self):
        model = [dict(product_name='DOOR TO PORT CHARGES', unit_price=price, currency='USD',
                      purchase_date=date, supplier_name=supplier, quantity=1, row_type='SERVICE')
                 for price, date, supplier in [(7680, '2026-06-22', 'Seller'), (8326, '2026-04-15', 'Seller'),
                                               (9350, '2026-07-10', 'Buyer Group, UAE')]]
        model.append(dict(product_name='Unrelated cable', unit_price=12, currency='USD',
                          purchase_date='2026-07-10', row_type='PRODUCT'))
        result = restore_freight_offers({'items': model, 'document_date': '2026-07-10'}, THREAD)
        self.assertEqual(len(result['items']), 5)
        self.assertEqual(result['items'][-1]['product_name'], 'Unrelated cable')
        self.assertEqual(result['supplier_name'], 'Example Shipping LLC')
        self.assertEqual(result['document_date'], '2026-06-29')

    def test_non_email_and_non_freight_text_is_untouched(self):
        self.assertEqual(freight_offer_rows('Quotation\nDOOR TO PORT CHARGES : USD 100/- 20\''), [])
        body = ('From: Seller <s@seller.example>\nSent: 1 May 2026\nINSURANCE PREMIUM : AED 3000\n'
                'STORAGE CHARGE : USD 200/TEU\nRegards\nSeller Trading LLC')
        self.assertEqual(freight_offer_rows(body), [])
