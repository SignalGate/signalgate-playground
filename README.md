# SignalGate Playground

**Watch 10,000 fake users beat a sample app. Then stop them.**

A short, guided story in your terminal. You start with an app that has no protection, watch an attack walk straight through it, and finish with the attack blocked: measured in numbers from your own machine.

- 🕹️ **It plays like a tutorial, not a homework assignment.** The console tells you what to do at each step, checks it, and tells you when you've nailed it.
- ✏️ **You touch one file.** A few short snippets in `app/routes.py`, shown exactly where they go. `/copy` puts them on your clipboard.
- 🔒 **Chapter 1 needs nothing**: no account, no API key, no network. You see the whole idea before you sign up for anything.
- ⏱️ **One sitting.** Most of the time is watching your traffic run.

## Start

Get the code:

```bash
git clone https://github.com/SignalGate/signalgate-playground.git
cd signalgate-playground
```

Then run it, whichever way you prefer:

**With pip** (needs Python 3.10 or newer: check with `python3 --version`):

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e . && playground
```

Next time, `source .venv/bin/activate && playground` is enough.

**With [uv](https://docs.astral.sh/uv/)** (it fetches the right Python for you):

```bash
uv run playground
```

The first run sets everything up, starts a small sample app on your laptop, and greets you. Type `/start`.

**You need:** macOS or Linux. From chapter 2 on, you also need a SignalGate account; the console gives you the sign-up link when you get there.

## What it looks like

When it's your turn, the console shows where each piece of code goes, then the code exactly as it goes in the file:

```text
━━ Chapter 3 of 6 · Teach SignalGate ●●●○○○ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SignalGate learns which actions turn into successes, so your app logs both.

╭ Your turn · edit app/routes.py ─────────────────────────────────────────────────╮
1. At the top of app/routes.py, with the other imports:

from app.signalgate_client import sg, sg_event

2. In action(), right below perform_action(body.user):

    ev = sg_event(request, body.user, body.signalgate_log, "playground.action")
    if ev:
        sg().log(ev)

3. In confirm(), right below the token check …
╰ /copy copies all three · /hint if you get stuck ────────────────────────────────╯
```

Save the file and press Enter. The console checks your work, and when it's right:

```text
✓ 9 passed · ! 0 notes · ◌ 0 not run — proven by 2 real events SignalGate accepted.
★ Nailed it: both log() calls are in, and SignalGate accepted real events from them.
```

## The six chapters

| | | |
|---|---|---|
| 1 | **Meet the attack** | 10,000 simulated visitors hit the sample app, and a live panel counts what happens. Only 20% of actions turn into a success, and nothing stopped the rest. *Offline, no account.* |
| 2 | **Connect** | You put your API key in `app/.env`. The console checks it, makes sure git can't leak it, and asks before it sends anything. |
| 3 | **Teach SignalGate** | You add two `log()` calls so SignalGate can learn what normal looks like. A couple of thousand visitors go through. |
| 4 | **Create the workflow** | You set it up in the SignalGate dashboard (the console gives you the exact clicks and values), run the dry run, and press Start. |
| 5 | **Turn on protection** | You add one `check()` call before the action. The console rehearses it, then confirms protection is live. |
| 6 | **Results** | Conversion before and after, the time `check()` added, and how many real users got caught. Then how to do the same in your real app. |

## You won't get stuck

The console is a guide, not a quiz.

- **Never guess the code.** `/hint` nudges you. Ask again for more, up to the exact code.
- **Every check explains itself.** It passes, or it tells you the file, the line, why it matters and how to fix it. Nothing fails silently.
- **Saving just works.** The console restarts the sample app with your change. If you break it, you get the error, the line and a hint.
- **Stop any time.** `/quit` saves your progress, and `/continue` picks it up next time.

## Commands

| | |
|---|---|
| Enter | "I've done it": check this step (`yes` works too) |
| `/hint` | a nudge for where you are; ask again for more |
| `/copy` | copy this step's code (`/copy 2` for just the second piece) |
| `/status` | what's done, your workflow, its protection stage |
| `/continue` | pick up where you left off |
| `/report` | a support bundle for a bug report (no code, keys or paths) |
| `/help` | every command |
| `/quit` | save and exit (Ctrl-D does the same) |

A question that ends in `(yes/no)` wants a `yes` or a `no`. Ctrl-C stops a run; press it again to exit.

<details>
<summary><b>The details</b>: flags, files, and input</summary>

### Flags

`playground --help` lists them all. The ones you might want:

- `--size N`: visitors per run through SignalGate (1,100–5,000; default 2,000)
- `--plain`: no colour or animation
- `--no-telemetry`: don't tell SignalGate which tutorial checks failed or your run's conversion before and after
- `--ci`: JSON lines for scripts (automatic when piped)
- `--yes`: auto-confirm prompts, for scripted runs
- `--until N`: stop after this chapter

### How input works

When the console asks you to do something ("Press Enter when all three edits are in"), press Enter once you have, and it checks your work. Keys pressed *during* a check or a run are dropped, so an extra Enter never starts the check twice. A question ending in `(yes/no)` wants a `yes` or a `no`; an empty line just asks again.

With no clipboard tool, `/copy` writes the code to `.playground/snippets/` and tells you where.

### Files the playground keeps

In `.playground/`:
- a lock file, so a second console can't run in the same folder;
- the sample app's output in `app.log` (check it if the app won't start);
- your progress in `state.json`;
- the `/report` bundle in `report.json`;
- `/copy` output in `snippets/`.

`app/.env` is created from `app/.env.example` on first run.

After a run through SignalGate, the console waits for the sample app's queued `log()` events to go out, with a progress bar. Ctrl-C skips the wait.

</details>

## Developing the playground itself

See [docs/development.md](docs/development.md).

## License

Apache-2.0. See [LICENSE](LICENSE), [NOTICE](NOTICE) and [TRADEMARK.md](TRADEMARK.md).
