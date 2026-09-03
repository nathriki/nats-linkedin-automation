# nats-linkedin-automation

An automated pipeline that finds Philippine news relevant to corporate/office
and BPO workers, turns it into a storytelling-style LinkedIn post, runs it
through a legal-risk check, and either publishes it automatically or sends it
to you on Telegram for a yes/no decision — with a photo attached, on a
schedule, without you touching a keyboard.

This document is the operator's guide: what it does, how to set it up, how to
run and monitor it day to day, and what still needs a human. The full build
history and every design decision behind it lives in
[`docs/designs/ph-corporate-worker-linkedin-pipeline.md`](docs/designs/ph-corporate-worker-linkedin-pipeline.md) —
read this file first, that one when you want the "why."

---

## 1. What this system does

Every weekday morning it runs a five-stage pipeline:

```
 news.raw          news.scored        post.drafted       post.pending_review
┌──────────┐      ┌───────────┐      ┌───────────┐          ┌───────────┐
│ SCRAPER  │ ───▶ │  SCORER   │ ───▶ │  DRAFTER  │ ───▶ ┌──▶ │ TELEGRAM  │
│ 3 news   │      │ keyword + │      │  Gemini   │      │    │ APPROVAL  │
│ sources  │      │ recency   │      │ storytell │      │    │   BOT     │
└──────────┘      └───────────┘      └───────────┘      │    └─────┬─────┘
                                            │            │          │ human
                                            ▼            │          │ tap
                                      ┌───────────┐      │          ▼
                                      │CLASSIFIER │ ─────┘    post.approved
                                      │legal-risk │ ───────────────┐
                                      │  gate     │                ▼
                                      └───────────┘          ┌───────────┐
                                                              │  POSTER   │
                                                              │ +photo →  │
                                                              │ LinkedIn  │
                                                              └───────────┘
```

1. **Scraper** pulls RSS from Philstar, GMA News, and Inquirer (title + short
   excerpt + link only — never the full article body).
2. **Scorer** is a cheap keyword + recency filter — no LLM call — that picks
   the top 1-3 candidates actually about labor/wage/RTO/BPO/contractualization
   topics, out of everything scraped.
