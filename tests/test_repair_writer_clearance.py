"""Automatic repairs cannot inherit a human's accepted unknown-writer risk."""

from controller.adapter.fake import FakeRuntimeAdapter, FakeStep
from controller.approval import Approvals
from controller.attempts import events as ev
from controller.recovery import Recovery
from controller.recovery.events import Finding
from tests.test_approval import yes
from tests.test_repair import RepairServiceCase, run
from tests.test_service import KEY, SHA_A


class RepairWriterClearanceTests(RepairServiceCase):
    def unknown_writer(self):
        self.adapter = FakeRuntimeAdapter([FakeStep.lost_response(), FakeStep.launch()])
        self.start()
        operator = Recovery(
            self.store,
            Approvals(self.store, KEY, confirm=yes, os_user="rolando"),
            self.github,
            confirm=yes,
        )
        operator.record_launch_finding(
            run(self.a1), Finding.NOT_FOUND, (), "run list empty", self.now
        )
        operator.clear(
            self.a1, ev.ClearingBasis.UNRESOLVED_ACCEPTED, self.now, note="accept uncertainty"
        )
        self.review_fails(self.a1, 7, SHA_A)
        return operator

    def test_accepted_unknown_writer_does_not_start_automatic_repair(self):
        self.unknown_writer()
        self.run_rounds(3)
        self.assertEqual(len(self.adapter.requests), 1)
        self.assertEqual(self.repair_records(), [])
        self.assertTrue(any("earlier worker" in text for text in self.texts()))

    def append_repair_permission(self):
        from controller.repair.findings import as_data
        from tests.test_repair import finding

        item = next(iter(self.view().items.values()))
        grant = self.authorizer.use_repair_allowance(
            item.authorization(),
            self.contract(),
            self.a2,
            7,
            SHA_A,
            "AC1 failed",
            as_data([finding(1)]),
        )
        with self.store.writer_lock():
            self.store.append(grant)

    def test_a_preexisting_repair_permission_cannot_bypass_the_launch_check(self):
        self.unknown_writer()
        self.append_repair_permission()
        _, _, blocks = self.dispatcher.launcher.check(self.contract(), self.now)
        self.assertTrue(blocks, "accepted uncertainty must block automatic repair dispatch")
        self.run_rounds(2)
        self.assertEqual(len(self.adapter.requests), 1)

    def test_an_explicit_manual_repair_still_allows_the_accepted_exception(self):
        self.unknown_writer()
        self.append_repair_permission()
        Approvals(self.store, KEY, confirm=yes, os_user="rolando").authorize_repair(
            self.contract(), 2, "Proceed by my explicit decision", self.now
        )
        self.tick(minutes=6)
        self.assertEqual(len(self.adapter.requests), 2)

    def test_later_verified_access_removal_releases_the_automatic_repair(self):
        operator = self.unknown_writer()
        self.github.push_allowed = False
        operator.clear(
            self.a1, ev.ClearingBasis.WRITE_ACCESS_REMOVED, self.now, note="removed and checked"
        )
        self.tick(minutes=6)
        self.assertEqual(len(self.adapter.requests), 2)

    def other_unknown_writer(self):
        from controller import contract as contracts
        from controller.interfaces import TaskId
        from tests.test_attempts import LOST

        task = TaskId("another-task")
        fire = self.gate.reserve(task, contracts.digest(self.contract()), self.now)
        self.gate.record_launch(fire, LOST, self.now)
        operator = Recovery(
            self.store,
            Approvals(self.store, KEY, confirm=yes, os_user="rolando"),
            self.github,
            confirm=yes,
        )
        operator.record_launch_finding(fire, Finding.NOT_FOUND, (), "run list empty", self.now)
        operator.clear(
            fire.attempt, ev.ClearingBasis.UNRESOLVED_ACCEPTED, self.now, note="accept uncertainty"
        )

    def test_unknown_writer_on_another_task_also_blocks_automatic_repair(self):
        self.start()
        self.review_fails(self.a1, 7, SHA_A)
        self.clear(self.a1)
        self.other_unknown_writer()
        self.run_rounds(2)
        self.assertEqual(len(self.adapter.requests), 1)
        self.assertEqual(self.repair_records(), [])
        self.assertTrue(any("another-task-a1" in text for text in self.texts()))

    def test_launch_rechecks_writer_evidence_inside_reservation(self):
        from controller.dispatch import Refused

        self.start()
        self.review_fails(self.a1, 7, SHA_A)
        self.clear(self.a1)
        self.append_repair_permission()
        _, _, blocks = self.dispatcher.launcher.check(self.contract(), self.now)
        self.assertEqual(blocks, ())

        def prepare(run_id, digest):
            self.other_unknown_writer()
            return lambda: self.fail("runtime must never be contacted")

        with self.assertRaises(Refused) as cm:
            self.dispatcher.launcher.fire(self.contract(), prepare)
        self.assertIn("repair-writer-not-cleared", [b.code for b in cm.exception.blocks])
        self.assertFalse(
            any(
                e.event.kind == ev.ATTEMPT_RESERVED and e.event.attempt == self.a2
                for e in self.store.events()
            )
        )
