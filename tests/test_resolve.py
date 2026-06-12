"""Cross-source resolution tests."""
import unittest

from fraudscan.resolve import build_operators, keys_for
from fraudscan.sources.base import Entity


def cc(uid, name, contact=None):
    raw = {"primarycontactpersonname": contact} if contact else {}
    return Entity(source="childcare", source_id=uid, name=name, raw=raw)


def hc(uid, name, status="Suspended"):
    return Entity(source="healthcare", source_id=uid, name=name, status=status,
                  entity_type="Nursing Assistant", raw={"status": status})


def ct(uid, name):
    return Entity(source="contracts", source_id=uid, name=name,
                  entity_type="STATE CONTRACT")


class TestResolution(unittest.TestCase):
    def test_links_three_sources_into_one_operator(self):
        ents = [
            cc("a", "Dream Academy LLC", contact="Olga Rodriguez"),
            cc("b", "Dream Center LLC", contact="Olga Rodriguez"),  # person->a
            ct("c", "Dream Academy"),                               # org->a
            hc("h", "Olga Rodriguez"),                              # person->a,b
            cc("d", "Sunshine House", contact="Bob Jones"),         # unrelated
        ]
        ops, _ = build_operators(ents, scores_by_uid={}, max_key_members=8)
        self.assertEqual(len(ops), 1)
        op = ops[0]
        self.assertEqual(op["member_count"], 4)
        self.assertEqual(op["source_count"], 3)
        self.assertEqual(set(op["sources"]), {"childcare", "contracts", "healthcare"})

    def test_single_source_cluster_excluded(self):
        ents = [cc("a", "Tiny A", contact="Jane Doe"),
                cc("b", "Tiny B", contact="Jane Doe")]
        ops, _ = build_operators(ents, scores_by_uid={}, max_key_members=8)
        self.assertEqual(ops, [])

    def test_generic_name_blob_skipped(self):
        ents = [hc(f"h{i}", "John Smith") for i in range(10)]
        ents.append(cc("a", "Smith Family Care", contact="John Smith"))
        ops, skipped = build_operators(ents, scores_by_uid={}, max_key_members=8)
        self.assertEqual(ops, [])          # 11 share the person key -> skipped
        self.assertTrue(any("JOHN SMITH" in k.upper() for k, _ in skipped))

    def test_cross_bonus_raises_score(self):
        # full structural bonus requires a VERIFIED adverse member (non-healthcare,
        # i.e. not merely name-bridged) — here the CHILDCARE record itself is barred
        # (e.g. HCA-terminated daycare), so structure scores in full.
        ents = [cc("a", "Dream Academy LLC", contact="Olga Rodriguez"),
                hc("h", "Olga Rodriguez")]
        ops, _ = build_operators(
            ents, scores_by_uid={"healthcare:h": 50, "childcare:a": 50},
            max_key_members=8,
            member_facts={"childcare:a": {"sanctioned": True, "barred": True,
                                          "funds": 0.0},
                          "healthcare:h": {"sanctioned": True, "barred": False,
                                           "funds": 0.0}})
        # base 50 + multi-program 10 + childcare/healthcare bridge 25
        self.assertEqual(ops[0]["combined_score"], 85.0)

    def test_name_bridged_adverse_scales_structure(self):
        # the SAME cluster with only a healthcare (name-bridged) adverse member keeps
        # the bridge bonus but scales the structural 10 down to 3
        ents = [cc("a", "Dream Academy LLC", contact="Olga Rodriguez"),
                hc("h", "Olga Rodriguez")]
        ops, _ = build_operators(
            ents, scores_by_uid={"healthcare:h": 50}, max_key_members=8,
            member_facts={"healthcare:h": {"sanctioned": True, "barred": False,
                                           "funds": 0.0}})
        # F12+G6: $0 at stake + no identity-VERIFIED adverse member -> the namesake's
        # base is capped at 15 (not imported wholesale), keeping the cluster at/below
        # the 45 verify-identity ceiling (Sandoval/Askew pattern).
        self.assertLessEqual(ops[0]["combined_score"], 45.0)
        self.assertGreater(ops[0]["combined_score"], 0)
        self.assertEqual(ops[0]["name_bridged_adverse"], 1)

    def test_person_dedup_two_credentials_one_human(self):
        # one woman, two suspended credentials (same name + birthyear) -> 1 barred
        e1 = hc("c1", "Rosa Sandoval")
        e1.raw["birthyear"] = "1984"
        e2 = hc("c2", "Rosa Sandoval")
        e2.raw["birthyear"] = "1984"
        ents = [cc("a", "Family Circle Kent", contact="Rosa Sandoval"), e1, e2]
        mf = {"healthcare:c1": {"barred": True, "sanctioned": True, "funds": 0.0},
              "healthcare:c2": {"barred": True, "sanctioned": True, "funds": 0.0}}
        ops, _ = build_operators(ents, scores_by_uid={"healthcare:c1": 60,
                                                      "healthcare:c2": 60},
                                 max_key_members=8, member_facts=mf)
        self.assertEqual(ops[0]["barred_members"], 1)

    def test_name_only_adverse_keeps_institutional_suppression(self):
        # a name-only healthcare adverse member must NOT strip suppression from an
        # institutional childcare program (the Patricia Smith circularity)
        ents = [cc("a", "YMCA Early Learning", contact="Pat Quill"),
                hc("h", "Pat Quill")]
        mf = {"healthcare:h": {"barred": True, "sanctioned": True, "funds": 0.0}}
        ops, _ = build_operators(ents, scores_by_uid={"healthcare:h": 50},
                                 max_key_members=8, member_facts=mf)
        self.assertTrue(any("suppressed" in s for s in ops[0]["signals"]))
        self.assertTrue(any("PERSON-NAME only" in s for s in ops[0]["signals"]))

    def test_national_count_caps_common_name_bridge(self):
        ents = [cc("a", "Tiny Tots", contact="Patricia Smith"),
                hc("h", "Patricia Smith")]
        ops, _ = build_operators(ents, scores_by_uid={"healthcare:h": 50},
                                 max_key_members=8,
                                 national_count=lambda n: 175)
        self.assertTrue(any("COMMON NAME (175 providers" in s
                            for s in ops[0]["signals"]))

    def test_structure_bonus_scaled_without_adverse(self):
        ents = [cc("a", "Dream Academy LLC", contact="Olga Rodriguez"),
                hc("h", "Olga Rodriguez")]
        ops, _ = build_operators(ents, scores_by_uid={"healthcare:h": 50},
                                 max_key_members=8)
        # no adverse member -> structural 10 scales to 3; the healthcare-bridge 25
        # is exempt (its premise is the sanctioned credential): 50 + 3 + 25 = 78,
        # then the F12 zero-dollar cap routes it to identity verification at 45.
        self.assertEqual(ops[0]["combined_score"], 45.0)
        self.assertTrue(any("scored low until adverse" in s
                            for s in ops[0]["signals"]))


