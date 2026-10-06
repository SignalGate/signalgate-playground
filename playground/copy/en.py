"""Every string the playground console shows, in English.

Nothing user-facing is written anywhere else: the state machine, the renderers
and the command line refer to these entries by key and call ``text()``.

Templates use ``str.format`` fields, and the template owns the formatting:
pass raw values and let the field format them, for example ``{total:,}`` for
``10,000`` and ``{conversion:.1%}`` for ``20.0%``.

A template may mark words to stress as ``**bold**``, within one line. Only the
template is read for these marks, never the values put into it, so a ``**``
in a value (``2**8`` in someone's code) is shown as it is. ``text()`` returns
plain text without the marks; ``segments()`` returns the pieces with a bold
flag each, for a renderer that can show bold.

Entries whose key starts with ``snippet.`` are code to paste: they are
returned exactly as written, never formatted and never read for marks. The
"Your turn" tasks (``TASKS``) are put together from a title, numbered edits
(a location line and a snippet each) and a footer.
"""

from __future__ import annotations

import re
from typing import Any

TEXT: dict[str, str] = {
    # --- name and tagline -----------------------------------------------------
    "app.wordmark": "SignalGate",
    "app.tagline": "Synthetic traffic · real SignalGate verdicts · your code.",
    # --- status marks (colour is never the only signal) ------------------------
    "mark.fail": "✗",
    "mark.note": "!",
    "mark.not_run": "◌",
    # --- command line ---------------------------------------------------------
    "cli.description": "Learn to integrate SignalGate's log() and check() in your terminal.",
    "cli.help.size": "Visitors per run through SignalGate ({min:,}-{max:,}).",
    "cli.help.plain": "Plain output: no colour, no animation.",
    "cli.help.no_telemetry": (
        "Don't tell SignalGate which tutorial checks failed or your run's conversion before "
        "and after."
    ),
    "cli.help.ci": "JSON lines for scripts; on automatically when output isn't a terminal.",
    "cli.help.yes": "Answer yes to every question but the re-run offer (for scripted runs).",
    "cli.help.until": "Stop after this chapter (for scripted runs).",
    "cli.error.size_not_int": "--size must be a whole number (got {value!r})",
    "cli.error.size_range": "--size must be between {min:,} and {max:,} (got {value:,})",
    # --- console lifecycle ----------------------------------------------------
    "console.lock_held": (
        "Another playground console is already running in this folder. Close it, then try again."
    ),
    "console.env_created": "Created app/.env from app/.env.example.",
    "console.bye": "Bye. Run uv run playground to start again.",
    "console.interrupt_again": "Stopping… press Ctrl-C again to exit.",
    "console.saved": "Progress saved. Type /continue next time to pick up where you left off.",
    # --- the sample app on your laptop ----------------------------------------
    "app.starting": "Starting the sample app…",
    "app.ready": (
        "I started a small sample app on your laptop. It's what the simulated visitors will call."
    ),
    "app.failed": "The sample app didn't start. Its output is in .playground/app.log.",
    "app.crashed": (
        "The sample app didn't complete any actions, so this run doesn't count. "
        "Its output is in .playground/app.log."
    ),
    "app.reloaded": "✓ Saved. Your change is running.",
    "app.restarted": "✓ The sample app stopped, so I restarted it.",
    "app.restarted_key": "✓ Restarted the sample app with your key.",
    "app.down": (
        "The sample app isn't running. Its output is in .playground/app.log. "
        "Fix the error and save; I'll restart it."
    ),
    "app.down_at": (
        "The sample app didn't start: {location}. Fix it and save; I'll restart it. "
        "Its output is in .playground/app.log."
    ),
    "app.down.error": "The sample app didn't start: {error} at {location}.",
    "app.down.code": "{code}",
    "app.down.hint.indent": (
        "The pasted code has extra spaces at the start. Indent it to match the lines around it."
    ),
    "app.down.hint.import": (
        "This import names something app.signalgate_client doesn't have. "
        "It provides sg and sg_event."
    ),
    "app.down.fix": "Fix it and save; I'll restart it. Its output is in .playground/app.log.",
    "app.not_ready": (
        "The sample app isn't running. Fix the error above and save the file; I'll restart it."
    ),
    # --- saving a file while a run is going -------------------------------------
    "reload.deferred": "change detected, reloading after this run · Ctrl-C to stop the run.",
    "run.invalidated": (
        "The sample app stopped or restarted during the run, so this run doesn't count."
    ),
    "run.again": "Type /start to run it again.",
    # --- waiting for the sample app's queued log() events ---------------------------
    # The time left is estimated on your laptop, from how fast your app's SDK has
    # been sending its queued events.
    "drain.waiting": (
        "Waiting for your app to send {remaining:,} queued log() events… Ctrl-C to skip."
    ),
    "drain.waiting_quit": (
        "Waiting for your app to send {remaining:,} queued log() events… Ctrl-C to quit now."
    ),
    "drain.skipped": "Skipped the wait; {remaining:,} log() events were still queued.",
    "drain.gave_up": (
        "Stopped waiting for your app's queued log() events; {remaining:,} were still queued."
    ),
    "drain.done": "✓ All queued logs sent.",
    "drain.sending": "Sending your app's queued logs to SignalGate",
    "drain.count": "{done:,} / {total:,}",
    "drain.skip_hint": "Ctrl-C to skip",
    "drain.quit_hint": "Ctrl-C to quit now",
    "drain.note": (
        "Your app's SDK sends logs one at a time, so delivery trails the visitors a little. "
        "Ctrl-C to skip."
    ),
    "drain.note_quit": (
        "Your app's SDK sends logs one at a time, so delivery trails the visitors a little. "
        "Ctrl-C to quit now."
    ),
    "eta.seconds": "about {seconds:,} s left",
    "eta.minutes": "about {minutes:,} min left",
    # --- saved progress -------------------------------------------------------
    "continue.offer": (
        "Welcome back. Type /continue to pick up where you left off, or /start to begin again."
    ),
    "continue.back": "Picking up where you left off.",
    "continue.run_interrupted": "Your last run was interrupted, so it doesn't count.",
    "continue.none": "There's no saved session to continue.",
    "state.set_aside": (
        "Your saved progress couldn't be read, so I moved it to .playground/state.json.bad "
        "and started fresh."
    ),
    "state.save_failed": (
        "I couldn't save your progress to .playground/state.json, "
        "so /continue won't have it next time."
    ),
    # --- chapters -------------------------------------------------------------
    "chapter.1": "Meet the attack",
    "chapter.2": "Connect",
    "chapter.3": "Teach SignalGate",
    "chapter.4": "Create the workflow",
    "chapter.5": "Turn on protection",
    "chapter.6": "Results and next steps",
    "chapter.heading": "Chapter {chapter} of 6 · {title}",
    # --- the opening (after the logo, before /start) ----------------------------
    "opening": (
        "Here's what will happen:\n"
        "\n"
        "1. I'll send 10,000 visitors to a small app on your laptop. The visitors are real\n"
        "   requests to the sample app I just started on your laptop. Each one triggers a\n"
        "   protected action: the kind of action attackers abuse. Most of them are an\n"
        "   attack: they trigger the action and never finish. You'll see how many actions\n"
        "   turn into a real success. No SignalGate yet, no account needed.\n"
        "2. You'll add two log() calls. Then I'll send a couple of thousand visitors through\n"
        "   your code so SignalGate can learn what normal traffic looks like here.\n"
        "3. You'll create a workflow in the dashboard and run a dry run: a one-time\n"
        "   prediction of what blocking would change. A workflow needs enough events first:\n"
        "   at least 1,000 actions is recommended, and we'll send more than that.\n"
        "4. Once the workflow is live, you'll add one check() call before the action.\n"
        "   Only then does SignalGate start deciding.\n"
        "5. I'll run the traffic again and show you the before and after.\n"
        "\n"
        "In a real app, SignalGate's browser script creates a sealed, single-use envelope for\n"
        "every request, and your frontend sends it to your backend. Here I play the frontend:\n"
        "I ask SignalGate for envelopes and attach them the way your frontend would. I can't\n"
        "open them either. Only SignalGate can.\n"
        "\n"
        "Type /start when you're ready."
    ),
    # --- chapter 1: meet the attack (offline) -----------------------------------
    "ch1.intro": (
        "I'm sending {total:,} simulated visitors to the sample app I started on your laptop. "
        "Each one is a real request to it. No SignalGate yet, and nothing leaves your machine."
    ),
    "ch1.panel.title": "Chapter 1 · no SignalGate",
    "ch1.panel.visitors": "Visitors",
    "ch1.panel.actions": "Actions",
    "ch1.panel.successes": "Successes",
    "ch1.panel.conversion": "Conversion",
    "ch1.panel.progress": "{done:,} of {total:,}",
    "ch1.close": (
        "{conversion:.1%} of actions turned into a success. "
        "{attack_in_ten} in 10 were the attack. **Ready to connect SignalGate?** (yes/no)"
    ),
    "ch1.stopped": (
        "Run stopped after {done:,} of {total:,} visitors. Type /start to run it again."
    ),
    # --- replies to commands ----------------------------------------------------
    "reply.no": "Okay, I'll wait for you.",
    "reply.unknown": "I understand yes, no and /commands. /help lists them.",
    "reply.busy": "A run is in progress. Ctrl-C stops it.",
    "help": (
        "Commands:\n"
        "  /start     begin\n"
        "  /continue  pick up where you left off\n"
        "  Enter      I've done it: check it (yes works too)\n"
        "  yes, no    answer a question that ends in (yes/no)\n"
        "  /hint      a nudge for the current step\n"
        "  /copy      copy this step's code (/copy 2: only the second snippet)\n"
        "  /status    where you are and what's done\n"
        "  /report    a support bundle for a bug report\n"
        "  /help      this list\n"
        "  /quit      stop and exit\n"
        "Ctrl-C stops a run; press it again to exit."
    ),
    "hint.opening": (
        "Type /start to begin. Chapter 1 runs only on your laptop; no account needed."
    ),
    "hint.ch1.running": "Nothing to do yet: watch the numbers. Ctrl-C stops the run.",
    "hint.ch1.close": (
        "Chapter 1 shows how many actions turn into a real success when most of the "
        "traffic is an attack. Type yes to go on, or no to take a break."
    ),
    "status.not_started": "You haven't started yet. Type /start when you're ready.",
    "status.chapter": "Chapter {chapter} of 6 · {title}",
    "status.done_none": "Completed: nothing yet.",
    "status.done_ch1": "Completed: chapter 1 ({conversion:.1%} conversion without SignalGate).",
    "status.done_ch2": "Connected: your playground session is open.",
    "status.done_learn": (
        "Learning run: {actions:,} actions, {successes:,} successes, {conversion:.1%} conversion."
    ),
    "status.prediction": "Dry-run prediction (snapshot): {conv_now:.1%} → {conv_pred:.1%}.",
    "status.round": (
        "Protected round {round}: {conversion:.1%} conversion, "
        "{real_blocked:,} of {real_total:,} real users blocked."
    ),
    "status.workflow": "Workflow playground: {state}.",
    "status.workflow_unknown": "Workflow playground: I couldn't read its status just now.",
    # --- checking your code: the message format -----------------------------------
    "verify.title": "{mark} {title}",
    "verify.location": "  {file}:{line}   {code}",
    "verify.why": "  Why: {why}",
    "verify.fix": "  Fix: {fix}",
    "verify.not_run": "{mark} {reason}",
    "verify.summary": "✓ {passed} passed · ! {notes} {notes_word} · ◌ {not_run} not run",
    "verify.summary.failed": "✗ {failed} failed · ",
    "verify.summary.proof": " — proven by {accepted} real {events_word} SignalGate accepted.",
    "verify.summary.rehearsal": " — a rehearsal on your laptop: nothing was sent to SignalGate.",
    "verify.word.note": "note",
    "verify.word.notes": "notes",
    "verify.word.event": "event",
    "verify.word.events": "events",
    "verify.key_masked": "pk_live_…",
    "verify.ellipsis": "…",
    # --- checking your code: why a check couldn't run ------------------------------
    "verify.not_run.app": (
        "The sample app didn't answer. Its output is in .playground/app.log. "
        "Press Enter to try again."
    ),
    "verify.not_run.self_test": (
        "Something in app/kit changed, so I can't check your code. "
        "Restore it with git checkout -- app/kit, then press Enter."
    ),
    "verify.not_run.key": (
        "The sample app has no API key yet. Put it in app/.env, then press Enter."
    ),
    "verify.not_run.restarted": (
        "The sample app restarted while I was checking. Press Enter to try again."
    ),
    "verify.not_run.records": (
        "The sample app dropped some of its records, so I can't judge this. "
        "Press Enter to try again."
    ),
    "verify.not_run.signalgate": "Your app couldn't reach SignalGate. Press Enter to try again.",
    "verify.not_run.lease": "I couldn't get envelopes for these checks. Press Enter to try again.",
    "verify.not_run.routes": "I couldn't read app/routes.py. Fix it and save, then press Enter.",
    "verify.not_run.depends": "Skipped: an earlier check has to pass first.",
    "verify.not_run.verdict": (
        "SignalGate blocked one of my test requests, so I couldn't finish this check. "
        "Press Enter to try again."
    ),
    "verify.not_run.nothing": "Your app sent no log events in this run.",
    "verify.not_run.status": (
        "I couldn't read how many events SignalGate stored. Press Enter to try again."
    ),
    "verify.not_run.stored": (
        "SignalGate is still storing this run's events. Press Enter to try again."
    ),
    "verify.not_run.no_proof": (
        "SignalGate hasn't accepted any of these events yet. Press Enter to try again."
    ),
    # --- findings: where the logs go --------------------------------------------------
    "finding.LOG_MISSING.title": "playground.action is never logged",
    "finding.LOG_MISSING.sent": (
        "I sent a valid action for user {user}. Your app answered {status} but logged nothing."
    ),
    "finding.LOG_MISSING.sent.rehearsal": (
        "I answered {verdict} on your laptop for user {user}. "
        "The action happened, but your app didn't log it."
    ),
    "finding.LOG_MISSING.why": (
        "Without the action log, SignalGate can't learn which actions turn into successes."
    ),
    "finding.LOG_MISSING.fix": (
        "In the action handler, after the action has happened, log playground.action "
        "with the envelope in signalgate_log."
    ),
    "finding.LOG_BEFORE_ACTION.title": "playground.action is logged before the action happens",
    "finding.LOG_BEFORE_ACTION.sent": (
        "I sent a valid action for user {user}. Your app logged it before the action ran."
    ),
    "finding.LOG_BEFORE_ACTION.why": (
        "A log for an action that never happened would teach SignalGate noise."
    ),
    "finding.LOG_BEFORE_ACTION.fix": "Move this log() below the line where the action happens.",
    "finding.LOG_WITHOUT_ACTION.title": "playground.action is logged even when the action fails",
    "finding.LOG_WITHOUT_ACTION.sent": (
        "The action failed for user {user}. Your app answered {status} but still logged it."
    ),
    "finding.LOG_WITHOUT_ACTION.sent.invalid_user": (
        "I sent an invalid user ({user}). Your app answered {status} but still logged it."
    ),
    "finding.LOG_WITHOUT_ACTION.sent.action_failed": (
        "I made the action fail for user {user}. Your app answered {status} but still logged it."
    ),
    "finding.LOG_WITHOUT_ACTION.why": (
        "A log for an action that never happened would teach SignalGate noise."
    ),
    "finding.LOG_WITHOUT_ACTION.fix": (
        "Log only after the action has happened: not before it, not in a finally block, "
        "and not on an error path."
    ),
    "finding.SUCCESS_LOG_MISSING.title": "playground.success is never logged",
    "finding.SUCCESS_LOG_MISSING.sent": (
        "I confirmed the right token for user {user}. "
        "Your app answered {status} but logged nothing."
    ),
    "finding.SUCCESS_LOG_MISSING.why": (
        "Without success logs, SignalGate can't tell which actions turn into successes."
    ),
    "finding.SUCCESS_LOG_MISSING.fix": (
        "In the confirm handler, after the token checks out, log playground.success "
        "with the envelope in signalgate."
    ),
    "finding.SUCCESS_LOG_ON_FAILURE.title": (
        "playground.success is logged even when the token is wrong"
    ),
    "finding.SUCCESS_LOG_ON_FAILURE.sent": (
        "I sent a wrong token for user {user}. Your app answered {status} but still logged it."
    ),
    "finding.SUCCESS_LOG_ON_FAILURE.why": "SignalGate would count failed attempts as real successes.",
    "finding.SUCCESS_LOG_ON_FAILURE.fix": (
        "move this log() inside the success branch, after the token check passes."
    ),
    "finding.WRONG_METHOD.title": "A log uses the wrong method name",
    "finding.WRONG_METHOD.sent": "Your app logged {method!r}. I expected {expected}.",
    "finding.WRONG_METHOD.sent.action": (
        "In the action handler your app logged {method!r}. I expected {expected}."
    ),
    "finding.WRONG_METHOD.sent.confirm": (
        "In the confirm handler your app logged {method!r}. I expected {expected}."
    ),
    "finding.WRONG_METHOD.why": (
        "Only the exact names playground.action and playground.success count here, "
        "and only they ever leave your laptop."
    ),
    "finding.WRONG_METHOD.fix": "Use exactly {expected}: same case, no spaces.",
    "finding.LOGGED_TWICE.title": "{method} is logged {count} times per request",
    "finding.LOGGED_TWICE.sent": (
        "I sent one {handler} request for user {user}. Your app logged {method} {count} times."
    ),
    "finding.LOGGED_TWICE.why": "Each envelope works once, so every log after the first is rejected.",
    "finding.LOGGED_TWICE.fix": "Log once per request.",
    # --- findings: envelopes and event fields ----------------------------------------
    "finding.WRONG_ENVELOPE_FIELD.title": "A call reads the wrong envelope",
    "finding.WRONG_ENVELOPE_FIELD.title.log": "{method} is logged with the wrong envelope",
    "finding.WRONG_ENVELOPE_FIELD.title.check": "check() reads the wrong envelope",
    "finding.WRONG_ENVELOPE_FIELD.sent": (
        "I put a fresh envelope in {expected_field}, but your app used the one from {field}."
    ),
    "finding.WRONG_ENVELOPE_FIELD.sent.log": (
        "I put a fresh envelope in {expected_field}, but your app logged the one from {field}."
    ),
    "finding.WRONG_ENVELOPE_FIELD.sent.check": (
        "I put a fresh envelope in {expected_field}, "
        "but your app's check() used the one from {field}."
    ),
    "finding.WRONG_ENVELOPE_FIELD.why": (
        "Each field carries its own single-use envelope for one call: signalgate for check() "
        "and the success log, signalgate_log for the action log."
    ),
    "finding.WRONG_ENVELOPE_FIELD.fix": "Read {expected_field} for this call.",
    "finding.ENVELOPE_REUSED.title": "One envelope is used for two calls",
    "finding.ENVELOPE_REUSED.sent": (
        "In one action request your app used the envelope from {field} for both check() and log()."
    ),
    "finding.ENVELOPE_REUSED.why": "Envelopes are single-use: SignalGate rejects the second call.",
    "finding.ENVELOPE_REUSED.fix": "Use signalgate for check() and signalgate_log for log().",
    "finding.ENVELOPE_FROM_OTHER_REQUEST.title": "A call uses an envelope from another request",
    "finding.ENVELOPE_FROM_OTHER_REQUEST.title.log": (
        "{method} is logged with an envelope from another request"
    ),
    "finding.ENVELOPE_FROM_OTHER_REQUEST.title.check": (
        "check() uses an envelope from another request"
    ),
    "finding.ENVELOPE_FROM_OTHER_REQUEST.sent": (
        "I put a fresh envelope in {expected_field} of this request, "
        "but your app used a different one."
    ),
    "finding.ENVELOPE_FROM_OTHER_REQUEST.why": (
        "Each request carries its own single-use envelope. "
        "One kept from an earlier request has already been used."
    ),
    "finding.ENVELOPE_FROM_OTHER_REQUEST.fix": (
        "Read the envelope from this request's {expected_field}, "
        "and don't keep envelopes between requests."
    ),
    "finding.USER_ID_MISMATCH.title": "The event's user_id isn't the request's user",
    "finding.USER_ID_MISMATCH.sent": "I sent user {user}; your app logged user_id {got!r}.",
    "finding.USER_ID_MISMATCH.why": "SignalGate needs to know which user each request is for.",
    "finding.USER_ID_MISMATCH.fix": "Pass the request's user (body.user) to sg_event().",
    "finding.IP_NOT_FORWARDED.title": "The event's IP isn't the visitor's address",
    "finding.IP_NOT_FORWARDED.sent": "I sent the request from {ip}; your app logged {got!r}.",
    "finding.IP_NOT_FORWARDED.why": (
        "SignalGate needs each visitor's own address, not your server's or a proxy's."
    ),
    "finding.IP_NOT_FORWARDED.fix": (
        "Use request.client.host, as sg_event() does. "
        "In production, set forwarded_allow_ips to your proxy's address."
    ),
    "finding.EVENT_TIME_INVALID.title": "The event's timestamp isn't the current time",
    "finding.EVENT_TIME_INVALID.sent": "Your app logged the timestamp {got!r}.",
    "finding.EVENT_TIME_INVALID.why": (
        "Each event needs the time it happened, in RFC 3339 with a timezone offset."
    ),
    "finding.EVENT_TIME_INVALID.fix": (
        "Let sg_event() set it, or use datetime.now(timezone.utc).isoformat()."
    ),
    "finding.LOG_REJECTED.title": "SignalGate rejected a log event",
    "finding.LOG_REJECTED.title.run": "SignalGate rejected {count:,} log events",
    "finding.LOG_REJECTED.sent": (
        "SignalGate rejected your app's {method} log for user {user} (status {code})."
    ),
    "finding.LOG_REJECTED.sent.run": (
        "{count:,} log events in this run were rejected (status {code}), "
        "and none of them was a repeat after a timeout."
    ),
    "finding.LOG_REJECTED.why": "An envelope works only exactly as it arrived, and only once.",
    "finding.LOG_REJECTED.fix": (
        "Pass the envelope through unchanged, once: sg_event(request, user, envelope, method)."
    ),
    # --- findings: the request must survive SignalGate --------------------------------
    "finding.REQUEST_FAILED.title": "Your app failed a valid request",
    "finding.REQUEST_FAILED.sent": (
        "I sent a valid request for user {user}. Your app answered {status}."
    ),
    "finding.REQUEST_FAILED.sent.action": (
        "I sent a valid action for user {user}. Your app answered {status}."
    ),
    "finding.REQUEST_FAILED.sent.confirm": (
        "I confirmed the right token for user {user}. Your app answered {status}."
    ),
    "finding.REQUEST_FAILED.why": "Adding SignalGate must never break a request that worked before.",
    "finding.REQUEST_FAILED.fix": "The error is in .playground/app.log.",
    "finding.HANDLER_ERROR.title": "Your app raised an error in {handler}()",
    "finding.HANDLER_ERROR.sent": "Error: {error} at {location}",
    "finding.HANDLER_ERROR.why": "Adding SignalGate must never break a request that worked before.",
    "finding.HANDLER_ERROR.fix": "Fix that line in app/routes.py and save; I'll restart the app.",
    "finding.CONFIRM_READS_LOG_FIELD.title": (
        "confirm() reads body.signalgate_log, but the confirm request carries its envelope "
        "in body.signalgate"
    ),
    "finding.CONFIRM_READS_LOG_FIELD.sent": (
        "I confirmed the right token for user {user}. Your app answered {status}: {error}"
    ),
    "finding.CONFIRM_READS_LOG_FIELD.why": (
        "Each request carries its own envelope. The confirm request has only signalgate."
    ),
    "finding.CONFIRM_READS_LOG_FIELD.fix": (
        "In confirm(), use body.signalgate: "
        'ev = sg_event(request, body.user, body.signalgate, "playground.success")'
    ),
    "finding.MISSING_ENVELOPE_BREAKS_REQUEST.title": "A request without an envelope fails",
    "finding.MISSING_ENVELOPE_BREAKS_REQUEST.sent": (
        "I sent a request with no envelope. Your app answered {status}."
    ),
    "finding.MISSING_ENVELOPE_BREAKS_REQUEST.sent.action": (
        "I sent an action with no envelope. Your app answered {status}."
    ),
    "finding.MISSING_ENVELOPE_BREAKS_REQUEST.sent.confirm": (
        "I sent a confirm with no envelope. Your app answered {status}."
    ),
    "finding.MISSING_ENVELOPE_BREAKS_REQUEST.why": "A missing envelope must never break the request.",
    "finding.MISSING_ENVELOPE_BREAKS_REQUEST.fix": (
        "Only log when the envelope is there: ev = sg_event(...), then if ev: sg().log(ev). "
        "Keep the envelope fields optional (= None)."
    ),
    "finding.SDK_ERROR_BREAKS_REQUEST.title": "A malformed envelope crashes the request",
    "finding.SDK_ERROR_BREAKS_REQUEST.sent": (
        "I sent an action with a malformed envelope. Your app answered {status}."
    ),
    "finding.SDK_ERROR_BREAKS_REQUEST.why": (
        "Bad input from a client must never take a request down."
    ),
    "finding.SDK_ERROR_BREAKS_REQUEST.fix": (
        "Build the event with sg_event(), which returns None for a malformed envelope."
    ),
    # --- findings: the client, and what the static checks find ------------------------
    "finding.CLIENT_PER_REQUEST.title": "Your code builds its own SignalGate client",
    "finding.CLIENT_PER_REQUEST.sent": "Your app built a new Client while handling a request.",
    "finding.CLIENT_PER_REQUEST.sent.static": "This line builds a new Client.",
    "finding.CLIENT_PER_REQUEST.why": (
        "Every client runs its own queue for log(). One shared client is enough."
    ),
    "finding.CLIENT_PER_REQUEST.fix": "Use sg() instead of Client(...).",
    "finding.CLIENT_CLOSED.title": "The shared client is closed while the app runs",
    "finding.CLIENT_CLOSED.sent": "Your app closed its SignalGate client while handling a request.",
    "finding.CLIENT_CLOSED.why": "A closed client drops every log() that comes after it.",
    "finding.CLIENT_CLOSED.fix": (
        "Don't call close() or use with on sg(). The app closes it when it shuts down."
    ),
    "finding.CHECK_TOO_EARLY.title": "check() is live in chapter 3",
    "finding.CHECK_TOO_EARLY.sent": "Your app called check() while handling a request.",
    "finding.CHECK_TOO_EARLY.sent.static": "This line calls check().",
    "finding.CHECK_TOO_EARLY.why": (
        "Add check() only once the workflow is live. "
        "Before that every answer is allow, so the call protects nothing."
    ),
    "finding.CHECK_TOO_EARLY.fix": "Remove it for now. You'll add it in chapter 5.",
    "finding.KEY_IN_CODE.title": "An API key is written in your code",
    "finding.KEY_IN_CODE.sent": "This line holds an API key.",
    "finding.KEY_IN_CODE.why": "Code gets shared and committed, and a key in it leaks with it.",
    "finding.KEY_IN_CODE.fix": (
        "Remove it and rotate the key in API access. The app reads the key from app/.env."
    ),
    "finding.CHECK_BLOCKS_EVENT_LOOP.title": "check() blocks an async def handler",
    "finding.CHECK_BLOCKS_EVENT_LOOP.sent": (
        "While one check() waited for its answer, your app stopped answering other requests."
    ),
    "finding.CHECK_BLOCKS_EVENT_LOOP.sent.static": (
        "check() is called directly inside async def {function}()."
    ),
    "finding.CHECK_BLOCKS_EVENT_LOOP.why": (
        "check() waits for its answer. Inside async def it holds up every other request meanwhile."
    ),
    "finding.CHECK_BLOCKS_EVENT_LOOP.fix": (
        "Use def, or run it in a thread: await asyncio.to_thread(sg().check, ev)."
    ),
    # --- findings: the check() gate -----------------------------------------------------
    "finding.CHECK_MISSING.title": "No check() before the action",
    "finding.CHECK_MISSING.sent": (
        "I answered block on your laptop for user {user}. "
        "Your app never called check(), and the action happened."
    ),
    "finding.CHECK_MISSING.why": "Without check(), nothing stops the attack.",
    "finding.CHECK_MISSING.fix": (
        "In the action handler, before the action happens, call check() with the envelope "
        "in signalgate, and stop the request on block."
    ),
    "finding.CHECK_AFTER_ACTION.title": "check() runs after the action",
    "finding.CHECK_AFTER_ACTION.sent": (
        "I answered block on your laptop for user {user}. "
        "Your app called check() only after the action had happened."
    ),
    "finding.CHECK_AFTER_ACTION.why": "Once the action has happened, blocking comes too late.",
    "finding.CHECK_AFTER_ACTION.fix": "Move check() above the line where the action happens.",
    "finding.VERDICT_IGNORED.title": "A block verdict doesn't stop the action",
    "finding.VERDICT_IGNORED.sent": (
        "I answered block on your laptop for user {user}. "
        "Your app answered {status}, and the action still happened."
    ),
    "finding.VERDICT_IGNORED.why": "Only stopping the request on block protects the action.",
    "finding.VERDICT_IGNORED.fix": (
        'If sg().check(ev).action == "block", raise HTTPException(status_code=403) '
        "before the action."
    ),
    "finding.GATES_ON_WRONG_VERDICT.title": "Your gate stops requests it should let through",
    "finding.GATES_ON_WRONG_VERDICT.sent": (
        "I answered {verdict} on your laptop for user {user}. "
        "Your app answered {status}, and the action didn't happen."
    ),
    "finding.GATES_ON_WRONG_VERDICT.why": (
        "Only block stops a request. dry_run_block means 'would block', so let it through."
    ),
    "finding.GATES_ON_WRONG_VERDICT.fix": 'Stop the request only when the action is "block".',
    "finding.BLOCKED_ACTION_LOGGED.title": "A blocked action is still logged",
    "finding.BLOCKED_ACTION_LOGGED.sent": (
        "I answered block on your laptop for user {user}. "
        "Your app refused the action but still logged playground.action."
    ),
    "finding.BLOCKED_ACTION_LOGGED.why": (
        "A log for an action that never happened would teach SignalGate noise."
    ),
    "finding.BLOCKED_ACTION_LOGGED.fix": "Log the action only after it has happened.",
    "finding.CHECK_ERROR_BREAKS_REQUEST.title": "A check() error crashes the request",
    "finding.CHECK_ERROR_BREAKS_REQUEST.sent": (
        "I made check() fail on your laptop for user {user}. Your app answered {status}."
    ),
    "finding.CHECK_ERROR_BREAKS_REQUEST.why": (
        "check() raises when a call is rejected. Unhandled, that becomes an error for your visitor."
    ),
    "finding.CHECK_ERROR_BREAKS_REQUEST.fix": (
        "Wrap check() in try/except SignalGateError and apply your policy there. "
        "The tutorial lets the request through."
    ),
    "finding.NO_403_ON_BLOCK.title": "A blocked request gets {status}, not 403",
    "finding.NO_403_ON_BLOCK.sent": (
        "I answered block on your laptop for user {user}. "
        "Your app stopped the action but answered {status}."
    ),
    "finding.NO_403_ON_BLOCK.why": "A 403 tells your frontend clearly that the request was refused.",
    "finding.NO_403_ON_BLOCK.fix": (
        'Raise HTTPException(status_code=403, detail="Request could not be completed").'
    ),
    # --- findings: after a run ------------------------------------------------------------
    "finding.LOG_DROPPED.title": "Your app dropped log events",
    "finding.LOG_DROPPED.sent": "{count:,} log() events were dropped before they were sent.",
    "finding.LOG_DROPPED.sent.queue_full": (
        "{count:,} log() events were dropped because your app's queue was full."
    ),
    "finding.LOG_DROPPED.sent.closed": (
        "{count:,} log() events were dropped because the client was closed."
    ),
    "finding.LOG_DROPPED.why": "Dropped events never reach SignalGate.",
    "finding.LOG_DROPPED.fix": "Run it again.",
    "finding.LOG_DROPPED.fix.queue_full": "Run it again. If it happens again, try a smaller --size.",
    "finding.LOG_DROPPED.fix.closed": "Don't close the shared client while the app runs.",
    "finding.LOGS_NOT_DELIVERED.title": "Too few log events reached SignalGate",
    "finding.LOGS_NOT_DELIVERED.sent": (
        "SignalGate accepted {accepted:,} of the {posted:,} {method} events your app sent."
    ),
    "finding.LOGS_NOT_DELIVERED.why": "SignalGate needs nearly every event of a run.",
    "finding.LOGS_NOT_DELIVERED.fix": (
        "Run it again. If it keeps happening, check the network between your machine "
        "and SignalGate."
    ),
    "finding.EVENTS_NOT_STORED.title": "SignalGate hasn't stored this run's events",
    "finding.EVENTS_NOT_STORED.sent": (
        "SignalGate reports {stored:,} of the {posted:,} {method} events stored."
    ),
    "finding.EVENTS_NOT_STORED.why": "Only stored events count toward your workflow.",
    "finding.EVENTS_NOT_STORED.fix": "Run it again.",
    "finding.DUPLICATE_DELIVERY.title": "Some log events were sent twice",
    "finding.DUPLICATE_DELIVERY.sent": (
        "{count:,} log events were sent again after a timeout, and SignalGate rejected the repeat."
    ),
    "finding.DUPLICATE_DELIVERY.why": (
        "The SDK retries a log that timed out, with the same key. I count each of them once."
    ),
    "finding.DUPLICATE_DELIVERY.fix": "Nothing to change in your code.",
    # --- lines the verifier already put into words -----------------------------------
    "verify.line": "{line}",
    # --- going online: anything unexpected --------------------------------------------
    "online.unreachable": (
        "◌ I couldn't reach SignalGate. Check your connection, then press Enter to try again."
    ),
    "online.unexpected": (
        "◌ Something went wrong on my side. Press Enter to try again; "
        "/report writes a bundle for a bug report."
    ),
    "online.no_app": (
        "◌ The sample app isn't running. Its output is in .playground/app.log. "
        "Fix the error and save; then press Enter."
    ),
    "online.outdated": "This playground is out of date. Run git pull, then start it again.",
    "online.session_gone": (
        "◌ Your playground session was closed. It expired, or a newer one was opened with this "
        "key. **Press Enter** to open a new one and carry on from here."
    ),
    "online.lease_conflict": (
        "✗ Your workspace's public key changed during the run, so it stopped. "
        "Press Enter to run it again."
    ),
    # --- chapter 2: connect -----------------------------------------------------------
    "ch2.intro": (
        "To connect, I need your SignalGate API key in app/.env.\n"
        "\n"
        "New to SignalGate?\n"
        "  1. Sign up at https://signalgate.ai/signup?utm_source=playground&utm_medium=cli"
        "&utm_campaign={version}\n"
        "  2. Verify your email.\n"
        "  3. Open API access and create an API key and a public key.\n"
        "  4. Paste the API key into app/.env.\n"
        "\n"
        "Already have a workspace?\n"
        "  Create a separate API key named playground in API access, and revoke it when\n"
        "  you're done. Or use the key you already have.\n"
        "  Never revoke a key your live app uses. If you don't have your key, ask your\n"
        "  workspace owner, or use a separate workspace with another email.\n"
        "  If you're a member (read-only), you won't be able to create the workflow in\n"
        "  chapter 4."
    ),
    "ch2.key.ask": (
        "Put your API key in app/.env after SIGNALGATE_API_KEY= and save it. "
        "**Press Enter** when it's saved."
    ),
    "ch2.key.ok": "✓ Found your API key in app/.env.",
    "ch2.key.missing": (
        "✗ There's no API key in app/.env yet. Paste it after SIGNALGATE_API_KEY=, save, "
        "then press Enter."
    ),
    "ch2.key.public_key": (
        "✗ That looks like your public key. Paste the API key instead: it starts with "
        "pk_live_. Then press Enter."
    ),
    "ch2.key.format": (
        "✗ That doesn't look like an API key: it's pk_live_ followed by 32 characters. "
        "Copy it again from API access, then press Enter."
    ),
    "ch2.git.ok": "✓ Your key is in no tracked file and no commit, and git ignores app/.env.",
    "ch2.git.no_git": (
        "◌ This folder isn't a git checkout, so I couldn't check that your key stays out of git."
    ),
    "ch2.git.env_tracked": (
        "✗ git tracks app/.env, so your key would be committed. Run git rm --cached app/.env, "
        "rotate your key in API access, then press Enter."
    ),
    "ch2.git.env_not_ignored": (
        "✗ git doesn't ignore app/.env. Add app/.env to .gitignore, then press Enter."
    ),
    "ch2.git.key_in_file": (
        "✗ Your API key is written in {file}, a file git tracks. Remove it, rotate your key "
        "in API access, then press Enter."
    ),
    "ch2.git.key_in_history": (
        "✗ Your API key is in a commit in this checkout. Rotate your key in API access, "
        "put the new one in app/.env, then press Enter."
    ),
    "ch2.git.key_shape_in_file": (
        "✗ {file} holds something shaped like an API key, and git tracks it. Remove it "
        "(and rotate that key if it's real), then press Enter."
    ),
    "ch2.consent": (
        "Before I send anything:\n"
        "\n"
        "I'll send synthetic playground traffic to your SignalGate workspace under the method "
        "names playground.action and playground.success. It will appear in Events, Analytics "
        "and dashboard totals, it counts toward your Quick Start checklist, and it is kept like "
        "any other traffic. It doesn't affect any other method. I'll also tell SignalGate which "
        "tutorial checks failed and your run's conversion before and after, never your code or "
        "keys. Run with --no-telemetry to turn that off.\n"
        "\n"
        "By continuing you confirm you're allowed to use this workspace's API key and to add "
        "tutorial traffic to it.\n"
        "\n"
        "**Do you agree?** (yes/no)"
    ),
    "ch2.session.ok": "✓ Your playground session is open.",
    "ch2.session.bad_key": (
        "✗ SignalGate didn't accept the key in app/.env: it's wrong, or it was revoked. "
        "Create a new one in API access, put it in app/.env, then press Enter."
    ),
    "ch2.session.active": (
        "✗ Another playground session is open in this workspace. It started {started_at} with "
        "the key that starts {key_prefix}. If that's you on another computer, finish it there. "
        "Otherwise it closes by itself after a day without activity."
    ),
    # When that other session started, in local time (part of the line above).
    "ch2.session.started": "at {moment:%H:%M} on {moment:%Y-%m-%d}",
    "ch2.session.no_public_key": (
        "✗ Your workspace has no active public key. Open API access in the dashboard, "
        "create a public key, then press Enter."
    ),
    "ch2.session.busy": "✗ SignalGate asked me to slow down. Wait a moment, then press Enter.",
    "ch2.session.rejected": (
        "✗ SignalGate didn't open the session (status {status}). Press Enter to try again."
    ),
    "ch2.session.retry": "**Press Enter** to try again.",
    "ch2.clock.warn": (
        "! Your computer's clock is {skew:,.0f} seconds off. Turn on automatic time when you can."
    ),
    "ch2.clock.block": (
        "✗ Your computer's clock is {skew:,.0f} seconds off. Turn on automatic time, "
        "then press Enter."
    ),
    # --- chapter 3: teach SignalGate ----------------------------------------------------
    "ch3.intro": "SignalGate learns which actions turn into successes, so your app logs both.",
    # The "Your turn" block is put together from these parts (see TASKS below).
    "ch3.task.title": "Your turn · edit app/routes.py",
    "ch3.edit.1": "**1. At the top of app/routes.py**, with the other imports:",
    "ch3.edit.2": "**2. In action(), right below perform_action(body.user):**",
    "ch3.edit.3": (
        "**3. In confirm(), right below the token check "
        "(after the raise HTTPException(...) line):**"
    ),
    "ch3.task.footer": "/copy copies all three · /hint if you get stuck",
    "snippet.ch3.import": "from app.signalgate_client import sg, sg_event",
    "snippet.ch3.action_log": (
        '    ev = sg_event(request, body.user, body.signalgate_log, "playground.action")\n'
        "    if ev:\n"
        "        sg().log(ev)"
    ),
    "snippet.ch3.success_log": (
        '    ev = sg_event(request, body.user, body.signalgate, "playground.success")\n'
        "    if ev:\n"
        "        sg().log(ev)"
    ),
    "ch3.lesson": (
        "The action log reads signalgate_log; the success log reads signalgate: each request "
        "carries its own envelope. A log for an action that never happened would teach "
        "SignalGate noise."
    ),
    "ch3.ask": "**Press Enter** when all three edits are in.",
    "ch3.again": "Fix it in app/routes.py and save. **Press Enter** to check again.",
    "ch3.learn.intro": (
        "Your code checks out. Now I'm sending {size:,} visitors to your sample app on your "
        "laptop, through your code. Your log() calls send what happens to SignalGate, so it can "
        "learn what normal traffic looks like here."
    ),
    "ch3.learn.again": "**Press Enter** to send the learning run again.",
    "ch3.topup": (
        "SignalGate needs {shortfall:,} more stored actions before a workflow can learn from "
        "them. **Send {shortfall:,} more visitors?** (yes/no)"
    ),
    "ch3.done": "✓ SignalGate has enough of your traffic for a workflow.",
    # --- the live panel of a run through SignalGate -----------------------------------------
    "panel.learn.title": "Chapter 3 · learning run",
    "panel.round.title": "Chapter 5 · protected round",
    "panel.rerun.title": "Chapter 6 · protected round 2",
    "panel.posted": "Logged by your code",
    "panel.delivered": "Delivered by your app",
    "panel.accepted": "Accepted by SignalGate",
    "panel.stored": "Stored by SignalGate",
    "panel.pending": "-",
    "run.stopped": "Run stopped after {done:,} of {total:,} visitors.",
    "run.none": "✗ No visitor got through the sample app, so this run doesn't count.",
    "run.degraded": (
        "! This run was cut short, or some check() calls failed and let visitors through, "
        "so its numbers may be less reliable."
    ),
    # --- chapter 4: create the workflow ---------------------------------------------------
    "ch4.intro": "Now create the workflow in the dashboard, with these exact values:",
    "ch4.click_path": (
        '  Dashboard → Flows → "New flow" (the button with the plus icon)\n'
        "    name                       playground\n"
        "    method                     playground.action\n"
        "    target_method              playground.success\n"
        "    analysis_window (h)        96 (leave)\n"
        "    conversion threshold (%)   0.5 (leave)\n"
        "    min group size             50 (leave)\n"
        "    action                     block (leave)\n"
        "  → Create → ▶ Dry Run"
    ),
    "ch4.why_block": (
        "Keep action = block. Then SignalGate really stops the attack, and your dashboard shows "
        "blocked requests in its counts and charts. With 'dry run block' it would only note what "
        "it would have blocked, and the dashboard would count nothing as blocked."
    ),
    "ch4.where": (
        "Playground traffic counts toward your Quick Start checklist. To see these numbers in "
        "SignalGate, open Analytics and pick the workflow playground, or open Events."
    ),
    "ch4.ask": "**Press Enter** when you've pressed ▶ Dry Run.",
    "ch4.prediction": (
        "✓ dry run complete: conv now {conv_now:.1%} → conv pred {conv_pred:.1%} · "
        "blocked {blocked_share:.0%}"
    ),
    "ch4.lesson": (
        "A dry run is a snapshot of your traffic at that moment. It never updates itself; to "
        "refresh a draft's numbers, click ▶ Dry Run again. Once a workflow is live, this panel "
        "updates by itself."
    ),
    "ch4.warning": (
        "You'll see a ⚠ next to blocked (hover it: 'Blocks >25% of actions'). Under an attack "
        "that's expected: 7 in 10 of these requests are the attack."
    ),
    "ch4.start": "Press ▶ Start in the dashboard. **Press Enter** when you have.",
    # --- what the dashboard shows while the console waits (no clock) ------------------------
    "wait.none": "Looking for a workflow named playground…",
    "wait.draft": "awaiting dry run",
    "wait.queued": "dry run queued",
    "wait.dry_running": "dry run: stage {stage} of {stages_total}",
    "wait.ready": "dry run complete",
    "wait.promoting": "promoting…",
    "wait.active": "active",
    "wait.paused": "paused",
    "wait.protecting": "Protection is turning on.",
    "wait.readiness": "Sending a readiness round through your gate…",
    "status.state.none": "not created yet",
    # --- the workflow isn't there yet ----------------------------------------------------------
    "wf.no_workflow": (
        "I can't see a workflow named playground in the workspace that owns this key. "
        "Create it there with New flow, then press Enter."
    ),
    "wf.not_playground": (
        "Your workflow playground doesn't use the tutorial's methods. Edit it: method "
        "playground.action, target_method playground.success. Then press Enter."
    ),
    "wf.method_wrong": (
        "Your workflow playground's method isn't exactly playground.action. "
        "Edit it, then press Enter."
    ),
    "wf.target_wrong": (
        "Your workflow playground's target method isn't exactly playground.success. "
        "Edit it, then press Enter."
    ),
    "wf.draft": "Your workflow playground is a draft. Press ▶ Dry Run, then press Enter.",
    "wf.no_prediction": (
        "The dry run hasn't shown a prediction yet. Press ▶ Dry Run again, then press Enter."
    ),
    "wf.not_active": "Your workflow playground isn't active yet. Press ▶ Start, then press Enter.",
    "wf.not_block": (
        "Your workflow playground's action isn't block. Switch it to block, then press Enter."
    ),
    "wf.paused": (
        "Your workflow playground is paused. Press ▶ Start to switch it back on, "
        "then **press Enter**."
    ),
    "wf.not_protecting": (
        "Protection hasn't turned on. In the dashboard, check that the workflow is named "
        "playground, its methods are playground.action and playground.success, and it's "
        "active (▶ Start). Then press Enter."
    ),
    "wf.status": (
        "◌ I couldn't read the workflow's status from SignalGate. Press Enter to try again."
    ),
    # --- chapter 5: turn on protection -----------------------------------------------------------
    "ch5.lesson": (
        "Add check() only once the workflow is live. Before that every answer is allow, so the "
        "call protects nothing. And only block stops a request. dry_run_block means 'would "
        "block', so let it through. A request with no envelope skips the check: that's the "
        "current product policy."
    ),
    "ch5.meanwhile": (
        "Meanwhile, while protection turns on: add the check() gate before the action happens. "
        "It reads the signalgate envelope."
    ),
    # The "Your turn" block is put together from these parts (see TASKS below).
    "ch5.task.title": "Your turn · edit app/routes.py",
    "ch5.edit.1": "**1. At the top of app/routes.py**, with the other imports:",
    "ch5.edit.2": "**2. At the start of action(), before perform_action(body.user):**",
    "ch5.task.footer": "/copy copies both · /hint if you get stuck",
    "snippet.ch5.import": "from signalgate import SignalGateError",
    "snippet.ch5.gate": (
        '    ev = sg_event(request, body.user, body.signalgate, "playground.action")\n'
        "    if ev:\n"
        "        try:\n"
        '            if sg().check(ev).action == "block":\n'
        '                raise HTTPException(status_code=403, detail="Request could not be '
        'completed")\n'
        "        except SignalGateError:\n"
        "            pass  # SignalGate unreachable or rejected the call: your policy. "
        "Here we let it through."
    ),
    "ch5.live": "✓ Protection is on for the workflow playground.",
    "ch5.ask": "**Press Enter** when both edits are in and saved. /hint if you get stuck.",
    "ch5.again": "Fix it in app/routes.py and save. **Press Enter** to check again.",
    "ch5.wait.again": "**Press Enter** to check again.",
    "ch5.readiness.intro": (
        "Your gate works in rehearsal. Now a short readiness round: a few visitors to your "
        "sample app on your laptop, through your gate."
    ),
    "ch5.readiness.live": "✓ live gate verified: SignalGate is stopping the attack.",
    "ch5.readiness.weak": (
        "SignalGate stopped part of this synthetic attack in this round. Synthetic results "
        "vary; your traffic will differ."
    ),
    "ch5.readiness.over_blocking": (
        "✗ SignalGate blocked real users in this round. Press ■ Stop on the workflow "
        "playground in the dashboard. This happens when its settings differ from the defaults, "
        "or it was edited after the dry run. Fix it, press ▶ Start, then press Enter."
    ),
    "ch5.readiness.diagnose": (
        "✗ SignalGate isn't stopping the attack yet. In the dashboard, check that the workflow "
        "is named playground, uses playground.action and playground.success, and is active; "
        "and that it has chapter 3's traffic. Then press Enter."
    ),
    "ch5.readiness.no_checks": (
        "✗ Your app didn't call check() for these visitors. Check app/routes.py, save, "
        "then press Enter."
    ),
    "ch5.readiness.again": "**Press Enter** to run the readiness round again.",
    "ch5.round.intro": (
        "Now the protected round: I'm sending {size:,} visitors to your sample app on your "
        "laptop, through your gate. Your app's SDK passes its check() and log() calls on to "
        "SignalGate."
    ),
    "ch5.round.again": "**Press Enter** to run the protected round again.",
    "ch5.tripwire": (
        "✗ Your gate isn't stopping the action: SignalGate answered block for {count:,} "
        "visitors whose action still happened. Make check() run before perform_action() "
        "and raise on block, save, then press Enter."
    ),
    "ch5.done": "✓ Protected round done.",
    # --- chapter 6: results ------------------------------------------------------------------------
    "ch6.ask": "**Press Enter** to see the results.",
    "results.frame": (
        " Round {round} · one burst · protection stage {stage} of 3            "
        "measured in your app\n"
        " ───────────────────────────────────────────────────────────────────────────────\n"
        "                               No SignalGate            With SignalGate\n"
        " Actions performed             {before_actions:>13,}{after_actions:>27,}\n"
        " Successes                     {before_successes:>13,}{after_successes:>27,}\n"
        " Conversion                    {before_conversion:>13.1%}{after_conversion:>27.1%}"
        "{trend}\n"
        " Real users blocked                        —{blocked_cell:>27}\n"
        " check() time                              —      {check_cell}\n"
        "                                                  (the rest is network between your\n"
        "                                                   machine and SignalGate)\n"
        " ───────────────────────────────────────────────────────────────────────────────"
    ),
    "results.frame.two": (
        " Round 1 and round 2 · one burst each · protection stage {stage} of 3   "
        "measured in your app\n"
        " ───────────────────────────────────────────────────────────────────────────────\n"
        "                               No SignalGate        Round 1        Round 2\n"
        " Actions performed             {before_actions:>13,}{r1_actions:>15,}{r2_actions:>15,}\n"
        " Successes                     {before_successes:>13,}{r1_successes:>15,}"
        "{r2_successes:>15,}\n"
        " Conversion                    {before_conversion:>13.1%}{r1_conversion:>15.1%}"
        "{r2_conversion:>15.1%}\n"
        " Real users blocked                        —{r1_blocked:>15}{r2_blocked:>15}\n"
        " check() time, round 2                     —      {check_cell}\n"
        "                                                  (the rest is network between your\n"
        "                                                   machine and SignalGate)\n"
        " ───────────────────────────────────────────────────────────────────────────────"
    ),
    "results.cell.of": "{blocked:,} of {total:,}",
    "results.cell.check": "median {check_ms:,.0f} ms · {inside_ms:,.0f} ms inside SignalGate",
    "results.cell.none": "—",
    "results.trend.up": " ▲",
    "results.trend.none": "",
    "results.prediction": (
        " Dry-run prediction (snapshot)   {conv_now:.1%} → {conv_pred:.1%}                "
        "(dashboard)"
    ),
    "results.indent": " {line}",
    "results.degraded": (
        "! {run} was cut short, or some check() calls failed and let visitors through, "
        "so its numbers may be less reliable."
    ),
    "results.run.learn": "The learning run",
    "results.run.round": "Round {round}",
    # The only depth and timing words allowed about live protection.
    "stage.0": "Protection is turning on.",
    "stage.partial": "This is protection stage {stage} of 3. Full protection within about an hour.",
    "stage.full": "Full protection (stage 3 of 3).",
    "results.framing": (
        "Real SignalGate verdicts on a synthetic attack we built to be clear-cut. "
        "Your traffic will differ."
    ),
    "ch6.rerun.offer": "**Want to see full protection?** You can re-run later. (yes/no)",
    "ch6.rerun.later": (
        "Great. Your workflow stays on. Close this window if you like, and type /continue "
        "when you're back."
    ),
    "ch6.rerun.anyway": "**Re-run now anyway?** (yes/no)",
    "ch6.rerun.go": (
        "Round 2: I'm sending {size:,} visitors to your sample app on your laptop, "
        "through your gate."
    ),
    "ch6.keep": (
        "**Keep the tutorial workflow running?** It only watches playground.* names and never "
        "touches your real traffic. You can stop it any time with ■ Stop in the dashboard. "
        "(yes/no)"
    ),
    "ch6.kept": "Your workflow playground stays on, and the dashboard keeps showing it at work.",
    "ch6.paused": "I've paused the workflow playground. ▶ Start switches it back on.",
    "ch6.complete_failed": (
        "◌ I couldn't reach SignalGate to finish the session. If you wanted the workflow paused, "
        "press ■ Stop in the dashboard."
    ),
    "ch6.diff": (
        "Taking it to your real app:\n"
        "  • Create a new workflow for your real app; don't edit playground.\n"
        "  • Rename the methods to names for your real action and success, in the form\n"
        "    <area>.<action> and <area>.<success>. Names starting with playground. are\n"
        "    reserved for the tutorial, so keep them out of your real app.\n"
        '  • Build the client with Client(api_key=os.environ["SIGNALGATE_API_KEY"]),\n'
        "    with no playground transport.\n"
        "  • Set uvicorn's forwarded_allow_ips (or your framework's proxy setting) to your\n"
        "    proxy's address.\n"
        "\n"
        '  - ev = sg_event(request, body.user, body.signalgate_log, "playground.action")\n'
        '  + ev = sg_event(request, body.user, body.signalgate_log, "<area>.<action>")\n'
        '  - ev = sg_event(request, body.user, body.signalgate, "playground.success")\n'
        '  + ev = sg_event(request, body.user, body.signalgate, "<area>.<success>")'
    ),
    "ch6.handoff": (
        "Next:\n"
        "  1. In your app's repo, run the Claude Code plugin: /plugin marketplace add\n"
        "     SignalGate/signalgate-claude-plugin, /plugin install signalgate@signalgate,\n"
        "     then /signalgate:integrate.\n"
        "  2. Docs: https://signalgate.ai/docs/backend\n"
        "  3. Book a 30-minute call: https://signalgate.ai/about/contact\n"
        "  4. The Quick Start steps you just completed count for this workspace. Your real\n"
        "     app's events will show up under your own method names."
    ),
    "ch6.done": "That's the tutorial. Type /quit to exit.",
    # --- achievements (one line each, when a step is done) and the completion card ---------
    "achievement.ch1": "★ Chapter 1 done. You've seen what the attack does to conversion.",
    "achievement.connected": "★ Connected. Your app now talks to SignalGate.",
    "achievement.ch3": (
        "★ Nailed it: both log() calls are in, and SignalGate accepted real events from them."
    ),
    "achievement.learned": "★ SignalGate has learned your traffic. Halfway there.",
    "achievement.prediction": "★ You've got a prediction. Now make it real.",
    "achievement.ch5": "★ Your check() gate works. Your code is now stopping the attack.",
    "achievement.results": "★ Done. You integrated SignalGate end to end.",
    "complete.card": (
        "What you built\n"
        "2 × log(), 1 × check(), 1 workflow\n"
        "From /start to the results: {minutes:,} min\n"
        "Next: bring it to your app. The steps are just below."
    ),
    "complete.card.untimed": (
        "What you built\n"
        "2 × log(), 1 × check(), 1 workflow\n"
        "Next: bring it to your app. The steps are just below."
    ),
    # --- /copy ----------------------------------------------------------------------------------
    "copy.done": "✓ Copied {count} snippets. Paste each one where its step says.",
    "copy.done_one": "✓ Copied snippet {index}. Paste it where step {index} says.",
    "copy.file": "There's no clipboard tool here, so I wrote the code to {path}. Copy it from there.",
    "copy.failed": (
        "◌ I couldn't copy the code or save it to .playground/snippets/. "
        "Copy it from the block above."
    ),
    "copy.none": "There's no code to copy in this step.",
    "copy.bad_index": (
        "This step has {count} snippets. Type /copy for all of them, or /copy 1 to /copy {count}."
    ),
    # --- hints for the online chapters ----------------------------------------------------------
    "hint.running": "Nothing to do yet: I'm working on it. Ctrl-C stops a run.",
    "hint.ch2.key": (
        "Open app/.env in your editor, replace PASTE_YOUR_API_KEY_HERE with your API key from "
        "API access (it starts with pk_live_), save, then press Enter."
    ),
    "hint.ch2.consent": "Type yes to agree and open your playground session, or /quit to stop.",
    "hint.ch2.session": "Press Enter to try opening the session again.",
    "hint.ch3.1": (
        "Two log() calls: one after the action has happened, one after the user succeeded. "
        "Each reads its own envelope, and only runs when that envelope is there."
    ),
    "hint.ch3.2": (
        "In app/routes.py: the first goes in action(), right below perform_action(body.user). "
        "The second goes in confirm(), right below the token check (after the raise line), "
        "before return."
    ),
    "hint.ch3.3": "The exact code, in app/routes.py:",
    "hint.ch3.learn": "Press Enter to send the learning run again.",
    "hint.ch3.topup": "Type yes to send the extra visitors, or no to wait.",
    "hint.ch4.dry_run": (
        "In the dashboard, press New flow, fill in the values above, press Create, "
        "then ▶ Dry Run. Then press Enter here."
    ),
    "hint.ch4.start": (
        "Press ▶ Start on the workflow playground in the dashboard, then press Enter."
    ),
    "hint.ch5.wait": "Check the workflow playground in the dashboard, then press Enter.",
    "hint.ch5.1": (
        "check() goes before the action, and stops the request only when its answer is block. "
        "It reads the envelope in signalgate."
    ),
    "hint.ch5.2": "In app/routes.py, at the start of action(), before perform_action(body.user).",
    "hint.ch5.3": "The exact code, in app/routes.py:",
    "hint.ch5.readiness": "Press Enter to run the readiness round again.",
    "hint.ch5.round": "Press Enter to run the protected round again.",
    "hint.ch6.results": "Press Enter to see the results.",
    "hint.ch6.rerun": "Type /continue when you want the re-run, or /quit to stop here.",
    "hint.ch6.keep": "yes keeps the workflow playground running; no pauses it.",
    "hint.done": "That's the whole tutorial. Type /quit to exit.",
    # --- /report --------------------------------------------------------------------------------
    "report.written": (
        "Wrote a support bundle to .playground/report.json: versions, where you are, check "
        "results and counters. No code, keys, envelopes or paths. Attach it to your bug report."
    ),
    "report.failed": "I couldn't write .playground/report.json.",
}


