import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import idle_gate
import wakecodex as wake
import watch_job


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = wake.Store(self.root / "state")
        self.thread = str(uuid.uuid4())

    def tearDown(self):
        self.temp.cleanup()

    def register(self):
        return self.store.register(self.thread, str(self.root), "Report the event, then stop.")

    def lifecycle(self, kind="task_started", turn="turn-1"):
        p = self.root / "rollout.jsonl"
        if not p.exists():
            p.write_text(json.dumps(dict(type="session_meta", payload=dict(id=self.thread))) + "\n")
        with p.open("a") as f:
            f.write(
                json.dumps(dict(type="event_msg", payload=dict(type=kind, turn_id=turn))) + "\n"
            )
        return p

    def guard(self):
        p = self.lifecycle()
        db = self.root / "threads.sqlite"
        with closing(sqlite3.connect(db)) as c, c:
            c.execute("create table threads(id text,rollout_path text)")
            c.execute("insert into threads values(?,?)", (self.thread, str(p)))
        return dict(
            thread=self.thread, database=str(db), rollout=str(p), after_turn="turn-1", user_count=0
        )

    def test_all_terminal_states_and_duplicate_suppression(self):
        for status in wake.TERMINAL_EVENTS:
            w = self.register()
            self.assertTrue(self.store.complete(w["id"], dict(status=status)))
            self.assertFalse(self.store.complete(w["id"], dict(status="failed")))
            self.assertEqual(self.store.get(w["id"])["event"]["status"], status)

    def test_no_model_call_while_waiting(self):
        self.register()
        adapter = unittest.mock.Mock()
        self.assertFalse(wake.dispatch_once(self.store, adapter))
        adapter.assert_not_called()

    def test_restart_preserves_pending_event(self):
        w = self.register()
        self.store.complete(w["id"], dict(status="paused"))
        reopened = wake.Store(self.store.root)
        calls = []
        wake.dispatch_once(reopened, lambda w, r: calls.append(w) or "delivered")
        wake.dispatch_once(reopened, lambda w, r: calls.append(w) or "delivered")
        self.assertEqual(len(calls), 1)

    def test_active_turn_blocks_dispatch_without_cli(self):
        g = self.guard()
        w = self.register()
        (self.store.root / (w["id"] + ".guard.json")).write_text(json.dumps(g))
        self.store.complete(w["id"], dict(status="completed"))
        with patch("wakecodex.subprocess.run") as run:
            wake.dispatch_once(self.store, wake.CodexAdapter())
            run.assert_not_called()
        self.assertEqual(self.store.get(w["id"])["status"], "needs_attention")

    def test_new_input_and_new_turn_cancel_barrier(self):
        g = self.guard()
        self.lifecycle("task_complete")
        self.assertEqual(idle_gate.guard_state(g), "idle")
        self.lifecycle("user_message")
        self.assertEqual(idle_gate.guard_state(g), "new_user_input")
        g["user_count"] = 1
        self.lifecycle("task_started", "turn-2")
        self.assertEqual(idle_gate.guard_state(g), "changed_turn")

    def test_kernel_idle_barrier(self):
        g = self.guard()
        t = threading.Thread(target=lambda: (time.sleep(0.04), self.lifecycle("task_complete")))
        t.start()
        try:
            self.assertEqual(idle_gate.wait_until_idle(g, timeout=2, quiet_seconds=0.02), "idle")
        finally:
            t.join()

    def test_wrong_thread_and_incomplete_lifecycle_fail(self):
        g = self.guard()
        g["thread"] = str(uuid.uuid4())
        with self.assertRaises(ValueError):
            idle_gate.require_idle(g)

    def test_pause_and_review_classification(self):
        for reason, status in [
            ("update budget", "completed"),
            ("teacher review", "needs_review"),
            ("signal pause", "paused"),
        ]:
            r = dict(type="complete", experiment_id="run-a", step=2048, reason=reason)
            self.assertEqual(watch_job.terminal_event(r, "run-a", 2048)["status"], status)
            self.assertIsNone(watch_job.terminal_event(r, "run-b", 2048))
            self.assertIsNone(watch_job.terminal_event(r, "run-a", 4096))

    def test_actual_file_event_emits_once(self):
        w = self.register()
        p = self.root / "events.jsonl"
        p.write_text("")

        def producer():
            time.sleep(0.05)
            with p.open("a") as f:
                f.write(
                    json.dumps(
                        dict(
                            type="complete",
                            step=2048,
                            experiment_id="run-a",
                            reason="teacher review",
                        )
                    )
                    + "\n"
                )

        t = threading.Thread(target=producer)
        t.start()
        r = watch_job.watch(self.store, w["id"], p, "run-a", 2048, timeout=2)
        t.join()
        self.assertTrue(r["accepted"])
        self.assertEqual(self.store.get(w["id"])["event"]["status"], "needs_review")
        r = watch_job.watch(self.store, w["id"], p, "run-a", 2048, timeout=0.1)
        self.assertEqual(r["status"], "wait_no_longer_pending")

    def test_ambiguous_delivery_never_retries_itself(self):
        w = self.register()
        self.store.complete(w["id"], dict(status="completed"))

        def broken(w, r):
            raise RuntimeError("ack lost")

        wake.dispatch_once(self.store, broken)
        adapter = unittest.mock.Mock()
        self.assertFalse(wake.dispatch_once(self.store, adapter))
        adapter.assert_not_called()

    def test_cancellation_prevents_wake(self):
        w = self.register()
        self.store.cancel(w["id"])
        self.assertFalse(self.store.complete(w["id"], dict(status="completed")))

    def test_real_worker_records_exit(self):
        w = self.store.register(
            self.thread,
            str(self.root),
            "Report",
            command=[sys.executable, "-c", "raise SystemExit(3)"],
        )
        wake.run_job(self.store, w["id"])
        self.assertEqual(self.store.get(w["id"])["event"]["exit_code"], 3)


if __name__ == "__main__":
    unittest.main()
