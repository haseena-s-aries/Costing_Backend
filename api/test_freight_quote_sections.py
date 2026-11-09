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


CONFIRMATION_THREAD = """From: Buyer Person <buyer@buyer-group.example>
Sent: Thursday, 2 July 2026 4:38 PM
Subject: RE: Shipment of containers - JOB CONFIRMATION
Dear Agent,
Your below offer for shipment of 4 nos of 40FT containers are confirmed.
4X40 HQ Container - Sea freight + Local charges : 8950 USD / Per Container = 8950 USD x 4 Nos = USD 35,800.00/-
Net amount confirmed is : USD 35,800.00/-
Kind Regards
From Buyer Group, UAE
From: Agent Person <agent@exco.example>
Sent: Tuesday, 30 June 2026 12:38 PM
Subject: RE: Shipment of containers
Please find the sailing schedule.
Agent Person | Sales Executive | EXCO Marine Logistics - Dubai
"""


class PriceCalculationTests(SimpleTestCase):
    def test_unit_times_quantity_equals_total(self):
        from api.services.price_calculation import calculation
        calc = calculation('Freight : 8950 USD / Per Container = 8950 USD x 4 Nos = USD 35,800.00/-')
        self.assertEqual((calc['unit_price'], calc['quantity'], calc['uom'], calc['line_total']),
                         ('8950', '4', 'Container', '35800.00'))
        # Arithmetic that does not hold is not a calculation.
        self.assertIsNone(calculation('Item 100 USD x 3 Nos = USD 400'))
        self.assertIsNone(calculation('Size 4X40 HQ container'))

    def test_confirmed_rate_is_one_row_for_the_other_party(self):
        rows = freight_offer_rows(CONFIRMATION_THREAD)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row['unit_price'], row['quantity'], row['line_total'], row['currency']),
                         ('8950', '4', '35800.00', 'USD'))
        self.assertEqual(row['product_name'], '4X40 HQ Container - Sea freight + Local charges')
        self.assertEqual(row['supplier_name'], 'EXCO Marine Logistics')
        self.assertEqual(row['purchase_date'], '2026-07-02')

    def test_total_rows_collapse_into_unit_price_row(self):
        from api.services.freight_quote_sections import collapse_calculated_totals
        items = [dict(product_name='Valve DN50', unit_price='120', quantity='5', purchase_date='2026-07-02', currency='USD'),
                 dict(product_name='Valve DN50 x 5 Nos =', unit_price='600', quantity=1, purchase_date='2026-07-02', currency='USD'),
                 dict(product_name='Gasket', unit_price='600', quantity=1, purchase_date='2026-07-03', currency='USD')]
        result = collapse_calculated_totals({'items': items})
        self.assertEqual([i['product_name'] for i in result['items']], ['Valve DN50', 'Gasket'])
        self.assertEqual(result['items'][0]['line_total'], '600')


AIR_THREAD = """From: Agent One / Example Freight <agent1@examplefreight.example>
Sent: Monday, 19 October 2026 8:29 pm
To: Buyer <buyer@buyer.example>
Subject: RE: RFQ air freight
Please find the below best rate for your enquiry,
AIR IMPORT ESTIMATE
TERM
EXW
AIRLINE
EY
AOL
EHU
AOD
AUH
CHARGEABLE WEIGHT
4380 KGS
ROUTE: EHU-AUH
TT: 1-2 days.
ALL IN CHARGES : AED 90800/- (DOOR TO DOOR)
AIR IMPORT ESTIMATE
TERM
EXW
AIRLINE
EY
AOL
PVG
AOD
AUH
ROUTE: PVG-AUH
ALL IN CHARGES : AED 93370/- (DOOR TO DOOR)
Regards
Agent One
Example Freight LLC
From: Agent One / Example Freight
Sent: Thursday, October 22, 2026 2:01 PM
To: Buyer <buyer@buyer.example>
Subject: RE: RFQ air freight
AIR IMPORT ESTIMATE
AOL
PVG
AOD
AUH
ALL IN CHARGES : AED 94370/- (DOOR TO DOOR)
"""


class AirEstimateTests(SimpleTestCase):
    def test_each_estimate_keeps_its_own_route_and_supplier(self):
        rows = freight_offer_rows(AIR_THREAD)
        self.assertEqual([(r['purchase_date'], r['product_name'], r['unit_price']) for r in rows], [
            ('2026-10-19', 'Air Freight All in Charges (Door to Door) - EHU to AUH', '90800'),
            ('2026-10-19', 'Air Freight All in Charges (Door to Door) - PVG to AUH', '93370'),
            # Forwarded copy without an address inherits the sender's company.
            ('2026-10-22', 'Air Freight All in Charges (Door to Door) - PVG to AUH', '94370'),
        ])
        self.assertEqual({r['supplier_name'] for r in rows}, {'Example Freight LLC'})
        self.assertIn({'label': 'Airline', 'value': 'EY'}, rows[0]['extra_attributes'])

    def test_same_line_rate_rows_are_replaced_by_the_estimate(self):
        from api.services.email_rate_evidence import restore_rates
        result = restore_rates({'items': []}, AIR_THREAD)
        self.assertEqual({str(r['extra_attributes']) for r in result['items']}, {str([{'label': 'Condition / inclusions', 'value': 'DOOR TO DOOR'}])})
        result = restore_freight_offers(result, AIR_THREAD)
        self.assertEqual(sorted(r['unit_price'] for r in result['items']), ['90800', '93370', '94370'])