def cca(uid, name, address, city="OLYMPIA", zip_="98501"):
    return Entity(source="childcare", source_id=uid, name=name,
                  address=address, city=city, zip=zip_, raw={})


class TestFuzzyAndAddress(unittest.TestCase):
    def test_fuzzy_name_variant_links(self):
        ents = [cc("a", "Bright Future Learning Center"),
                cc("b", "Bright Futuer Learning Center")]   # transposed letters
        ops, _ = build_operators(ents, {}, max_key_members=8, fuzzy_threshold=0.9)
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["member_count"], 2)
        self.assertTrue(any("fuzzy" in s.lower() for s in ops[0]["signals"]))

    def test_unrelated_similar_token_not_linked(self):
        ents = [cc("a", "Sunshine Daycare"), cc("b", "Moonlight Daycare")]
        ops, _ = build_operators(ents, {}, max_key_members=8, fuzzy_threshold=0.9)
        self.assertEqual(ops, [])

    def test_shared_address_links_different_names(self):
        ents = [cca("a", "Happy Kids", "123 Main St"),
                cca("b", "Joyful Tots", "123 MAIN STREET")]   # abbrev variant
        ops, _ = build_operators(ents, {}, max_key_members=8)
        self.assertEqual(len(ops), 1)
        self.assertTrue(any("one address" in s for s in ops[0]["signals"]))

    def test_same_name_same_address_not_surfaced_as_colocated(self):
        # identical names at one address = one provider, not a multi-name operator
        ents = [cca("a", "Tiny Tots", "5 Oak Ave"),
                cca("b", "Tiny Tots", "5 Oak Ave")]
        ops, _ = build_operators(ents, {}, max_key_members=8)
        self.assertEqual(ops, [])


def ccg(uid, name, address, lat, lon):
    return Entity(source="childcare", source_id=uid, name=name, address=address,
                  city="OLYMPIA", zip="98501", lat=lat, lon=lon, raw={})


class TestGeoProximity(unittest.TestCase):
    def test_nearby_different_addresses_link(self):
        # ~33m apart, different names and street addresses
        ents = [ccg("a", "Happy Kids", "100 Main St", 47.6000, -122.3000),
                ccg("b", "Joyful Tots", "102 Main St", 47.6003, -122.3000)]
        ops, _ = build_operators(ents, {}, geo_radius_m=50)
        self.assertEqual(len(ops), 1)
        self.assertTrue(any("Geocode-proximate" in s for s in ops[0]["signals"]))

    def test_far_apart_not_linked(self):
        ents = [ccg("a", "Happy Kids", "100 Main St", 47.6000, -122.3000),
                ccg("c", "Distant Care", "5 Far Rd", 47.7000, -122.3000)]  # ~11km
        ops, _ = build_operators(ents, {}, geo_radius_m=50)
        self.assertEqual(ops, [])

    def test_zero_coords_ignored(self):
        ents = [ccg("a", "Happy Kids", "100 Main St", 0.0, 0.0),
                ccg("b", "Joyful Tots", "102 Main St", 0.0, 0.0)]
        ops, _ = build_operators(ents, {}, geo_radius_m=50)
        self.assertEqual(ops, [])


if __name__ == "__main__":
    unittest.main()
