"""A test-only fake of the playground API, to build and test the console without the real one.

Tests only: nothing in ``playground/`` or ``app/`` imports it, it has no
command-line switch, and it never reads the environment. See ``server.py``
for what it answers and ``script.py`` for how a test steers it.
"""

from tests.fake_server.script import Script, default_steps, name_only, no_workflow, playground_flow
from tests.fake_server.server import Delivery, FakeServer, SessionView

__all__ = [
    "Delivery",
    "FakeServer",
    "Script",
    "SessionView",
    "default_steps",
    "name_only",
    "no_workflow",
    "playground_flow",
]
