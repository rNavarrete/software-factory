"""Every dispatch refusal is in the ledger (ENG-163, governance map G-A1).

The gate already recorded the refusals it decides. These cover the ones
raised before the gate: no approval, a declined prompt, a revoked approval,
an invalid contract, a wrong target. Nothing is ever sent in any of them.
"""

from datetime import timedelta

from controller import contract as contracts
from controller.attempts import events as ev
from tests.test_dispatch import NOW, Confirm, DispatchCase, example


class RefusalRecordTests(DispatchCase):
    def refusals(self):
        return [s.event for s in self.store.events() if s.event.kind == ev.DISPATCH_REFUSED]

    def codes(self, event):
        return {b["code"] for b in event.data["blocks"]}

    def test_declined_approval_is_recorded(self):
        self.confirm.answer = False
        self.assertIn("not-approved", self.refused())
        (r,) = self.refusals()
        self.assertIn("not-approved", self.codes(r))
        self.assertEqual(r.task, self.task)
        self.assertTrue(r.data["before_gate"])
        self.assert_nothing_sent()

    def test_revoked_approval_is_recorded(self):
        self.approve()
        saved, self.confirm = self.confirm, Confirm()
        self.approvals.revoke(self.task, self.digest, "changed my mind", NOW)
        self.confirm = saved
        self.assertIn("approval-revoked", self.refused())
        (r,) = self.refusals()
        self.assertIn("approval-revoked", self.codes(r))

    def test_expired_and_declined_is_recorded(self):
        self.approve(at=NOW - timedelta(days=4))
        self.confirm.answer = False
        self.refused()
        self.assertEqual(len(self.refusals()), 1)

    def test_modified_contract_declined_is_recorded(self):
        self.approve()
        changed = example(goal="Something else entirely.")
        self.assertNotEqual(contracts.digest(changed), self.digest)
        self.confirm.answer = False
        self.refused(changed)
        (r,) = self.refusals()
        self.assertIn("not-approved", self.codes(r))

    def test_invalid_contract_is_recorded_even_without_a_task(self):
        broken = example()
        del broken["base_commit"]
        self.assertEqual(self.refused(broken), {"contract-invalid"})
        no_task = example()
        del no_task["task_id"]
        self.refused(no_task)
        first, second = self.refusals()
        self.assertEqual(first.task, self.task)
        self.assertIsNone(second.task)

    def test_gate_refusals_are_not_recorded_twice(self):
        self.approve()
        self.key_side_effect = lambda: self.gate.hold("manual", "race", NOW)
        self.assertIn("hold", self.refused())
        (r,) = self.refusals()
        self.assertNotIn("before_gate", r.data)

    def test_a_refusal_that_cannot_be_recorded_still_reaches_rolando(self):
        self.confirm.answer = False

        def broken_lock():
            raise OSError("disk full")

        self.store.writer_lock = broken_lock
        codes = self.refused()
        self.assertIn("not-approved", codes)
        self.assertIn("not-recorded", codes)