# Keys of code to paste, returned exactly as written.
SNIPPET_PREFIX = "snippet."

# The "Your turn" tasks: task key -> (title key, ((location key, snippet key), ...),
# footer key). Each edit is numbered in its location line.
TASKS: dict[str, tuple[str, tuple[tuple[str, str], ...], str]] = {
    "ch3.task": (
        "ch3.task.title",
        (
            ("ch3.edit.1", "snippet.ch3.import"),
            ("ch3.edit.2", "snippet.ch3.action_log"),
            ("ch3.edit.3", "snippet.ch3.success_log"),
        ),
        "ch3.task.footer",
    ),
    "ch5.task": (
        "ch5.task.title",
        (
            ("ch5.edit.1", "snippet.ch5.import"),
            ("ch5.edit.2", "snippet.ch5.gate"),
        ),
        "ch5.task.footer",
    ),
}

# A bold mark: two asterisks on each side of text that doesn't start or end
# with a space, within one line.
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")

Segment = tuple[str, bool]


def _spans(template: str) -> list[Segment]:
    """``template`` split into pieces, each with whether it is marked bold."""
    spans: list[Segment] = []
    at = 0
    for match in _BOLD.finditer(template):
        if match.start() > at:
            spans.append((template[at : match.start()], False))
        spans.append((match.group(1), True))
        at = match.end()
    if at < len(template):
        spans.append((template[at:], False))
    return spans


