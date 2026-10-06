"""Messages exchanged between the chapter state machine, the runner and the renderers.

The machine is pure: it receives user input (``Machine.handle``) and results
from the runner (``Machine.on_event``) and answers with a list of the events
below. Renderers turn ``Say`` / ``ShowLogo`` / ``AwaitInput`` into output; the
runner carries out the effects (``StartApp``, ``RunChapter1``, ``SaveState``)
and feeds the outcome back as a result (``AppReady``, ``RunFinished``,
``StateSaved`` and so on). Progress during a run goes from the runner straight
to the renderer as ``RunProgress``; the machine never sees it.

The machine answers with one of two kinds of reply:

- **Turn replies** answer the user's input, or a result that ends a step of
  the session (``AppReady``, ``AppFailed``, ``RunFinished``, ``RunStopped``,
  ``RunInvalidated``, and ``AppDown`` before the opening or in an unattended
  session). A turn reply always ends with ``AwaitInput``, ``Exit`` or an
  effect, so the session never stalls.
- **Notices** report something that happened while the machine was already
  waiting (``AppReloaded``, ``ChangeDetected``, ``StateSaved``, and
  ``AppDown`` after the opening in an interactive session). A notice holds
  ``Say`` events only, possibly none, and never changes ``Machine.to_dict()``:
  the question on screen stays open and any text the user has typed is kept.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Literal

from playground.copy.blocks import Block, block_of

# What kind of input the machine is waiting for.
#   "start"    -> the opening; waits for /start
#   "yes_no"   -> a yes/no question: only yes or no answers it
#   "any"      -> free input; only commands are meaningful
#   "continue" -> "I've done it, check it": Enter (an empty line) or yes goes on
Expect = Literal["start", "yes_no", "any", "continue"]

# Why the sample app came back up.
#   "change"  -> a watched file changed and the console restarted the app
#   "crash"   -> the app stopped on its own and the console started it again
#   "request" -> the console restarted it on purpose (with your key, in chapter 2)
ReloadCause = Literal["change", "crash", "request"]

# Slash commands the console completes and the machine understands.
COMMANDS: tuple[str, ...] = (
    "/start",
    "/continue",
    "/hint",
    "/status",
    "/help",
    "/report",
    "/quit",
    "/copy",
)

# Plain answers the machine understands.
ANSWERS: tuple[str, ...] = ("yes", "no")


# --- output: machine -> renderer -------------------------------------------------


@dataclass(frozen=True)
class Say:
    """Print the copy string ``key`` (see ``playground.copy.en``) with ``params``.

    ``block`` says which kind of block the line belongs to, so a renderer can
    space and style it (see ``playground.copy.blocks``). Left out, it comes
    from the key; the machine sets it only where a key plays another role.
    Two says with the same key and params are equal whatever their block.
    """

    key: str
    params: Mapping[str, Any] = field(default_factory=dict)
    block: Block | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if self.block is None:
            object.__setattr__(self, "block", block_of(self.key))


@dataclass(frozen=True)
class ShowLogo:
    """Draw the logo and the tagline."""


@dataclass(frozen=True)
class AwaitInput:
    """Hand the input line to the user."""

    expect: Expect


@dataclass(frozen=True)
class Exit:
    """Stop the console with ``code`` (0 ok, 2 integration check failed,
    3 compatibility or usage, 4 timeout)."""

    code: int = 0


# --- effects: machine -> runner ----------------------------------------------------


@dataclass(frozen=True)
class StartApp:
    """Start the sample app on loopback."""


@dataclass(frozen=True)
class RunChapter1:
    """Send the chapter 1 visitors through the sample app, offline."""


@dataclass(frozen=True)
class ChapterDone:
    """Chapter ``chapter`` is complete (the runner uses it for ``--until``)."""

    chapter: int


@dataclass(frozen=True)
class SaveState:
    """Save ``Machine.to_dict()`` now, then feed back ``StateSaved(ok)``."""


@dataclass(frozen=True)
class CopySnippets:
    """Copy the snippets of the task ``task`` (a ``TASKS`` key in
    ``playground.copy.en``) for ``/copy``: all of them, or only snippet
    ``index`` (1-based). Its ``StepDone`` says how it went."""

    task: str
    index: int | None = None


# --- results: runner -> machine (and renderer) -----------------------------------


@dataclass(frozen=True)
class AppReady:
    """The sample app answered its health check."""


@dataclass(frozen=True)
class AppFailed:
    """The sample app did not start; ``reason`` is a copy key."""

    reason: str


@dataclass(frozen=True)
class RunProgress:
    """Live counters during a run (runner -> renderer only).

    A run through SignalGate also counts the app's log events: ``posted``
    by your code, ``delivered`` by your app's SDK, ``accepted`` by
    SignalGate, then ``stored`` once the run is over. Chapter 1 leaves them
    None. ``eta_s`` estimates how long the app's SDK still needs to send its
    queued log events, once every visitor is done (None until then, or when
    it can't be measured yet).
    """

    done: int
    total: int
    actions: int
    successes: int
    posted: int | None = None
    delivered: int | None = None
    accepted: int | None = None
    stored: int | None = None
    eta_s: float | None = None


@dataclass(frozen=True)
class DeliverProgress:
    """How far a wait for the app's queued log events has got (runner ->
    renderer only): ``done`` of ``total`` sent, and an estimate of the
    seconds left, or None when it can't be measured yet."""

    done: int
    total: int
    eta_s: float | None = None


