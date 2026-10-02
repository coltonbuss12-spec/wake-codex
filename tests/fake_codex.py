#!/usr/bin/env python3
"""Test double: accepts a queue message without making any model calls."""

import sys
import uuid

if sys.argv[1:] == ["queue", "--help"]:
    print("queue --thread UUID --message TEXT")
    raise SystemExit(0)
assert sys.argv[1] == "queue", "must not fall back to exec resume"
thread = sys.argv[sys.argv.index("--thread") + 1]
print(f"Queued message {uuid.uuid4()} for thread {thread}.")
