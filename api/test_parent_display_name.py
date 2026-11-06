from django.test import SimpleTestCase

from api.services.import_service import ImportService


class ParentDisplayNameTests(SimpleTestCase):
    def test_parent_is_added_when_existing_display_is_only_product_name(self):
        service = ImportService()
        mapped = service._build_display_name({
            'parent_category': 'MTO FOR TERMINAL BLOCK',
            'product_name': 'Terminal end cover',
            'display_name': 'Terminal end cover',
        })
        self.assertEqual(mapped['display_name'], 'MTO FOR TERMINAL BLOCK | Terminal end cover')

    def test_existing_richer_display_is_preserved(self):
        service = ImportService()
        mapped = service._build_display_name({
            'parent_category': 'MTO FOR TERMINAL BLOCK',
            'product_name': 'Terminal end cover',
            'display_name': 'MTO FOR TERMINAL BLOCK | Terminal end cover | Material: PVC',
        })
        self.assertEqual(mapped['display_name'], 'MTO FOR TERMINAL BLOCK | Terminal end cover | Material: PVC')