BREAKBULK = """From: Agent <agent@exampleshipping.example>
Sent: Thursday, 27 August 2026 3:48 PM
To: Buyer
Subject: RE: RFQ sea freight shipment
Thanks for your enquiry, please find below our break bulk charges.
Door to Port – USD 23,940/- Under deck , Non-stackable – HVAC UNITS ONLY
Wooden Packing – USD 9000/-
Clearance and delivery from Jebel Ali port.
DO – AED 575/-
TRANSPORTATION – AED 2700/- PER TRUCK (3 TRUCK required as per dimension)
POL:Tianjin or nearby
POD:Jebel Ali
Thanks & Best Regards
Agent
Example Shipping LLC
From: Agent Two <agent2@examplefreight.example>
Sent: Wednesday, 21 October 2026 11:30 AM
To: Buyer
Subject: RE: PICK UP OF THE MATERIALS FROM DMC TO MBK JETTY
Please find the charges as follows:
POL: DXB – AUH
Transporta(cid:415)on Charges: AED 1350/- ( 7 TON )
Regards
Agent Two
Example Freight LLC
"""


class RouteContextTests(SimpleTestCase):
    def test_route_and_location_context_make_rows_meaningful(self):
        from api.services.freight_quote_sections import _restore_ligatures
        rows = {r['product_name']: r for r in freight_offer_rows(_restore_ligatures(BREAKBULK))}
        self.assertEqual(set(rows), {
            'Door to Port - Tianjin to Jebel Ali',
            'DO - Jebel Ali port',
            'Transportation - from Jebel Ali port',
            'Transportation Charges (7 TON) - DXB to AUH',
        })
        self.assertIn({'label': 'Condition', 'value': 'Under deck , Non-stackable – HVAC UNITS ONLY'},
                      rows['Door to Port - Tianjin to Jebel Ali']['extra_attributes'])
        truck = rows['Transportation - from Jebel Ali port']
        self.assertEqual((truck['quantity'], truck['uom'], truck['row_type']), ('3', 'truck', 'SERVICE'))
        self.assertEqual(rows['DO - Jebel Ali port']['row_type'], 'CHARGE')

    def test_terms_prose_is_not_a_location_heading(self):
        body = ("From: Agent <a@x.example>\nSent: 1 May 2026\nSubject: RE: quote\nSea freight : USD 900/- (DOOR TO DOOR)\n"
                "Any additional costs arising from inspections will be charged.\nINSPECTION – AED 170/-\nRegards\nX Shipping LLC")
        self.assertFalse(any('inspections' in r['product_name'] for r in freight_offer_rows(body)))


COLLECTIONS = """From: Person - Example Kingston <person@examplekingston.example>
Sent: Thursday, 18 December 2025 2:10 PM
To: Buyer
Subject: RE: DOOR TO DOOR DELIVERY FROM CHINA TO UAE
Please find below current sea freight charges.
Collection 1
POL: SHANGHAI
POD: AUH port ( AUH delivery )
Ocean Freight FCL charges all in for 7 x 40 ft OT IG (Door to Door) : AED 15,550 / per Cntr
Carrier : MSC
Collection 2
POL: QINGDAO
POD: AUH port ( AUH delivery )
Ocean Freight FCL charges all in for 4 x 40 ft HC (Door to Door) : AED 15,573 / per Cntr
Carrier : HMM
Standby above free time for trailer @ AED 75/per hour
PERSON
Pricing Coordinator
"""


class CollectionSectionTests(SimpleTestCase):
    def test_collections_keep_route_carrier_count_and_container_type(self):
        rows = freight_offer_rows(COLLECTIONS)
        self.assertEqual([(r['product_name'], r['quantity'], r['uom'], r['unit_price'], r['supplier_name']) for r in rows], [
            ('Ocean Freight FCL charges all in 40 ft OT IG (Door to Door) - Shanghai to AUH port', '7', 'container', '15550', 'Example Kingston'),
            ('Ocean Freight FCL charges all in 40 ft HC (Door to Door) - Qingdao to AUH port', '4', 'container', '15573', 'Example Kingston'),
        ])
        self.assertIn({'label': 'Carrier', 'value': 'HMM'}, rows[1]['extra_attributes'])
        self.assertIn({'label': 'Container size', 'value': '40OT'}, rows[0]['extra_attributes'])
        self.assertNotIn('Condition', str(rows[0]['extra_attributes']))
