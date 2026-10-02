# Wake Codex

### Let the job finish. Then wake the agent.

**Event-driven continuations for Codex. No model polling while you wait.**

Training a model? Running a long build? Waiting for an evaluation? Wake Codex
keeps a durable record of the job and queues a follow-up in your existing Codex
chat when there is something to do.

```text
Start the job ──► End your turn ──► Go do something else
                      │
               No model inference
                      │
Job completes / fails / pauses / needs review
                      │
                Durable event
                      │
              Native Codex queue
                      │
          Same chat continues with the result
```

No recurring "is it done yet?" prompts. No new chat for every update. No need to
leave an agent burning tokens to watch a process.

## Why a queue, not another agent process?

A desktop chat can retain its writer lock even after its current turn ends.
Launching `codex exec resume` against that chat may fail with **"already has an
active writer."**

Wake Codex defaults to **`codex queue`**, which hands the message to the existing
session owner instead of competing for that lock. The native queue → automatic
follow-up → visible reply path was exercised in a live desktop chat, with its
queue also visible on the paired phone.

**Queue acceptance is not task completion.** Wake Codex records these separately.
See [verification and limitations](docs/verification.md) for precisely what was
tested and what still depends on your Codex installation.

## What you get

- **Zero model calls while waiting.** A lightweight Python listener handles events.
- **Your existing chat.** Delivery targets an explicit thread UUID.
- **Durable handoff.** SQLite preserves pending events across listener restarts.
- **Duplicate suppression.** The first terminal event wins; repeated callbacks
  do not enqueue another continuation.
- **Useful terminal states.** `completed`, `failed`, `paused`, `needs_review`, and
  `cancelled`.
- **Conservative recovery.** An uncertain delivery is flagged for inspection,
  never blindly retried.
- **Authenticated local callbacks.** The listener binds to `127.0.0.1`; each wait
  has a private callback token.
- **No Python dependencies.** Standard library only. Python 3.10+, macOS or Linux.
- **A Codex skill.** Included [SKILL.md](SKILL.md) explains when and how to use it.

## Requirements

1. Python 3.10 or newer on macOS or Linux. Windows is not currently supported.
2. An authenticated Codex CLI with **`codex queue --help`** support. The live
   desktop test used CLI **0.159.2**. This is a tested version, not a claim that
   every earlier or later release supports the same behavior.
3. An existing saved chat and its exact UUID. Keep the owning desktop/CLI session
   running for automatic consumption. Test your setup once before relying on it.

If the CLI on your PATH lacks `queue`, pass `--codex /absolute/path/to/codex`.
Prefer the binary matching your desktop installation. Wake Codex will fail the
capability check rather than silently falling back to the writer-conflicting path.

## Quick start

```sh
git clone https://github.com/rotcev/wake-codex.git
cd wake-codex

# One private state directory; use the same path for every command.
umask 077
WAKE_STATE="$PWD/.state"

python3 scripts/wakecodex.py --state "$WAKE_STATE" start
python3 scripts/wakecodex.py --state "$WAKE_STATE" doctor
```

`doctor` should report `healthy: true`, `backend: "queue"`, and `dry_run: false`.
The OS selects a free localhost port. No model is invoked by these checks.

### Wrap a foreground job

Replace `YOUR_THREAD_UUID` with the UUID of the chat you want to continue:

```sh
python3 scripts/wakecodex.py --state "$WAKE_STATE" submit \
  --thread YOUR_THREAD_UUID \
  --cwd "$PWD" \
  --then 'Read the job result. Report success or failure briefly, then stop. Do not restart anything.' \
  -- python3 -c 'import time; time.sleep(10); print("Job finished")'
```

The command returns immediately with a wait ID. The detached wrapper captures
the job's exit status and log path, then emits an event. **End the submitting
agent turn** so Codex can consume the queued follow-up. The sleep is only a demo;
replace it with your actual foreground job.

The job command runs with the launcher's host permissions, not inside a Codex
model sandbox. Only submit commands you have authorized. A command that merely
launches another background job needs an explicit completion callback instead.

### Receive an external completion event

Register without a command and keep the returned JSON private:

```sh
python3 scripts/wakecodex.py --state "$WAKE_STATE" submit \
  --thread YOUR_THREAD_UUID --cwd "$PWD" \
  --then 'Inspect the saved evaluation artifacts and report the result. Do not start another run.' \
  > registration.json
```

Have the producer save a small terminal event **after** its outputs are safely
written. For example, `result.json` can contain:

```json
{
  "status": "needs_review",
  "message": "Evaluation finished; the next stage needs review.",
  "report_path": "/absolute/path/to/report.json"
}
```

Then send it:

```sh
python3 scripts/send_event.py \
  --registration registration.json --result result.json
```

Callback retries are safe because the same wait accepts only its first terminal
event. This is not a promise of exactly-once execution across crashes. See
[operations](docs/operations.md) for HTTP, existing-job watching, and recovery.

## Status means what it says

| Status | Meaning |
| --- | --- |
| `waiting` | Registered; no terminal event yet. |
| `ready` | Event saved durably; delivery pending. |
| `delivering` | A delivery attempt is in flight. |
| `queued` | Native queue acknowledged the target and message ID. Not proof the agent ran. |
| `delivered` | Legacy resume backend observed a completed turn. Not proof the task succeeded. |
| `needs_attention` | Delivery failed or is ambiguous. Inspect before retrying. |
| `previewed` | Dry-run prompt saved; nothing sent to Codex. |
| `cancelled` | A pending Wake Codex wait was cancelled locally. |

```sh
python3 scripts/wakecodex.py --state "$WAKE_STATE" list
python3 scripts/wakecodex.py --state "$WAKE_STATE" show WAIT_UUID
python3 scripts/wakecodex.py --state "$WAKE_STATE" cancel WAIT_UUID
python3 scripts/wakecodex.py --state "$WAKE_STATE" stop
```

Cancelling a wait does **not** kill training, retract an already queued message,
or interrupt an agent. Stopping the listener leaves jobs and saved events intact.

## Use it as a Codex skill

The repository root is a self-contained skill. From the cloned repository:

```sh
mkdir -p "$HOME/.codex/skills"
ln -s "$PWD" "$HOME/.codex/skills/wakecodex"
```

If `wakecodex` already exists, inspect and back it up before replacing it—do not
blindly overwrite a customized installation. Reload skill discovery as needed.
Then ask Codex to use **Wake Codex** for an authorized long-running job.

## Test it without spending model tokens

```sh
python3 -m unittest discover -s tests -v
```

The suite includes a real localhost HTTP listener and a real subprocess acting
as a fake Codex queue. It tests authentication, persistence, duplicate callbacks,
crash-safe delivery decisions, terminal-event handling, and queue acknowledgments.
These are deterministic plumbing tests, not a substitute for one live test in
your app version.

## Security and scope

- Queue delivery inherits the existing chat's model, tools, and permissions. It
  does **not** impose a new read-only sandbox. Keep continuation instructions narrow.
- Callback payloads are untrusted result data, not authority to perform new work.
- State, callback credentials, prompts and logs stay local. Do not commit them.
- Event payloads are limited to 64 KiB. Send artifact paths, not giant logs.
- No desktop lock bypasses, private IPC injection, recurring agent polling, or
  automatic retries of uncertain model actions.
- The host must stay awake. Reboot recovery of in-flight job processes is not
  implemented; durable events survive, but a running job is not magically restored.

**Independent community project. Not affiliated with or endorsed by OpenAI.**

MIT licensed. Built for the moments when the useful thing for an agent to do is
*wait without thinking—and return when there is evidence to act on.*
