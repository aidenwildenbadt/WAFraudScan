"""Tests for the misspelled-word-in-name rule."""
import unittest

from fraudscan.rules.naming import misspelled_word_in_name, _edits1
from fraudscan.sources.base import Entity


def mk(uid, name):
    return Entity(source="childcare", source_id=uid, name=name)


class TestMisspelling(unittest.TestCase):
    def test_flags_one_edit_misspelling(self):
        ents = [mk("a", "Bright Learing Center"),      # learing -> learning
                mk("b", "Sunshine Learning Center"),   # correct
                mk("c", "Tiny Acadmy"),                # acadmy -> academy
                mk("d", "School Age Progam"),          # progam -> program
                mk("e", "Frontline Transpot")]         # transpot -> transport
        flags = misspelled_word_in_name(ents, {})
        by = {f.entity_uid: f for f in flags}
        self.assertEqual(set(by), {"childcare:a", "childcare:c",
                                   "childcare:d", "childcare:e"})
        self.assertEqual(by["childcare:a"].evidence["misspellings"][0]["likely"],
                         "LEARNING")
        self.assertEqual(by["childcare:d"].evidence["misspellings"][0]["likely"],
                         "PROGRAM")

    def test_correct_words_not_flagged(self):
        ents = [mk("a", "Little Montessori Preschool"),
                mk("b", "Evergreen Childrens Academy")]
        self.assertEqual(misspelled_word_in_name(ents, {}), [])

    def test_short_and_proper_names_skipped(self):
        # short tokens (<5) and ordinary proper nouns shouldn't trip it
        ents = [mk("a", "ABC Kids"), mk("b", "Smith Daycare")]
        self.assertEqual(misspelled_word_in_name(ents, {}), [])

    def test_edits1_contains_target(self):
        self.assertIn("LEARNING", _edits1("LEARING"))   # one insertion
        self.assertIn("ACADEMY", _edits1("ACADMEY"))    # one transposition

    def test_expanded_vocabulary(self):
        ents = [mk("a", "Profesional Family Care"),     # professional (dropped s)
                mk("b", "Comunity Childrens Center"),    # community (dropped m)
                mk("c", "Acheivement Academy"),          # achievement (transposed)
                mk("d", "Independant Living Services")]   # independent (a for e)
        by = {f.entity_uid: f for f in misspelled_word_in_name(ents, {})}
        self.assertEqual(set(by), {"childcare:a", "childcare:b",
                                   "childcare:c", "childcare:d"})

    def test_two_edit_fallback_long_word(self):
        # "Acomodation" is two deletions from "Accommodation"
        ents = [mk("a", "Acomodation Health Services")]
        by = {f.entity_uid: f for f in misspelled_word_in_name(ents, {})}
        self.assertIn("childcare:a", by)
        self.assertEqual(by["childcare:a"].evidence["misspellings"][0]["likely"],
                         "ACCOMMODATION")


if __name__ == "__main__":
    unittest.main()