@dataclass(frozen=True)
class RunFinished:
    """A run completed. ``attack`` and ``real`` count visitors by class.

    ``elapsed_s`` is the active console time since ``/start``, as the runner
    measured it when the run ended (None when it has no clock).
    """

    total: int
    actions: int
    successes: int
    attack: int
    real: int
    elapsed_s: float | None = None


@dataclass(frozen=True)
class RunStopped:
    """A run was stopped by the user before it completed."""

    done: int
    total: int
    actions: int
    successes: int


@dataclass(frozen=True)
class AppReloaded:
    """The sample app is up again as boot number ``boot`` (a notice).

    ``boot`` counts the boots of this console session, starting at 1.
    """

    boot: int
    cause: ReloadCause = "change"


@dataclass(frozen=True)
class BootFailure:
    """Why the sample app didn't start, read from its output.

    ``error`` is the exception's name and ``message`` its text (keys masked);
    ``location`` is ``app/<file>.py:<line>``; ``source`` is the offending
    line as Python printed it and ``caret`` the marker line under it, when
    there is one. ``hint`` is a copy key for a fix hint, or None.
    """

    error: str
    message: str
    location: str
    source: str | None = None
    caret: str | None = None
    hint: str | None = None


@dataclass(frozen=True)
class AppDown:
    """The sample app isn't running.

    ``location`` is where its start-up failed, as ``app/<file>.py:<line>``,
    or None when that isn't known. ``failure`` says more when the app's
    output told the console why (None otherwise).
    """

    location: str | None = None
    failure: BootFailure | None = None


@dataclass(frozen=True)
class ChangeDetected:
    """A watched file changed while a run was going (a notice)."""


@dataclass(frozen=True)
class RunInvalidated:
    """The sample app stopped or restarted during a run, so the run doesn't count."""


@dataclass(frozen=True)
class StateSaved:
    """The runner's answer to ``SaveState``: whether the save worked (a notice)."""

    ok: bool


# --- the online chapters (2-6) ---------------------------------------------------------


class Phase(Enum):
    """Which kind of traffic run: the learning run, or a protected round."""

    LEARN = auto()
    PROTECT = auto()


class Gate(Enum):
    """Which workflow gate a wait is for (see ``playground.verify.workflow``)."""

    G2 = auto()
    G3 = auto()
    G4 = auto()


@dataclass(frozen=True)
class CheckKey:
    """Read the API key from ``app/.env``, check its shape and that git can't leak it."""


@dataclass(frozen=True)
class OpenSession:
    """Open the playground session (``size`` visitors per run), check the clock,
    restart the sample app with the key."""

    size: int


@dataclass(frozen=True)
class EndSession:
    """End a session a discarded saved session had open (no result comes back)."""

    session: Mapping[str, Any]


@dataclass(frozen=True)
class RunGate:
    """Check your code for ``chapter``: the code gate, and in chapter 5 the
    rehearsal and protection-live too."""

    chapter: int


@dataclass(frozen=True)
class RunTraffic:
    """Send ``size`` visitors through your app and SignalGate, then check the run."""

    phase: Phase
    size: int
    round: int = 1


@dataclass(frozen=True)
class WaitWorkflow:
    """Watch the workflow in the dashboard until ``gate`` passes or needs you."""

    gate: Gate


@dataclass(frozen=True)
class RunReadiness:
    """Readiness rounds until protection is live, or it is clear it isn't."""


