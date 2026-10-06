"""The verifier: every "yes, done" is checked against evidence.

Every check ends as PASS, FAIL or NOT RUN, and a check that couldn't run is
never PASS. The evidence is what the console sent plus what the sample app
recorded, read back through its control routes (``evidence``); nothing here
calls the playground API or SignalGate itself.

Entry points for the chapters
-----------------------------

Reaching the app: ``AppLink(base_url, token)`` for the running boot.

Chapter 3, on ``yes``::

    visits = <lease: phase "probe", count PROBE_VISITS, probe_roles(3)>
    code = await run_code_gate(link, visits, chapter=3, routes=routes, root=root)
    lines = gate_lines(code)        # the findings in words, then the summary
    ids = code.finding_ids          # all telemetry ever learns
    # code.verdict is Verdict.PASS / FAIL / NOT_RUN; code.routes_sha256 binds a
    # PASS to the file it read, so a later change re-runs the gate.

The learning run, and every traffic run after it::

    start = await mark(control)     # just before the run, on the same boot
    ... every visitor request carries X-Playground-Run: run ...
    ev = await collect(control, start, run=run)  # after the drain;
                                                 # EvidenceUnavailable: run doesn't count
    stored = await wait_for_stored(fetch_stored_since, posted_counts(ev), limits.ingest_wait_s)
    data = data_gate(ev, stored)    # data.result, data.degraded, data.posted, ...
    g1 = gate_g1(status)            # g1.shortfall sizes a top-up run

Chapter 4: ``gate_g2(status)`` (freeze ``.prediction`` on PASS), then
``gate_g3(status)``; each FAIL carries a ``reason`` code for the copy.

Chapter 5, on ``yes``::

    visits = <lease: phase "probe", count PROBE_VISITS, probe_roles(5)>
    code = await run_code_gate(link, visits, chapter=5, routes=routes, root=root)
    rehearsal = await run_rehearsal(link, routes=routes, root=root)
    g4 = gate_g4(status)
    ready = readiness(readiness_visits, ev)   # after each readiness round
    step = readiness_next([...verdicts so far...], limits.readiness_max_attempts)
    tripped = tripwire(ev)                    # during a protected round

Modules: ``model`` (verdicts and results), ``findings`` (the public finding
IDs), ``static`` (``ast`` checks on ``app/routes.py``), ``evidence``,
``applog`` (what a handler raised, read from the app's log), ``probes`` (the
code gate), ``rehearsal``, ``delivery`` (the data gate), ``workflow``
(G1-G4, readiness, tripwire) and ``messages`` (the words).
"""

from playground.verify.delivery import DataGate, data_gate, posted_counts, wait_for_stored
from playground.verify.evidence import (
    AppLink,
    Evidence,
    EvidenceUnavailable,
    Mark,
    collect,
    connect,
    mark,
    new_run_id,
)
from playground.verify.messages import finding_lines, gate_lines
from playground.verify.model import Check, Finding, GateResult, Site, Verdict
from playground.verify.probes import PROBE_VISITS, probe_roles, run_code_gate
from playground.verify.rehearsal import run_rehearsal
from playground.verify.workflow import (
    Readiness,
    WorkflowGate,
    gate_g1,
    gate_g2,
    gate_g3,
    gate_g4,
    readiness,
    readiness_next,
    tripwire,
)

__all__ = [
    "PROBE_VISITS",
    "AppLink",
    "Check",
    "DataGate",
    "Evidence",
    "EvidenceUnavailable",
    "Finding",
    "GateResult",
    "Mark",
    "Readiness",
    "Site",
    "Verdict",
    "WorkflowGate",
    "collect",
    "connect",
    "data_gate",
    "finding_lines",
    "gate_g1",
    "gate_g2",
    "gate_g3",
    "gate_g4",
    "gate_lines",
    "mark",
    "new_run_id",
    "posted_counts",
    "probe_roles",
    "readiness",
    "readiness_next",
    "run_code_gate",
    "run_rehearsal",
    "tripwire",
    "wait_for_stored",
]