3. **Drafter** (Gemini) writes a short, storytelling LinkedIn post told
   through a disclosed *fictional composite* worker (e.g. "Meet 'Jana', a
   composite drawn from real BPO worker stories") — never claiming to
   describe one real, identifiable person.
4. **Classifier** (Gemini + a deterministic keyword backstop) decides if the
   draft is safe to auto-publish or must go to a human first. It routes to
   review only if the post names a specific person with a claim attached,
   names a specific private company negatively, or alleges corruption/fraud/
   wrongdoing of any kind. This exists because a false negative here is a
   real cyber-libel exposure event, not just a quality miss.
5. **Poster** attaches a real stock photo (via Unsplash, picked by keyword
   match to the post) and publishes to your LinkedIn profile — but only
   after two more gates: a **kill-switch** (see §7) and a **dedup-key check**
   (never re-posts the same article twice, even if NATS redelivers a
   message).

Anything the classifier flags lands in your Telegram as a message with
**✅ Approve** / **❌ Reject** buttons. Nothing risky ever posts without you
tapping a button.

---

## 2. Architecture

Everything is a NATS JetStream message on one of six subjects, defined once
in [`schema/messages.py`](schema/messages.py) — every service imports its
message shapes from there, so that file is the single source of truth for
what a message looks like.

| Subject | Stream | Published by | Consumed by |
|---|---|---|---|
| `news.raw` | `NEWS` | scraper | scorer |
| `news.raw.failed` | `NEWS` | scraper | (nothing consumes this yet — see §9) |
| `news.scored` | `NEWS` | scorer | drafter |
| `post.drafted` | `POSTS` | drafter | classifier |
| `post.pending_review` | `POSTS` | classifier | approval bot |
| `post.approved` | `POSTS` | classifier *or* approval bot | poster |

Both streams keep 90 days of history (`LIMITS` retention) as an audit trail,
independent of whether a consumer has acknowledged a message — set up in
[`nats/setup_streams.py`](nats/setup_streams.py).

Each stage also keeps its own small state in a NATS KV bucket:

| Bucket | Used by | Purpose |
|---|---|---|
| `seen_news` | scraper | never republish the same article across runs |
| `drafter_state` | drafter | remembers recent disclosure phrasings so they don't repeat verbatim |
| `pending_approvals` | approval bot | the full pending post, keyed by `news_id`, so a Telegram button tap can act on it without holding the original NATS message unacked for hours |
| `posted_news` | poster | dedup — never post the same article twice |
| `kill_switch` | kill-switch | rolling flag timestamps |
| `pipeline_control` | kill-switch | the paused/unpaused flag itself |

**The services**, each a standalone script meant to run once and exit (except
the approval bot, which stays running):

| Service | File | What it does |
|---|---|---|
| Scraper | [`scraper/news_scraper.py`](scraper/news_scraper.py) | Fetches all 6 RSS feeds (3 sources × nation/business), retries transient failures 3× with backoff, dedups against `seen_news` |
| Scorer | [`scorer/heuristic_scorer.py`](scorer/heuristic_scorer.py) | Word-boundary keyword match × recency decay, publishes only the top 3 |
| Drafter | [`drafter/post_drafter.py`](drafter/post_drafter.py) | Calls Gemini (`gemini-3.5-flash-lite`) with a structured-output schema so the disclosure line is a guaranteed field, not hoped-for prose |
| Classifier | [`classifier/risk_classifier.py`](classifier/risk_classifier.py) | Gemini verdict + a hard keyword backstop (`corrupt`, `bribery`, `fraud`, `embezzle`, `graft`, ...) that can only push *toward* "risky", never override it toward "safe" |
| Approval bot | [`approval_bot/telegram_approval_bot.py`](approval_bot/telegram_approval_bot.py) | Always-on Telegram bot; delivers pending posts, handles Approve/Reject taps and the `/flag`, `/unpause`, `/status` commands |
| Poster | [`poster/linkedin_poster.py`](poster/linkedin_poster.py) | Picks a photo ([`imagegen/unsplash_photo_picker.py`](imagegen/unsplash_photo_picker.py)), uploads it via LinkedIn's Images API, publishes via `POST /rest/posts` |

[`scheduler/run_pipeline.py`](scheduler/run_pipeline.py) runs scraper →
scorer → drafter → classifier → poster in sequence as subprocesses. The
approval bot is deliberately **not** part of it — it's a persistent service,
not a periodic job (see §6).

---

## 3. One-time setup

### 3.1 Python environment

```
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
```

(`requirements-dev.txt` pulls in `requirements.txt` plus `pytest` /
`pytest-asyncio` for the test suite.)

### 3.2 NATS server

Install NATS with JetStream support. On Windows:

```
winget install NATSAuthors.NATSServer
```

Then, from the project root, with the server binary on your PATH (or its
full path):

```
nats-server -c nats/nats-server.conf
```

This runs on port `4222` (monitoring on `8222`), storing JetStream data
under `nats/data/jetstream/` (gitignored). Leave it running, then create the
two streams once:

```
.venv\Scripts\python.exe nats\setup_streams.py
```

(Safe to re-run any time — it updates existing streams rather than erroring.)

### 3.3 Secrets — `.env`

Copy [`.env.example`](.env.example) to `.env` (gitignored — never commit it)
and fill in four things. **None of these are printed or committed anywhere;
you fill them in yourself.**

| Variable | Where to get it |
|---|---|
| `LINKEDIN_CLIENT_ID` / `LINKEDIN_CLIENT_SECRET` | Create a LinkedIn developer app, request the `w_member_social` scope |
| `GEMINI_API_KEY` | [Google AI Studio](https://aistudio.google.com/apikey) — free tier covers the drafter/classifier text calls used here |
| `TELEGRAM_BOT_TOKEN` | Create a bot via [@BotFather](https://t.me/BotFather) (`/newbot`) |
| `TELEGRAM_CHAT_ID` | Message your new bot once, then run `.venv\Scripts\python.exe approval_bot\get_chat_id.py` — it prints the ID to paste in |
| `UNSPLASH_ACCESS_KEY` | Create a free "Demo" app at [unsplash.com/oauth/applications](https://unsplash.com/oauth/applications) (50 req/hr, no card) |

Then authorize LinkedIn once (opens a browser, you approve access, it saves
a token locally):

```
.venv\Scripts\python.exe auth\linkedin_oauth.py
```

This writes `.linkedin_token.json` (gitignored). **LinkedIn access tokens
expire in ~59 days and this flow has no refresh token** — see §9, you'll
need to re-run this script periodically.

---

## 4. Running it

**Every Python process in this project needs `PYTHONUTF8=1` set** — without
it, on a non-UTF-8 Windows locale, Gemini's non-ASCII output (em dashes,
etc.) gets silently corrupted before your code ever sees it. Set it once for
your user (`setx PYTHONUTF8 1`, new terminals only) or per-command as shown
below.

### Run the whole pipeline once

```
$env:PYTHONUTF8=1; .venv\Scripts\python.exe scheduler\run_pipeline.py
```

Runs scraper → scorer → drafter → classifier → poster in sequence, logging
everything to `scheduler\logs\run-<timestamp>.log` (gitignored). Each stage
runs regardless of whether an earlier one failed — a scraper hiccup shouldn't
block the classifier from processing drafts already queued from a prior run.
**Requires NATS to already be running** (see §3.2), and the approval bot
running if you want risky posts to actually reach you (see §6).

### Run one stage at a time (for testing/debugging)

```
$env:PYTHONUTF8=1
.venv\Scripts\python.exe scraper\news_scraper.py
.venv\Scripts\python.exe scorer\heuristic_scorer.py
.venv\Scripts\python.exe drafter\post_drafter.py
.venv\Scripts\python.exe classifier\risk_classifier.py
.venv\Scripts\python.exe poster\linkedin_poster.py
```

A stage with nothing new to do prints a clear "nothing to do" message rather
than silently exiting — a zero-news morning is expected and not an error.

---

## 5. Always-on pieces and scheduling

Two different mechanisms, because they do different jobs:

### NATS + the approval bot — start at Windows login

These must be running continuously (the bot needs to be able to react to a
button tap at any time, and the whole pipeline needs NATS reachable). They're
started via a copy of [`scheduler/start_background_services.vbs`](scheduler/start_background_services.vbs)
in your per-user Startup folder (`shell:startup`) — no admin rights needed.
The VBS launches [`scheduler/start_background_services.ps1`](scheduler/start_background_services.ps1)
completely hidden, which starts `nats-server` and then, 5 seconds later,
`approval_bot\telegram_approval_bot.py`, both logging to files under
`nats\` / `approval_bot\` (gitignored).

**This only runs when you actually log into Windows.** If your PC is off,
asleep, or you're not logged in, neither NATS nor the bot are running.

### The weekday 8am pipeline run — Task Scheduler

The recurring run is a registered Windows Scheduled Task,
`NatsLinkedInAutomation-Pipeline`, running
[`scheduler/register_scheduled_tasks.ps1`](scheduler/register_scheduled_tasks.ps1)'s
trigger: Mon-Fri at 8:00 AM, running `scheduler\run_pipeline.py`. Check its
status any time with:

```
Get-ScheduledTask -TaskName "NatsLinkedInAutomation-Pipeline"
Get-ScheduledTaskInfo -TaskName "NatsLinkedInAutomation-Pipeline"
```

**The two pieces depend on each other**: the 8am task needs NATS already
running, which only happens if you were logged into Windows at that point.
If you weren't, the task fires but fails at the scraper stage (can't reach
NATS) — check `scheduler\logs\` and `Get-ScheduledTaskInfo`'s
`LastTaskResult` (0 = success, anything else = a stage failed) to confirm.

To re-register the task (e.g. after moving the project, or if it was never
registered): run `scheduler\register_scheduled_tasks.ps1` from an elevated
PowerShell window (right-click → Run as Administrator) — registering a
Scheduled Task needs admin rights that a normal terminal doesn't have.

To stop the recurring automation: `Unregister-ScheduledTask -TaskName
"NatsLinkedInAutomation-Pipeline" -Confirm:$false`.

---

## 6. Operating the Telegram approval bot

When the classifier flags a post, you get a Telegram message like:

```
⚠️ Needs review

<the drafted post text>

Source: <link to the original article>
Reason: <why the classifier flagged it>
```

with **✅ Approve** / **❌ Reject** buttons underneath. Tapping **Approve**
publishes it to `post.approved` (the poster picks it up on its next run);
**Reject** just discards it. There's no timeout — a pending post can sit for
hours or days until you act on it.

Three commands, sent as regular Telegram messages to the bot:

- **`/status`** — replies with `Poster paused: True/False`.
- **`/flag <news_id> [reason]`** — flags an already-published post as
  actually problematic (something the classifier should have caught but
  didn't). **Two or more flags within a rolling 24-hour window
  auto-pauses all future auto-posting** — the poster does nothing on its
  next run until you explicitly resume it. This is the kill-switch
  ([`approval_bot/kill_switch.py`](approval_bot/kill_switch.py)): there's no
  automated way to detect a bad post after the fact, so this is the manual
  incident-response trigger.
- **`/unpause`** — resumes auto-posting after a kill-switch pause. Use this
  after you've checked the flagged post(s) on LinkedIn, deleted/edited as
  needed, and understood what went wrong.

---

## 7. Checking on things

**Is NATS up?**
```
Invoke-RestMethod http://localhost:8222/varz
```

**Are NATS and the bot currently running?**
```
Get-Process python, nats-server
```

**Did today's scheduled run happen, and what did it do?**
Check `scheduler\logs\run-<timestamp>.log` (one file per run, UTC timestamp
in the filename) — every stage's stdout/stderr is captured there, including
which items were found, drafted, classified, and posted (or why not).

**Did the scheduled task actually fire, and did it succeed?**
```
Get-ScheduledTaskInfo -TaskName "NatsLinkedInAutomation-Pipeline"
```
`LastTaskResult: 0` means success; anything else means a stage failed — check
the matching log file for which one and why.

---

## 8. Costs

Everything currently in use is free:

- **Gemini** (drafter + classifier text calls): free tier, `gemini-3.5-flash-lite`.
- **Unsplash** (photos): free "Demo" tier, 50 requests/hour.
- **NATS, Telegram, LinkedIn's API**: free.

**Gemini's image-generation models are NOT free** (confirmed against
Google's own pricing docs — there's no free tier for image models at all,
unlike text) — that's why photos come from Unsplash instead of an
AI-generated illustration. If you ever want AI-generated images specifically,
that requires enabling billing on the Google AI Studio project and swapping
`imagegen/unsplash_photo_picker.py` back out for a Gemini image call.

---

## 9. Known limitations — things only you can do

- **LinkedIn token expiry (~59 days, no refresh token)**: when
  `poster\linkedin_poster.py` starts failing with an auth error, re-run
  `.venv\Scripts\python.exe auth\linkedin_oauth.py` to get a fresh token —
  it opens a browser for a one-click re-approval.
- **`news.raw.failed` has no consumer yet**: if a source's feed goes down
  persistently, a message lands on this subject but nothing currently reads
  it or alerts you. Worth adding a small heartbeat/alert consumer if source
  reliability becomes a real problem.
- **No infra heartbeat monitor**: if NATS or the bot host goes down
  entirely, nothing currently pages you — the content-focused kill-switch in
  §6 only catches *bad posts*, not *the pipeline silently not running*.
  Check §7 periodically, especially after not using the machine for a while.
- **Metricool MCP integration**: scoped as a possible way to hand scheduling
  off to Metricool's own publish queue instead of this project's poster, but
  not yet connected — needs `claude mcp add --transport http metricool
  https://ai.metricool.com/mcp` run in a real terminal (not from inside a
  Claude Code session) followed by interactive `/mcp` authentication.

---

## 10. Running the tests

```
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest
```

Every service has a matching `test_*.py` file next to it — all mocked, no
real network/NATS/LinkedIn/Gemini calls, safe to run any time.

---

## Project structure

```
auth/               LinkedIn OAuth (one-time + re-auth)
schema/              messages.py -- the frozen NATS subject/payload schema, source of truth
scraper/             news_scraper.py -- Philstar + GMA News + Inquirer RSS -> news.raw
scorer/              heuristic_scorer.py -- cheap keyword+recency filter -> news.scored
drafter/              post_drafter.py -- Gemini storytelling drafts -> post.drafted
classifier/          risk_classifier.py -- legal-risk gate -> post.approved / post.pending_review
approval_bot/         telegram_approval_bot.py, kill_switch.py, get_chat_id.py
imagegen/             unsplash_photo_picker.py -- picks a photo for each post
poster/                linkedin_poster.py, dedup_store.py -- publishes to LinkedIn
scheduler/            run_pipeline.py + Task Scheduler/Startup-folder registration scripts
nats/                  nats-server.conf, setup_streams.py, smoke_test.py
eval/                  held-out eval sets for the drafter and classifier
docs/designs/          the living design doc -- full decision history and rationale
```
