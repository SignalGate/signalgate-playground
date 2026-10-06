"""Local limits for the sample app's own scaffolding.

Every number here describes the sample app on your laptop only: how long it
gives itself to shut down, how much it keeps in memory, how big a page of
records is. None of them describes SignalGate.

This is the only module under ``app/kit`` (and ``app/signalgate_client.py``)
allowed to hold numeric duration constants; a test enforces that.
"""

# How long the sample app waits for its SignalGate client to finish sending
# queued events when it shuts down. It stays below the console's own grace
# before a hard stop (APP_STOP_GRACE_S), so a clean shutdown always fits.
CLIENT_CLOSE_TIMEOUT_S = 4.5

# When the console that started the sample app goes away without stopping it,
# the app asks itself to exit, and forces the exit if it is still running
# this long afterwards.
LIFELINE_EXIT_GRACE_S = 6.5

# The longest local delay a canary registration may ask the sample app to
# wait before it answers.
CANARY_DELAY_MAX_MS = 2500

# The longest canary nonce the sample app accepts, in characters.
CANARY_NONCE_MAX_CHARS = 128

# How many call records the sample app keeps in memory per boot; the oldest
# are dropped first.
CALLS_MAX_RECORDS = 100_000

# The most entries one control request may register or read back at once.
PAGE_MAX = 1000

# Text values kept in a call record are cut to this many characters.
RECORD_STR_MAX = 256
