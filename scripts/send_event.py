#!/usr/bin/env python3
"""Send a small terminal result using a private submit registration."""

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from wakecodex import MAX_EVENT, TERMINAL_EVENTS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registration", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--url", help="Explicit listener URL override, e.g. an SSH tunnel")
    args = parser.parse_args()
    registration = json.loads(Path(args.registration).read_text())
    with Path(args.result).open("rb") as f:
        payload = f.read(MAX_EVENT + 1)
    event = json.loads(payload)
    if (
        not isinstance(event, dict)
        or event.get("status") not in TERMINAL_EVENTS
        or len(payload) > MAX_EVENT
    ):
        parser.error("Expected a terminal JSON object of at most 64 KiB")
    url = args.url or registration.get("service", {}).get("url")
    if not url:
        parser.error("No listener URL; pass --url")
    for attempt in range(4):
        request = urllib.request.Request(
            url.rstrip("/") + registration["callback_path"],
            data=payload,
            headers={
                "Authorization": "Bearer " + registration["callback_token"],
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                print(response.read().decode())
            return
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or attempt == 3:
                raise SystemExit(f"Callback failed: HTTP {exc.code}") from None
        except OSError:
            if attempt == 3:
                raise SystemExit(
                    "Callback unreachable; retain the result and retry later"
                ) from None
        # Retrying delivery of the same event is safe: first terminal event wins.
        # This never retries a model continuation.
        time.sleep(2**attempt)


if __name__ == "__main__":
    main()
