"""Local pacing and console constants.

Every number here describes this console and your laptop only: how fast the
local runs go, how long to wait for the sample app on your machine, how often
to redraw. None of them describes SignalGate.

This is the only module on the console side allowed to hold numeric duration
constants: ``playground/traffic``, ``playground/machine``,
``playground/supervisor``, ``playground/verify``, ``playground/runner.py``,
``playground/online.py``, ``playground/api`` and ``playground/statefile.py``
all take their durations and counts from here. Anything tied to SignalGate's
own timing comes from the session's ``limits`` at run time instead. A
test enforces that. The sample app keeps its own in ``app/kit/local_constants.py``.
"""

# Chapter 1 runs offline. A visitor who succeeds confirms after a short random
# delay, compressed so the whole run takes seconds.
OFFLINE_SUCCESS_DELAY_MAX_S = 0.8

# How many visitor requests are in flight at once during an offline run. The
# console's async HTTP client is fastest on loopback with a handful of
# connections; more connections make the whole run slower, not faster.
OFFLINE_CONCURRENCY = 4

# Redraw the live panel after this many finished visitors.
PROGRESS_EVERY_VISITS = 100

# The sample app on your laptop: how long to wait for it to answer its health
# check, how often to ask, and how long a graceful stop may take before the
# process is killed.
APP_START_TIMEOUT_S = 20.0
APP_HEALTH_POLL_S = 0.05
APP_STOP_GRACE_S = 8.0

# Per-request timeout for the console's own loopback calls to the sample app.
LOOPBACK_REQUEST_TIMEOUT_S = 10.0

# How many times the console tries to start the sample app, moving to a new
# port on your laptop when the old one turns out to be taken.
APP_START_ATTEMPTS = 3

# After a file in app/ is saved, wait this long for more saves before
# restarting the sample app, so a burst of saves gives one restart.
RELOAD_DEBOUNCE_S = 0.3

# A save that empties a file and then rewrites it can be seen half-way. The
# watcher looks again after this long before it calls a batch a change.
RELOAD_SETTLE_S = 0.05

# Before the sample app stops, the console waits for its queued log() events
# to leave. How often it reads the app's counters while waiting ...
DRAIN_POLL_S = 0.25
# ... how often it passes the count on to the screen, which redraws its line ...
DRAIN_REPORT_EVERY_S = 0.5
# ... how long the counters may stay unchanged before it stops waiting ...
DRAIN_STALL_S = 12.0
# ... and the longest it waits in total before it stops the app anyway.
DRAIN_MAX_S = 72.0
# In JSON-lines mode (--ci), how often that wait reports its count.
DELIVER_REPORT_EVERY_S = 4.0

# How the console says how long such a wait on your laptop still takes: under
# a minute, it rounds the estimate up to a whole number of these seconds.
ETA_STEP_S = 5

# How long one read of the sample app's counters on your laptop may take
# while waiting, and how many reads in a row may fail before the console
# treats the app as unreachable.
DRAIN_REQUEST_TIMEOUT_S = 2.5
DRAIN_FAILURES_MAX = 3

# Checking your code. The sample app and this console share one clock, so an
# event your app builds for a probe must carry a time within this margin of
# when the probe was sent.
EVENT_TIME_TOLERANCE_S = 5.0
# The event-loop probe: the sample app holds one check() answer back on your
# laptop for this long, and its health route must keep answering meanwhile,
# each time faster than the budget below.
PROBE_CANARY_DELAY_MS = 500
PROBE_HEALTH_BUDGET_S = 0.2
# While waiting for stored counts after a run, how often to ask again.
STORED_POLL_S = 1.0
# When a request to the sample app failed and its log file doesn't show the
# error yet, how long to give the file before reading it once more.
APP_LOG_SETTLE_S = 0.2

# /copy: how long a clipboard tool on your laptop may take to receive the code.
CLIPBOARD_TIMEOUT_S = 5.0

# The console's calls to the playground API: how long one request may take.
API_REQUEST_TIMEOUT_S = 15.0

# Checking that your key stays out of git: how long one git command on your
# laptop may take.
GIT_TIMEOUT_S = 60.0

# Runs through SignalGate. How many visitor requests are in flight at once ...
ONLINE_CONCURRENCY = 4
# ... how often the console reads the sample app's counters to pace the run
# and redraw its panel ...
PACER_POLL_S = 0.5
# ... over how much recent time it measures how fast your app's SDK sends its
# log events ...
DRAIN_RATE_WINDOW_S = 5.0
# ... and the smallest SDK backlog (queued log events) it always allows.
BACKLOG_FLOOR = 50
# A count, not a duration: the most log events one visitor adds to the sample
# app's queue (one when the action happens, one when the user succeeds).
LOGS_PER_VISIT_MAX = 2

# Waiting for your workflow in the dashboard: how often the console asks for
# its status, how long a step that needs a click from you (creating the
# workflow, pressing a button) may stay unchanged before the console hands
# the prompt back to you, and how many reads in a row may fail before it
# stops asking.
WORKFLOW_POLL_S = 1.0
WORKFLOW_NUDGE_S = 45.0
STATUS_READ_FAILURES_MAX = 3
