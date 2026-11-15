"""Worker Orchestrator: workers that execute the development system's requests.

A request is the authoritative unit of work (requests/, claims, work records,
evidence, the completion gate). A worker only *executes* one request at a
time: it claims it through the framework (``agent_process.claim_request``),
runs a coding agent on it, checks the result independently and, when the work
cannot be finished, escalates it through the framework (needs-review). No
worker keeps a ticket list of its own.

Modules:

    state       per-worker runtime records under .agent/state/runtime/workers/
    agent       launching and verifying one agent session (generic CLI adapter)
    gitops      read-only git inspection of the checkout
    prompt      the worker prompt (prompts/worker.md)
    validation  independent checks after each attempt
    runner      the worker process loop (select, claim, attempt, conclude)
    pool        the API the CLI and the control panel use: status, start,
                stop, enable, disable, recover, peek

docs/process/worker-orchestrator.md describes the model.
"""

VERSION = "2.0.0"