@dataclass(frozen=True)
class ReadStage:
    """Read the workflow's state and protection stage."""


@dataclass(frozen=True)
class Complete:
    """End the session, keeping the workflow running or pausing it."""

    keep: bool
    conv_before: float | None = None
    conv_after: float | None = None


@dataclass(frozen=True)
class WriteReport:
    """Write the support bundle for ``/report``."""


@dataclass(frozen=True)
class Prediction:
    """A dry run's prediction, frozen when it lands."""

    conv_now: float
    conv_pred: float
    blocked_share: float


@dataclass(frozen=True)
class RunStats:
    """What one traffic run measured in your app.

    ``real_blocked`` and ``real_total`` count real users (a protected round
    only); ``check_ms`` and ``inside_ms`` are median check() times, all of
    it and the part inside SignalGate (None when there were none).
    ``degraded`` marks a run that was cut short because the app's events
    fell behind, or in which too many check() calls failed open: its numbers
    may be less reliable.
    """

    total: int
    actions: int
    successes: int
    real_blocked: int = 0
    real_total: int = 0
    check_ms: float | None = None
    inside_ms: float | None = None
    degraded: bool = False


@dataclass(frozen=True)
class StepDone:
    """The outcome of an online effect.

    ``says`` is what the step found, already in words (copy keys). When
    ``passed`` is False the tutorial doesn't move on: an interactive session
    asks again, an unattended one exits with ``exit_code``, and a ``fatal``
    outcome exits either way. The other fields carry what a step measured:
    a new ``session``, a run's ``stats``, a learning run's ``shortfall``, a
    dry run's ``prediction``, the workflow's protection ``stage`` and
    whether it is ``paused``. ``session_closed`` says the playground session
    this step used was closed (it expired, or another session replaced it);
    the machine decides what follows. ``elapsed_s`` is the active console
    time since ``/start`` when the step ended, as the runner measured it
    (None when it has no clock).
    """

    passed: bool
    says: tuple[Say, ...] = ()
    exit_code: int = 2
    fatal: bool = False
    session: Mapping[str, Any] | None = None
    stats: RunStats | None = None
    shortfall: int = 0
    prediction: Prediction | None = None
    stage: int | None = None
    paused: bool = False
    session_closed: bool = False
    elapsed_s: float | None = None


Output = Say | ShowLogo | AwaitInput | Exit
Effect = (
    StartApp
    | RunChapter1
    | ChapterDone
    | SaveState
    | CheckKey
    | OpenSession
    | EndSession
    | RunGate
    | RunTraffic
    | WaitWorkflow
    | RunReadiness
    | ReadStage
    | Complete
    | WriteReport
    | CopySnippets
)
Result = (
    AppReady
    | AppFailed
    | RunProgress
    | DeliverProgress
    | RunFinished
    | RunStopped
    | AppReloaded
    | AppDown
    | ChangeDetected
    | RunInvalidated
    | StateSaved
    | StepDone
)
Event = Output | Effect | Result

# The effects the machine hands to the runner (every one is followed by
# nothing else until its result comes back, except the three that give none).
EFFECTS = (
    StartApp,
    RunChapter1,
    ChapterDone,
    SaveState,
    CheckKey,
    OpenSession,
    EndSession,
    RunGate,
    RunTraffic,
    WaitWorkflow,
    RunReadiness,
    ReadStage,
    Complete,
    WriteReport,
    CopySnippets,
)

__all__ = [
    "ANSWERS",
    "COMMANDS",
    "EFFECTS",
    "AppDown",
    "AppFailed",
    "AppReady",
    "AppReloaded",
    "AwaitInput",
    "Block",
    "BootFailure",
    "ChangeDetected",
    "ChapterDone",
    "CheckKey",
    "Complete",
    "CopySnippets",
    "DeliverProgress",
    "Effect",
    "EndSession",
    "Event",
    "Exit",
    "Expect",
    "Gate",
    "OpenSession",
    "Output",
    "Phase",
    "Prediction",
    "ReadStage",
    "ReloadCause",
    "Result",
    "RunChapter1",
    "RunFinished",
    "RunGate",
    "RunInvalidated",
    "RunProgress",
    "RunReadiness",
    "RunStats",
    "RunStopped",
    "RunTraffic",
    "SaveState",
    "Say",
    "ShowLogo",
    "StartApp",
    "StateSaved",
    "StepDone",
    "WaitWorkflow",
    "WriteReport",
]