def _task_segments(key: str) -> list[Segment]:
    title, edits, footer = TASKS[key]
    parts = [*segments(title), ("\n", False)]
    for location, snippet in edits:
        parts += [*segments(location), ("\n\n", False), (TEXT[snippet], False), ("\n\n", False)]
    return [*parts, *segments(footer)]


def segments(key: str, **params: Any) -> list[Segment]:
    """The copy for ``key`` as pieces of text, each with whether it is bold.

    Joined, the pieces are exactly ``text(key, **params)``. Raises
    ``KeyError`` for an unknown key.
    """
    if key in TASKS:
        return _task_segments(key)
    template = TEXT[key]
    if key.startswith(SNIPPET_PREFIX):
        return [(template, False)] if template else []
    spans = _spans(template)
    if params:
        spans = [(piece.format(**params), bold) for piece, bold in spans]
    return [(piece, bold) for piece, bold in spans if piece]


def text(key: str, **params: Any) -> str:
    """Return the copy for ``key``, formatted with ``params``, as plain text.

    Raises ``KeyError`` for an unknown key, so a typo fails loudly in tests.
    """
    return "".join(piece for piece, _bold in segments(key, **params))


def task_plain(task_key: str) -> str:
    """A "Your turn" task as plain text: the title, then each edit's location
    line, a blank line, its code and a blank line, then the footer."""
    return text(task_key)


def snippet_text(task_key: str, index: int | None = None) -> str:
    """The code of a task, for ``/copy``.

    With no ``index``: every snippet, each after a ``# `` comment line naming
    where it goes, with a blank line between them. With ``index`` (1-based):
    that snippet alone. Raises ``IndexError`` for an index the task doesn't
    have.
    """
    _title, edits, _footer = TASKS[task_key]
    if index is not None:
        if not 1 <= index <= len(edits):
            raise IndexError(index)
        return TEXT[edits[index - 1][1]]
    return "\n\n".join(f"# {text(location)}\n{TEXT[snippet]}" for location, snippet in edits)


__all__ = [
    "SNIPPET_PREFIX",
    "TASKS",
    "TEXT",
    "segments",
    "snippet_text",
    "task_plain",
    "text",
]
