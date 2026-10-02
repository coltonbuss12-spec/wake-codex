"""Conservative same-chat idle barrier. Read-only session inspection, no inference.

Rollout lifecycle observation is not an atomic app-server ownership lock.
Desktop display must still be verified separately after an authorized test.
"""

import json
import os
import select
import sqlite3
import time
from pathlib import Path


def resolve_rollout(database, thread):
    db = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = db.execute("SELECT rollout_path FROM threads WHERE id=?", (thread,)).fetchone()
    finally:
        db.close()
    if not row:
        raise ValueError("Target thread is not in this installation")
    return Path(row[0]).resolve(strict=True)


def read_lifecycle(path, thread):
    last = None
    user_count = 0
    with Path(path).open() as f:
        header = json.loads(f.readline())
        if header.get("type") != "session_meta" or header.get("payload", {}).get("id") != thread:
            raise ValueError("Rollout identity mismatch")
        for line in f:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("type") != "event_msg":
                continue
            event = msg.get("payload", {})
            if event.get("type") == "user_message":
                user_count += 1
            if event.get("type") in ("task_started", "task_complete", "turn_aborted"):
                last = dict(kind=event["type"], turn_id=event.get("turn_id"))
    return dict(last=last, user_count=user_count)


def guard_state(guard):
    current = resolve_rollout(guard["database"], guard["thread"])
    if current != Path(guard["rollout"]).resolve():
        return "changed_rollout"
    state = read_lifecycle(current, guard["thread"])
    last = state["last"]
    if state["user_count"] != guard["user_count"]:
        return "new_user_input"
    if not last or last["turn_id"] != guard["after_turn"]:
        return "changed_turn"
    if last["kind"] == "task_started":
        return "active"
    if last["kind"] == "turn_aborted":
        return "aborted"
    return "idle"


def require_idle(guard):
    state = guard_state(guard)
    if state != "idle":
        raise RuntimeError("Same-chat wake refused: " + state)


class FileSignal:
    """Kernel file-change wait on macOS; bounded Python-only fallback elsewhere."""

    def __init__(self, path):
        self.fd = os.open(path, os.O_RDONLY)
        self.queue = select.kqueue() if hasattr(select, "kqueue") else None
        if self.queue:
            self.queue.control(
                [
                    select.kevent(
                        self.fd,
                        filter=select.KQ_FILTER_VNODE,
                        flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                        fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE,
                    )
                ],
                0,
            )

    def wait(self, seconds):
        if self.queue:
            self.queue.control(None, 1, seconds)
        else:
            time.sleep(min(seconds, 1.0))

    def close(self):
        if self.queue:
            self.queue.close()
        os.close(self.fd)


def wait_until_idle(guard, timeout=1800, quiet_seconds=5):
    """Wait only for the submitting turn; cancel rather than follow a newer turn."""
    signal = FileSignal(guard["rollout"])
    deadline = time.monotonic() + timeout
    idle_since = None
    try:
        while time.monotonic() < deadline:
            state = guard_state(guard)
            if state not in ("active", "idle"):
                return state
            if state == "idle":
                if idle_since is None:
                    idle_since = time.monotonic()
                if time.monotonic() - idle_since >= quiet_seconds:
                    require_idle(guard)
                    return "idle"
                delay = quiet_seconds - (time.monotonic() - idle_since)
            else:
                idle_since = None
                delay = min(60, deadline - time.monotonic())
            signal.wait(max(0.01, delay))
        return "deadline"
    finally:
        signal.close()
