import unittest

from fraudscan.brand import brand_key, open_brand
from fraudscan.rules.childcare import shared_contact_multiple_providers
from fraudscan.sources.base import Entity


class TestBrandKey(unittest.TestCase):
    def test_brand_root_extraction(self):
        self.assertEqual(brand_key("MARTHA & MARY VINLAND SCHOOL AGE PROGRAM"),
                         "MARTHA MARY")
        self.assertEqual(brand_key("MARTHA AND MARY SUQUAMISH SCHOOL AGE PROGRAM"),
                         "MARTHA MARY")
        # site numerals are not brand: CHILDS TIME IX == CHILDS TIME XI
        self.assertEqual(brand_key("CHILDS TIME IX"), brand_key("CHILDS TIME XI"))
        # all-generic names have no brand
        self.assertEqual(brand_key("EARLY LEARNING CENTER"), "")

    def test_gateway_prefix_folding(self):
        # fleet regression: 'GATEWAY EXTENDED' must fold into 'GATEWAY' when the bare
        # key occurs, else the open-brand discount never fires for this church chain
        trio = ["GATEWAY EXTENDED DAY PROGRAM", "GATEWAY LEARNING CENTER",
                "GATEWAY LEARNING CENTER 2"]
        self.assertEqual(open_brand(trio), "GATEWAY")

    def test_open_brand_detection(self):
        sites = ["MARTHA & MARY VINLAND SCHOOL AGE PROGRAM",
                 "MARTHA & MARY POULSBO SCHOOL AGE PROGRAM",
                 "MARTHA AND MARY CHILDRENS SERVICES"]
        self.assertEqual(open_brand(sites), "MARTHA MARY")
        # differently-branded co-located orgs -> NOT an open brand
        mixed = ["FRENCH AMERICAN SCHOOL OF PUGET SOUND", "SEATTLE AMISTAD SCHOOL",
                 "HEARING SPEECH AND DEAFNESS CENTER"]
        self.assertIsNone(open_brand(mixed))


class TestVenueHosting(unittest.TestCase):
    def test_venue_detection(self):
        from fraudscan.brand import is_venue
        self.assertTrue(is_venue("RAINIER BEACH COMMUNITY CENTER"))
        self.assertTrue(is_venue("ORCA K-8 SCHOOL"))
        self.assertTrue(is_venue("ST LUKE'S CHURCH"))
        self.assertFalse(is_venue("SEED OF LIFE CENTER FOR EARLY LEARNING"))

    def test_host_venue_colocation_not_flagged(self):
        from fraudscan.rules.childcare import address_shared_multiple_providers
        ents = [Entity(source="childcare", source_id="1",
                       name="SEED OF LIFE CENTER FOR EL AND PS AT RBCC",
                       address="8825 RAINIER AVE S", city="SEATTLE", raw={}),
                Entity(source="childcare", source_id="2",
                       name="RAINIER BEACH COMMUNITY CENTER",
                       address="8825 Rainier Ave S", city="Seattle", raw={})]
        flags = address_shared_multiple_providers(ents, {})
        self.assertEqual(flags, [])   # program + its host venue = hosting, not shells

    def test_two_unrelated_brands_still_flagged(self):
        from fraudscan.rules.childcare import address_shared_multiple_providers
        ents = [Entity(source="childcare", source_id="1",
                       name="SUNSHINE KIDS ACADEMY",
                       address="100 MAIN ST", city="SEATTLE", raw={}),
                Entity(source="childcare", source_id="2",
                       name="BLUE HERON LEARNING CENTER",
                       address="100 Main St", city="Seattle", raw={})]
        flags = address_shared_multiple_providers(ents, {})
        self.assertEqual(len(flags), 2)
        self.assertEqual(flags[0].severity, 14)   # full severity, cross-brand


class TestBrandDiscount(unittest.TestCase):
    def _ent(self, i, name, email="shared@chain.org"):
        return Entity(source="childcare", source_id=str(i), name=name,
                      raw={"primarycontactemail": email})

    def test_same_brand_cluster_discounted(self):
        ents = [self._ent(1, "MARTHA & MARY VINLAND SCHOOL AGE PROGRAM"),
                self._ent(2, "MARTHA & MARY POULSBO SCHOOL AGE PROGRAM"),
                self._ent(3, "MARTHA & MARY COUGAR VALLEY SCHOOL AGE PROGRAM")]
        flags = shared_contact_multiple_providers(ents, {"severity": 22})
        self.assertTrue(flags)
        self.assertTrue(all(f.severity < 10 for f in flags))
        self.assertIn("open brand", flags[0].explanation)
        self.assertEqual(flags[0].evidence.get("open_brand"), "MARTHA MARY")

    def test_cross_brand_cluster_full_severity(self):
        ents = [self._ent(1, "SUNSHINE KIDS ACADEMY"),
                self._ent(2, "RAINIER VALLEY MONTESSORI"),
                self._ent(3, "BLUE HERON LEARNING CENTER")]
        flags = shared_contact_multiple_providers(ents, {"severity": 22})
        self.assertTrue(flags)
        self.assertTrue(all(f.severity == 22 for f in flags))
        self.assertNotIn("open brand", flags[0].explanation)


if __name__ == "__main__":
    unittest.main()
