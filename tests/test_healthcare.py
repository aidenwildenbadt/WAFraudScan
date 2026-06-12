"""Healthcare sanction-screening rule tests."""
import unittest

from fraudscan.rules import healthcare as hc
from fraudscan.sources.base import Entity


def cred(uid, status, actiontaken="No", ctype="Registered Nurse License"):
    return Entity(source="healthcare", source_id=uid, name=uid,
                  entity_type=ctype, status=status,
                  raw={"status": status, "actiontaken": actiontaken,
                       "credentialtype": ctype})


class TestHealthcareRules(unittest.TestCase):
    def test_revoked_or_suspended(self):
        ents = [cred("a", "Revoked"), cred("b", "Suspended"), cred("c", "Active")]
        flags = hc.credential_revoked_or_suspended(ents, {"severity": 30})
        self.assertEqual({f.entity_uid for f in flags},
                         {"healthcare:a", "healthcare:b"})

    def test_disciplinary_action(self):
        ents = [cred("a", "Active", actiontaken="Yes"),
                cred("b", "Active", actiontaken="No"),
                cred("c", "Active", actiontaken="Pending")]
        taken = hc.disciplinary_action_taken(ents, {"severity": 22})
        pending = hc.disciplinary_action_pending(ents, {"severity": 12})
        self.assertEqual({f.entity_uid for f in taken}, {"healthcare:a"})
        self.assertEqual({f.entity_uid for f in pending}, {"healthcare:c"})

    def test_active_with_conditions(self):
        ents = [cred("a", "Active With Conditions"), cred("b", "Active")]
        flags = hc.credential_active_with_conditions(ents, {"severity": 18})
        self.assertEqual({f.entity_uid for f in flags}, {"healthcare:a"})


if __name__ == "__main__":
    unittest.main()
