# Grok-Powered Bug Triage → GitHub Issue Pipeline (v2.2)

> Multi-session implementation plan. Each **Session N** is a self-contained
> work unit. Pick up at the next unchecked session in any new chat.
>
> **What changed in v2:** semantic de-duplication of issues across re-tries,
> TOON-encoded payloads sent to Grok, premium Grok models, long timeouts, a
> daily ticket-creation quota, and a background poller that auto-triages
> error-bearing sessions instead of requiring a manual click.
>
> **What changed in v2.1:** every error row in the dashboard now shows
> per-error ticket status — issue number, link, created-at timestamp,
> recurrence count, and the action that produced it
> (`created` / `commented` / `deferred` / `skipped`).
>
> **What changed in v2.2:** GUI control panel with kill-switch + pause +
> "skip already-handled" guard, and full per-attempt telemetry — Grok latency
> (ms), prompt/completion/total tokens, model version, outcome
> (`created`/`commented`/`skipped`/`deferred`/`error`) and the `reason`
> when it didn't create a ticket. Aggregate stats endpoint + a "Stats" panel.

---

## Implementation log

- ✅ **Session 1** — env vars wired into [`app.py`](../../app.py), `toons>=0.5.0`
  added to [`requirements.txt`](../../requirements.txt), `state.json` load /
  save / quota-roll helpers (`_load_state`, `_save_state`, `_today_utc`,
  `_now_utc_iso`, `_roll_quota_if_new_day`), full default state shape
  (`controls`, `lastTicket`, `attempts[]`, `stats`, `poller`), state init at
  module load, `./data:/data` volume in
  [`docker-compose.yml`](../../docker-compose.yml), `/data` mkdir in
  [`Dockerfile`](../../Dockerfile), `GET /api/triage/health` route, and new
  env-var rows in the [`README`](../../README.md). Verified: `state.json`
  is created on first boot and `quota_used` survives a simulated restart.
- ✅ **Session 2** — `_build_bug_payload`, `_fetch_repo_context` (10 min
  in-memory cache), `_to_toon` (logs JSON-vs-TOON size reduction),
  `_call_grok` (returns telemetry dict with `model`, `grokMs`, `tokens`,
  `payloadBytes`, and surfaces failures as `error` instead of throwing),
  and `POST /api/triage/<doc_id>` (503 when `GROK_API_KEY` is empty, honors
  `controls.modelOverride`). Verified end-to-end with a mocked Grok response:
  payload is built, TOON-encoded, sent, and the route returns
  `{bug, triage, model, tokens, payloadBytes, grokMs, totalMs}`.
- ✅ **Session 3** — full dedup engine + GitHub client + daily quota +
  telemetry surface. New helpers in [`app.py`](../../app.py):
  `_normalize`, `compute_signature`, `_first_error_tool`, `_signature_for_doc`,
  GitHub clients (`_gh_headers`, `_github_search_issue_by_signature`,
  `_github_search_issue_by_doc`, `_github_get_issue`, `_github_create_issue`,
  `_github_comment_issue`), Markdown templates (`_format_issue_body` with both
  `grokcoder-debugger-id` and `grokcoder-signature` HTML markers,
  `_format_recurrence_comment`), state mutators (`_record_attempt` ring-buffer
  cap 200, bumps `stats.today` + `stats.lifetime`, updates `lastTicket` on
  `created`), `_gate` (kill-switch + skipProcessedDocs + skipKnownSignatures),
  `_hydrate_signature_from_github` (state-wipe rescue). New routes:
  `POST /api/issues` (full create-vs-comment-vs-defer pipeline),
  `GET /api/issues/status?docIds=…`, `GET /api/issues/recent`,
  `GET /api/triage/attempts?limit&outcome`, `GET /api/triage/stats`
  (today / last24h / lifetime + lastTicket + lastAttempt + quota),
  `GET /api/triage/last-ticket`, `GET/POST /api/triage/controls`,
  `POST /api/triage/kill`, `POST /api/triage/resume`. The existing
  `POST /api/triage/<doc_id>` now runs `_gate` before spending Grok tokens
  and records Grok errors as `outcome="error"` attempts. Verified with
  mocked Grok + GitHub: signature stability across re-runs, create,
  dup-docId → `skipped-doc-processed`, dup-sig → `commented` with
  `occurrences=2`, quota exhaustion → `deferred-quota` (HTTP 429),
  all three `status` shapes, attempts feed + outcome filter, stats
  totals + lastTicket, controls / killSwitch / resume gating both routes,
  and GitHub-search rehydrate after a state wipe still routes to the
  existing issue (`commented` on `#42`).
- ✅ **Session 4** — background poller thread + control endpoints. New
  module-level `_poller_state` (`lastRun`, `lastError`, `running`,
  `thread`) + `_poller_lock`. New helpers in [`app.py`](../../app.py):
  `_discover_error_docs(limit=50)` (N1QL `ANY t IN d.tools_called
  SATISFIES t.error IS NOT MISSING AND t.error != {} END`, ordered
  newest-first), `_process_poller_doc(doc, trigger)` (runs the full
  pipeline with **two pre-Grok short-circuits** — `_gate()` and the
  quota guard — and delegates the actual create-vs-comment-vs-defer
  decision to the existing `POST /api/issues` endpoint via Flask's
  `test_client`, so dedup logic isn't duplicated), `_poller_run_once()`
  (re-reads `controls.pollerEnabled` + `controls.killSwitch` every tick
  so GUI changes apply immediately; breaks early between docs if either
  flag flips; updates `state.poller.lastRun`/`lastError` + the in-process
  `_poller_state`; guards against concurrent runs via `_poller_lock`),
  `_poller_loop()` (sleeps `POLL_INTERVAL_SEC` between ticks),
  `_maybe_start_poller()` (no-op when `POLLER_ENABLED=false`, when
  `GROK_API_KEY` is empty, or when `GITHUB_TOKEN` is empty; idempotent
  if the thread is already alive). New routes:
  `GET /api/poller/state` (returns `enabled`, `killSwitch`, `intervalSec`,
  `lastRun`, `lastError`, `running`, `threadAlive`, `quotaUsed`,
  `quotaLimit`, `configured`), `POST /api/poller/pause` /
  `POST /api/poller/resume` (mirror `controls.pollerEnabled` + persist;
  `resume` also re-invokes `_maybe_start_poller()`), and
  `POST /api/poller/run-now` (triggers a single iteration synchronously,
  works even when the daemon thread isn't running — e.g. when env vars
  are missing or `POLLER_ENABLED=false`). `_maybe_start_poller()` is now
  called at import time so the poller also runs under a WSGI server.
  Verified with a mocked Grok + GitHub: `pause`/`resume` toggle
  `controls.pollerEnabled`, `run-now` while paused returns
  `{skipped:'disabled'}`, kill-switch yields `skipped-kill-switch`,
  exhausted quota yields `deferred` (and the docId lands in
  `state.deferred[]`), happy-path yields `created` (issue #42,
  `state.lastTicket.issueNumber=42`, `quota_used=1`), a second doc
  with the same signature yields `skipped-known-signature` and does
  **not** burn Grok or increment quota, and `/api/poller/state.lastRun`
  is stamped after `run-now` even when discovery returns zero docs.
- ✅ **Session 5** — full triage dashboard UI. In
  [`templates/index.html`](../../templates/index.html): new `.triage-bar`
  header strip (`#pollerBadge`, `#quotaBadge`, `#lastTicketBadge` + Pause /
  Resume / Run-now / 🛑 Kill switch / ⚙ Settings buttons + inline toast),
  collapsible `<details>` blocks for `📊 Triage stats`, `🧾 Recent triage
  attempts` (with `All|Created|Commented|Skipped|Deferred|Errors` filter
  pills + Refresh), and `⏳ Deferred queue`; new `Ticket` column added to
  the error table (colspan bumped 5 → 6 in every placeholder row); new
  `🐛 Triage → GitHub` tab inside the report modal; new `#controlsModal`
  bound to `GET/POST /api/triage/controls` with toggles for `killSwitch`,
  `pollerEnabled`, `issueCreationEnabled`, `skipProcessedDocs`,
  `skipKnownSignatures`, plus a `modelOverride` dropdown.
  In [`static/js/script.js`](../../static/js/script.js): `timeAgo(iso)`
  (`just now`, `Ns`, `Nm`, `Nh`, `Nd`), `fmtMs`, `fmtTokens`,
  `renderTicketCell(s, docId)` (4 visual states 🟢/🔁/⏳/⚪ — created/
  commented/deferred/none — with severity pill + `seen ×N` + `Triage now`
  button on uncovered docs), `loadIssueStatuses()` (batch
  `GET /api/issues/status?docIds=…` after every `loadData()`),
  `refreshTriageBar()` (polls `/api/poller/state` + `/api/triage/last-ticket`
  every 15 s, flips badges/colors and rewires the kill button between
  `🛑 Kill switch` ↔ `▶ Resume all`), `pollerPause/Resume/RunNow`,
  `killSwitch`/`resumeAll`, `loadStats() → renderStatsPanel()` (three
  cards: today / last24h / lifetime, plus a Last-ticket hero), `loadAttempts()
  → renderAttempts()` with outcome-bucketed border colors and full-attempt
  JSON tooltips, `setAttemptsFilter()`, `loadDeferred() → renderDeferred()`
  with per-row Force-triage buttons, `openControlsPanel`/`closeControlsPanel`
  and `saveControl(key, value)` (debounced 300 ms, posts only the changed
  field, shows a ✓ saved toast and updates `Last updated: … by …`),
  `triageRow(docId)` (manual flow: `POST /api/triage/<docId>` → if a
  triage came back, `POST /api/issues`; refreshes statuses + bar +
  attempts + stats afterward; handles gate short-circuits gracefully),
  and `renderTriageTab(docId, …)` for the modal sticky-header (three
  variants: ✅ Issue already filed / ⏳ Queued for triage / ⚪ none).
  `viewReport()` now also renders the Triage tab; `switchTab()` knows
  about `triage`. In
  [`static/css/style.css`](../../static/css/style.css): `.triage-bar`
  (+ `.killed` red variant), `.triage-badge ok|warn|fail|clickable`,
  `.triage-btn` (+ `.danger`), `.triage-toast`, `.muted`,
  `.collapse-panel`, `.sev sev-low|medium|high|critical` pills,
  `.ticket-cell`/`.ticket-deferred`/`.ticket-none`/`.ticket-btn`,
  `.stats-row`/`.stat-card.triage`/`.last-ticket-hero`,
  `.attempts-filter-bar`/`.filter-pill`/`.attempts-list`/`.attempt-row`
  with one `border-left` color per `outcome-created|commented|skipped|
  deferred|error`, `.controls-row`, `.modal-tab.triage-tab`, and
  `.triage-sticky-header` (default / `.deferred` / `.none`).
  Verified end-to-end with a flask `test_client`: `/` renders cleanly
  and contains every Session-5 element ID, the `Ticket` column appears
  in the error table, all read + write endpoints
  (`/api/poller/{state,pause,resume,run-now}`,
  `/api/triage/{stats,last-ticket,controls,attempts}`,
  `/api/issues/{status,recent}`) return 200, pause/resume actually flip
  `controls.pollerEnabled`, and every required JS function + CSS class
  is present in the served assets.
- ✅ **Session 6** — hardening, observability, deferred drain, and operator
  docs. New helper in [`app.py`](../../app.py): `_drain_deferred()` runs at
  the top of every `_poller_run_once()` tick. It calls
  `_roll_quota_if_new_day()` first so a fresh UTC day automatically reopens
  quota; snapshots `state.deferred[]`, clears it, then iterates oldest-first
  re-fetching each source doc via `_fetch_document()` and re-running the full
  `_process_poller_doc(doc, trigger='poller-drain')` pipeline. If quota
  re-fills mid-drain the unprocessed tail is pushed back onto
  `state.deferred[]`; missing source docs are dropped with a log line; per-
  outcome counters are returned. Quota rollover (#1) was already covered at
  every read site (`/api/issues`, `/api/triage/{stats,health}`,
  `_process_poller_doc`, `/api/poller/state`), and `_record_attempt()` now
  also calls `_roll_quota_if_new_day()` so per-day stats roll even when no
  HTTP route is hit. Structured logging (#3) is centralized inside
  `_record_attempt()` — one line per pipeline step: `[triage] doc=… sig=…
  action=… url=… grokMs=… tokens=… quota=N/M trigger=… reason=…` — replacing
  the ad-hoc print in `/api/issues`. Cost guard (#4) was moved into
  `_to_toon(payload, max_bytes=_MAX_PAYLOAD_BYTES)`: if the encoded TOON
  exceeds the cap it drops `chatTail` first and then aggressively truncates
  `toolErrors[*].error` to 512 B and re-encodes, logging each pass. README
  (#5) gained an "Auto-Triage Pipeline" section covering env vars, the two-
  layer dedup (`processed_docs` + signature), daily quota + deferred drain,
  the 32 KB cost guard, the full list of inspection endpoints, the
  structured-log format, the outcome vocabulary, and Docker persistence.
  Dockerfile (#6) now `chmod 0777 /data` and declares it as a `VOLUME` so
  `state.json` survives `docker compose down`. New helper script
  [`scripts/dump_state.py`](../../scripts/dump_state.py) (#7, optional)
  pretty-prints `state.json` — quota, controls, top-20 signatures by
  occurrence count, deferred queue, last 15 attempts, and lifetime stats.
  `/api/poller/run-now` and the periodic tick now also report
  `deferredDrain: {drained, outcomes}` so the GUI / operator can confirm a
  drain happened. Verified end-to-end with a smoke test: with
  `DAILY_ISSUE_QUOTA=2`, priming `state.deferred=[{docId:'foo',…}]` and
  `quota_used=5`, `_drain_deferred()` correctly returned
  `{'drained':0,'skipped':'quota-still-full'}`; clearing the deferred queue
  returned `{'drained':0,'skipped':'empty'}`; the cost guard reduced a 50 KB
  payload to 603 B in two passes; `_record_attempt(outcome='created')`
  emitted the expected structured `[triage] …` line; and
  `scripts/dump_state.py` rendered the state file end-to-end.

---

## Goal

From the **grokCoder Debugger** running locally in Docker:

1. **Poll** Couchbase for sessions whose `tools_called[*].error` is non-empty.
2. For each new error session, build a clean **TOON-encoded** bug payload.
3. Send it (+ target-repo context) to the **Grok API** (premium model, long timeout).
4. Get back structured `{problems, rca, fixes, severity, signature}`.
5. **Deduplicate** against existing issues using both an exact `docId` marker
   **and** a semantic `signature` (hash of normalized error fingerprint).
6. If unique **and** under today's quota (≤10 / day), create a GitHub Issue
   in [`Fujio-Turner/continue-vscode-todos-tool`](https://github.com/Fujio-Turner/continue-vscode-todos-tool/issues).
7. Otherwise: link the existing issue (post a comment) or queue for tomorrow.
8. UI still supports a manual **"Triage this one now"** button for one-offs.

## High-Level Architecture

```diagram
                                        ╭─────────────────────────╮
                                        │  state.json             │
                                        │  • daily counter        │
                                        │  • signature → issue#   │
                                        │  • last-poll timestamp  │
                                        ╰────────────┬────────────╯
                                                     │
╭────────────────╮      ╭────────────────────────────┴───╮      ╭────────────────╮
│ Couchbase      │◀─[1]─┤   Background poller (thread)   │──[5]─▶│  GitHub API   │
│ tools_called[] │      │   ─ every POLL_INTERVAL_SEC    │      │  search +     │
│   .error != {} │      │   ─ build TOON bug payload     │      │  POST issue   │
╰────────────────╯      │   ─ compute signature          │      ╰───────┬───────╯
                        │   ─ dedupe vs state + GitHub   │              │
                        │   ─ check daily quota          │              │
                        │   ─ call Grok (5+ min timeout) │              │
                        ╰──────┬──────────────┬──────────╯              │
                               │ TOON         │ JSON                    │
                               ▼              ▼                         │
                        ╭──────────────╮  ╭───────────────╮            │
                        │ Grok API     │  │ Flask UI      │◀───────────┘
                        │ grok-4-…     │  │ /api/triage   │  issue url
                        ╰──────────────╯  │ /api/poller/* │
                                          ╰───────────────╯
```

## Tech Decisions (locked in up-front)

| Concern | Decision |
|---|---|
| Grok model | `GROK_MODEL` env, default `grok-4-latest`. Set to `grok-4.2.0` or `grok-4.3.0` in `.env` to use the premium tier. |
| Grok endpoint | `GROK_API_URL` env, default `https://api.x.ai/v1/chat/completions`. |
| Auth | `GROK_API_KEY` → `Authorization: Bearer …` |
| Request format | Body is JSON (xAI requires JSON), but the **payload inside `messages[].content`** is **TOON-encoded** via the [`toons`](https://pypi.org/project/toons/) PyPI library. |
| Timeout | `GROK_TIMEOUT_SEC` env, default **`360`** (6 minutes). Use `requests.post(..., timeout=GROK_TIMEOUT_SEC)`. |
| GitHub auth | Fine-grained PAT in `GITHUB_TOKEN` with `Issues: Read & Write` on target repo. |
| Target repo | `GITHUB_REPO=Fujio-Turner/continue-vscode-todos-tool` |
| Daily ticket quota | `DAILY_ISSUE_QUOTA` env, default **`10`**. Persisted in `state.json`. Window: UTC midnight → midnight. |
| Dedup keys | Two-layer: (a) exact `docId` marker, (b) **`signature`** = SHA-1 of `(tool_name + normalized_error_message + normalized_first_stack_frame + user_prompt_topic)`. |
| Poller | Background `threading.Thread` started on app boot. Interval `POLL_INTERVAL_SEC`, default `60`. Can be paused/resumed via `/api/poller/state`. |
| Persistence | `state.json` (mounted as volume in Docker). Keys: `quota_date`, `quota_used`, `signatures` (sig → {docId, issueUrl, count, lastSeen}), `processed_docs`, `pollerEnabled`. |
| Backoff | If Grok 429 or 5xx → exponential backoff, max 3 retries, then mark doc as "deferred" and skip until next day. |

## Idempotency & dedup logic (the core change)

```diagram
                ┌─ already in state.processed_docs[docId]? ──── YES → skip
                │
new doc ──▶ compute signature ──▶ in state.signatures? ──── YES ─┐
                │                                                 │
                NO                                                ▼
                │                                       (a) bump signatures[sig].count
                ▼                                       (b) post comment on existing
        GitHub search "grokcoder-signature:<sig>"          issue: "Re-occurred in
        in open+closed issues, body or label                docId=<new>, count=N"
                │                                       (c) record docId as processed
        ┌───────┴────────┐                              (d) do NOT consume quota
        │ found?         │
        ├ YES ──▶ link, comment, record, no-quota
        └ NO  ──▶ Grok triage → quota check
                         │
                  quota_used < DAILY_ISSUE_QUOTA?
                         │
                  YES ──▶ create issue with body containing
                          both markers:
                            <!-- grokcoder-debugger-id:<docId> -->
                            <!-- grokcoder-signature:<sig> -->
                          plus label `grok-sig:<sig[:8]>`
                  NO  ──▶ defer: store in state.deferred[], skip
```

The **signature** is what catches "I opened a fresh chat and tried again". The
exact `docId` is what catches "I literally re-ran the same session".

### Signature builder (pseudocode)

```python
import hashlib, re
def _normalize(s: str) -> str:
    s = s.lower()
    s = re.sub(r'/[\w./-]+:\d+:\d+', '<loc>', s)      # file:line:col
    s = re.sub(r'\b0x[0-9a-f]+\b', '<hex>', s)         # pointers
    s = re.sub(r'\b\d{6,}\b', '<num>', s)              # long ids
    s = re.sub(r'"[^"]{12,}"', '"<str>"', s)           # long quoted strings
    s = re.sub(r'\s+', ' ', s).strip()
    return s

def compute_signature(tool_call, user_prompt):
    err = tool_call.get('error') or {}
    msg = err.get('message') or json.dumps(err, sort_keys=True)[:500]
    stack = (err.get('stack') or '').splitlines()[:1]
    topic = (user_prompt or '')[:80]
    raw = '|'.join([
        tool_call.get('name', ''),
        _normalize(msg),
        _normalize(''.join(stack)),
        _normalize(topic),
    ])
    return hashlib.sha1(raw.encode()).hexdigest()
```

---

# Session 1 — Configuration, deps, and `state.json`

**Outcome:** App boots with new env vars + `toons` installed + `state.json`
loaded/persisted on disk. No behavior change yet.

### Tasks

1. Add to [`requirements.txt`](../../requirements.txt):
   ```
   toons>=0.5.0
   ```
2. Extend [`.env.example`](../../.env.example):
   ```env
   # --- Grok / xAI ---
   GROK_API_KEY=
   GROK_MODEL=grok-4-latest          # bump to grok-4.2.0 or grok-4.3.0 for premium
   GROK_API_URL=https://api.x.ai/v1/chat/completions
   GROK_TIMEOUT_SEC=360              # 6 minutes — premium models can be slow

   # --- GitHub ---
   GITHUB_TOKEN=
   GITHUB_REPO=Fujio-Turner/continue-vscode-todos-tool

   # --- Triage pipeline ---
   DAILY_ISSUE_QUOTA=10              # max GitHub issues to create per UTC day
   POLL_INTERVAL_SEC=60              # background poller cadence
   POLLER_ENABLED=true               # start the poller on boot
   STATE_FILE=/data/state.json       # persisted dedup/quota state
   ```
3. In [`docker-compose.yml`](../../docker-compose.yml): add a `./data:/data`
   volume mount so `state.json` survives container restarts. Pass all new env
   vars through.
4. In [`app.py`](../../app.py): read all new env vars near the existing block.
5. New module-level helpers in `app.py`:
   ```python
   _state_lock = threading.Lock()
   def _load_state() -> dict: ...
   def _save_state(state: dict) -> None: ...
   def _today_utc() -> str: ...      # "YYYY-MM-DD"
   def _roll_quota_if_new_day(state) -> None:
       if state.get('quota_date') != _today_utc():
           state['quota_date'] = _today_utc()
           state['quota_used'] = 0
   ```
   Default state (rich enough for the dashboard to render per-row badges):
   ```json
   {
     "quota_date": "1970-01-01",
     "quota_used": 0,
     "signatures": {
       "<sha1>": {
         "issueNumber": 42,
         "issueUrl": "https://github.com/.../issues/42",
         "issueTitle": "...",
         "firstDocId": "doc-abc",
         "firstSeenAt": "2026-05-16T12:00:00Z",
         "lastDocId":  "doc-xyz",
         "lastSeenAt": "2026-05-16T18:33:00Z",
         "count": 3,
         "severity": "high",
         "labels": ["bug","grok-triage","severity:high","grok-sig:abcd1234"]
       }
     },
     "processed_docs": {
       "doc-abc": {
         "signature":   "<sha1>",
         "issueNumber": 42,
         "issueUrl":    "https://github.com/.../issues/42",
         "action":      "created",
         "at":          "2026-05-16T12:00:00Z"
       }
     },
     "deferred": [
       { "docId": "doc-def", "signature": "<sha1>",
         "at": "2026-05-16T19:00:00Z", "reason": "quota" }
     ],

     "controls": {
       "pollerEnabled":   true,
       "issueCreationEnabled": true,
       "skipKnownSignatures": true,
       "skipProcessedDocs":   true,
       "modelOverride":   null,
       "killSwitch":      false,
       "updatedAt":       "2026-05-16T12:00:00Z",
       "updatedBy":       "ui"
     },

     "lastTicket": {
       "issueNumber": 42,
       "issueUrl":    "https://github.com/.../issues/42",
       "issueTitle":  "...",
       "docId":       "doc-abc",
       "signature":   "<sha1>",
       "severity":    "high",
       "at":          "2026-05-16T12:00:00Z",
       "model":       "grok-4.3.0",
       "grokMs":      48213,
       "tokens":      { "prompt": 3120, "completion": 980, "total": 4100 }
     },

     "attempts": [
       {
         "id":         "att-2026-05-16T12-00-00Z-doc-abc",
         "at":         "2026-05-16T12:00:00Z",
         "docId":      "doc-abc",
         "signature":  "<sha1>",
         "outcome":    "created",
         "reason":     null,
         "issueNumber": 42,
         "issueUrl":   "https://github.com/.../issues/42",
         "severity":   "high",
         "model":      "grok-4.3.0",
         "grokMs":     48213,
         "githubMs":   612,
         "totalMs":    49102,
         "tokens":     { "prompt": 3120, "completion": 980, "total": 4100 },
         "payloadBytes": { "jsonEquivalent": 18432, "toonSent": 9210 },
         "trigger":    "poller"
       }
     ],

     "stats": {
       "today": {
         "date": "2026-05-16",
         "attempts": 7, "created": 3, "commented": 2,
         "skipped": 1, "deferred": 1, "errors": 0,
         "tokensTotal": 24310, "grokMsTotal": 312045
       },
       "lifetime": {
         "attempts": 152, "created": 48, "commented": 71,
         "skipped": 22, "deferred": 6, "errors": 5,
         "tokensTotal": 612433, "grokMsTotal": 7240133
       }
     },

     "pollerEnabled": true
   }
   ```
   - `controls.killSwitch=true` → **no** Grok calls and **no** GitHub writes
     happen, period. Highest-priority gate, checked before everything else.
   - `controls.issueCreationEnabled=false` → Grok triage may still run for
     preview, but `POST /api/issues` returns `skipped-issue-creation-disabled`
     and the poller short-circuits before calling GitHub.
   - `controls.skipKnownSignatures=true` (default) is the "don't recreate
     tickets I've already done" guard. If `false`, the comment-on-dup path
     is also disabled (every signature, even repeated, would be sent to Grok —
     **expensive**, use only for debugging).
   - `controls.skipProcessedDocs=true` (default) skips any docId already in
     `processed_docs`.
   - `controls.modelOverride` (e.g. `"grok-4-latest"`) wins over `GROK_MODEL`
     env var, so the GUI can dial down to a cheaper model without restarting.

   Every code path that mutates state **must** stamp `at` / `firstSeenAt` /
   `lastSeenAt` (UTC, ISO-8601) and store `action` (one of
   `created` | `commented` | `deferred` | `skipped-doc-processed` |
   `skipped-known-signature` | `skipped-issue-creation-disabled` |
   `skipped-kill-switch` | `error`).
   `attempts` is a ring buffer capped at 200 most recent entries; older
   data is rolled up into `stats.lifetime` before being dropped.
6. New route `GET /api/triage/health`:
   ```json
   {
     "grok":  { "configured": bool, "model": "...", "timeoutSec": 360 },
     "github":{ "configured": bool, "repo": "..." },
     "quota": { "used": N, "limit": 10, "date": "YYYY-MM-DD" },
     "poller":{ "enabled": bool, "intervalSec": 60, "lastRun": "...", "lastError": null }
   }
   ```
7. README: add new env-var rows.

### Verify

- `docker compose up --build` boots cleanly.
- `curl http://localhost:7777/api/triage/health` returns all four sections.
- Kill+restart container → `state.json` survives, quota number stays put.

---

# Session 2 — TOON payload builder + Grok client

**Outcome:** A function pipeline that takes a session doc → bug payload (TOON
string) → Grok call → parsed JSON result. Manual `POST /api/triage/<docId>`
endpoint exposes it; no auto-poller, no GitHub yet.

### Tasks

1. **`_build_bug_payload(session_doc) -> dict`** — returns a Python dict (not
   yet TOON-encoded) shaped for compact tabular encoding:
   ```python
   {
       "docId":  "...",
       "src":    "...",
       "userPrompt": "...(<=4KB)",
       "toolErrors": [          # uniform array → TOON tabular!
           {"i": 0, "name": "...", "error": "...short json...", "execTime": 1.2}
       ],
       "chatTail": [...last 6 entries, truncated...]
   }
   ```
   Truncate any string field >4 KB with `"...[truncated]"`. Cap total <32 KB.

2. **`_fetch_repo_context(repo) -> dict`** — cached 10 min in-memory:
   ```python
   {
       "repo":   "Fujio-Turner/continue-vscode-todos-tool",
       "branch": "main",
       "readme": "...(first 8 KB)...",
       "tree":   ["src/extension.ts", "src/...", ...top 200 paths]
   }
   ```
   Endpoints: `GET /repos/{r}/readme`, `GET /repos/{r}/git/trees/{branch}?recursive=1`.

3. **`_to_toon(payload) -> str`** — wrapper over `toons.dumps(payload)`. Log
   `len(json.dumps(payload))` vs `len(toon_str)` so we can see the savings.

4. **`_call_grok(bug_payload: dict, repo_context: dict) -> dict`**:
   ```python
   import toons
   bug_toon  = toons.dumps(bug_payload)
   repo_toon = toons.dumps(repo_context)

   system = (
       "You are a senior code reviewer for the repo named in 'repo.repo'. "
       "Inputs are TOON-encoded (Token-Oriented Object Notation). "
       "TOON uses key:value with indentation for nesting; arrays like "
       "items[N]{a,b}: followed by CSV rows. "
       "Return STRICT JSON (NOT TOON) with keys: "
       "problems[], rca, fixes[{file?,description,patch?}], "
       "suggestedTitle, severity(low|medium|high|critical), "
       "signatureHint (1-line normalized fingerprint of the failure)."
   )
   user = f"### BUG (TOON)\n{bug_toon}\n\n### REPO (TOON)\n{repo_toon}"
   body = {
       "model": GROK_MODEL,
       "messages": [{"role":"system","content":system},
                    {"role":"user","content":user}],
       "response_format": {"type":"json_object"},
       "temperature": 0.2,
   }
   r = requests.post(GROK_API_URL, headers={
           "Authorization": f"Bearer {GROK_API_KEY}",
           "Content-Type": "application/json",
       }, json=body, timeout=GROK_TIMEOUT_SEC)
   r.raise_for_status()
   content = r.json()["choices"][0]["message"]["content"]
   return json.loads(content)
   ```
   On `json.JSONDecodeError`, return `{"_parseError": str(e), "_raw": content}`.
   Log `r.json().get("usage", {})` for token accounting.

5. **`POST /api/triage/<doc_id>`** — fetch doc → build payload → call Grok →
   return `{"bug": payload, "triage": result, "tokens": {...}, "ms": ...}`.
   503 if `GROK_API_KEY` is empty. Make `_call_grok` return a **telemetry
   dict** that the route + Session 3 will both consume:
   ```python
   def _call_grok(bug, repo):
       t0 = time.monotonic()
       ...
       resp_json = r.json()
       usage = resp_json.get("usage", {}) or {}
       return {
           "triage":  json.loads(content),
           "model":   resp_json.get("model") or GROK_MODEL,   # actual model echoed back by xAI
           "grokMs":  int((time.monotonic() - t0) * 1000),
           "tokens": {
               "prompt":     usage.get("prompt_tokens", 0),
               "completion": usage.get("completion_tokens", 0),
               "total":      usage.get("total_tokens", 0),
           },
           "payloadBytes": {
               "jsonEquivalent": len(json.dumps(bug)) + len(json.dumps(repo)),
               "toonSent":       len(bug_toon) + len(repo_toon),
           },
       }
   ```
   On any failure also return a `"error": "..."` field; the caller turns this
   into an `attempts[]` entry with `outcome="error"` so failures are still
   visible in the stats UI (not silently lost).

### Verify

- `curl -X POST http://localhost:7777/api/triage/<known-error-doc> | jq` returns
  `triage.problems`, `triage.rca`, `triage.fixes`, `triage.severity`.
- Flask log shows the **TOON-vs-JSON size reduction** for that payload.
- Setting `GROK_MODEL=grok-4.2.0` (or `4.3.0`) still works; the long timeout
  prevents premature failures.

---

# Session 3 — Dedup engine + GitHub Issue creator + daily quota

**Outcome:** A `POST /api/issues` endpoint that, given a triage result, decides:
create-new vs comment-on-existing vs over-quota — and persists everything to
`state.json`.

### Tasks

1. **`compute_signature(tool_call, user_prompt)`** (see pseudocode above).
   For a session with multiple errors, compute one signature per error and
   pick the **first** (or the highest-severity one).

2. **GitHub helpers** (all use `Authorization: Bearer {GITHUB_TOKEN}` +
   `Accept: application/vnd.github+json`):
   - `_github_search_issue_by_signature(repo, sig) -> Optional[issue]`:
     `GET /search/issues?q=repo:{repo}+"grokcoder-signature:{sig}"`. Return
     first hit (open OR closed) — closed-but-recurring should still be linked,
     not duplicated.
   - `_github_search_issue_by_doc(repo, doc_id) -> Optional[issue]`: same but
     with `grokcoder-debugger-id:{doc_id}`.
   - `_github_create_issue(repo, title, body, labels) -> dict`.
   - `_github_comment_issue(repo, number, body) -> dict`.

3. **`_format_issue_body(triage, bug, sig)`** — Markdown body with both markers:
   ```md
   > Auto-filed by **grokCoder Debugger**.
   > <!-- grokcoder-debugger-id:<docId> -->
   > <!-- grokcoder-signature:<sig> -->
   > First seen in session: `<docId>`

   ## Problems
   - …

   ## Root-Cause Analysis
   <triage.rca>

   ## Suggested Fix(es)
   - **file:** `src/foo.ts`
     <description>
     ```diff
     <patch>
     ```

   ---
   <details><summary>Original bug payload (TOON)</summary>

   ```toon
   <toons.dumps(bug)>
   ```
   </details>
   ```

4. **`_format_recurrence_comment(doc_id, count, src)`**:
   ```md
   🔁 **Re-occurred** in a new debugger session.
   - docId: `<doc_id>`
   - src: `<src>`
   - total observed occurrences: **<count>**
   - (filed automatically by grokCoder Debugger)
   ```

5. **`POST /api/issues`** — body: `{ "docId", "triage", "bug", "signature"? }`.
   Server recomputes `signature` if not supplied (defense in depth). Flow:

   ```python
   with _state_lock:
       state = _load_state()
       _roll_quota_if_new_day(state)

       # (a) exact same docId already processed?
       if doc_id in state['processed_docs']:
           return jsonify({"status":"skipped-doc-processed",
                           "url": state['processed_docs'][doc_id].get('issueUrl')})

       sig_entry = state['signatures'].get(signature)

       # (b) signature seen before → comment, do not consume quota
       if sig_entry:
           issue = _github_get_issue(repo, sig_entry['issueNumber'])  # ensure still exists
           _github_comment_issue(repo, sig_entry['issueNumber'],
                                 _format_recurrence_comment(...))
           sig_entry['count'] += 1
           sig_entry['lastSeen'] = doc_id
           state['processed_docs'][doc_id] = {"issueUrl": sig_entry['issueUrl'],
                                              "reason":"dup-signature"}
           _save_state(state)
           return jsonify({"status":"commented", "url": sig_entry['issueUrl'],
                           "occurrences": sig_entry['count']})

       # (c) belt-and-suspenders: search GitHub by signature in case state.json was wiped
       existing = _github_search_issue_by_signature(repo, signature)
       if existing:
           state['signatures'][signature] = {...from existing...}
           # fall through to "commented" branch above logic
           ...

       # (d) net-new → quota check
       if state['quota_used'] >= DAILY_ISSUE_QUOTA:
           state['deferred'].append({"docId": doc_id, "signature": signature,
                                     "at": datetime.utcnow().isoformat()})
           _save_state(state)
           return jsonify({"status":"deferred-quota",
                           "used": state['quota_used'],
                           "limit": DAILY_ISSUE_QUOTA}), 429

       # (e) create
       issue = _github_create_issue(repo, triage['suggestedTitle'],
                                    _format_issue_body(triage, bug, signature),
                                    labels=["bug","grok-triage",
                                            f"severity:{triage.get('severity','medium')}",
                                            f"grok-sig:{signature[:8]}"])
       state['signatures'][signature] = {
           "issueNumber": issue['number'], "issueUrl": issue['html_url'],
           "firstDocId": doc_id, "count": 1, "lastSeen": doc_id,
       }
       state['processed_docs'][doc_id] = {"issueUrl": issue['html_url'],
                                          "reason":"created"}
       state['quota_used'] += 1
       _save_state(state)
       return jsonify({"status":"created", "url": issue['html_url'],
                       "number": issue['number'],
                       "quotaUsed": state['quota_used'],
                       "quotaLimit": DAILY_ISSUE_QUOTA})
   ```

6. **Read-only status endpoints** (powers the dashboard badges):

   - `GET /api/issues/status?docIds=a,b,c` →
     ```json
     {
       "a": {
         "hasIssue": true,
         "action": "created",
         "issueNumber": 42,
         "issueUrl": "https://github.com/.../issues/42",
         "issueTitle": "...",
         "signature": "<sha1>",
         "firstSeenAt": "2026-05-16T12:00:00Z",
         "lastSeenAt":  "2026-05-16T18:33:00Z",
         "count": 3,
         "severity": "high"
       },
       "b": { "hasIssue": false, "deferred": true, "reason": "quota" },
       "c": { "hasIssue": false }
     }
     ```
     Implementation: for each `docId`, look up `state.processed_docs[docId]`,
     then resolve `signature` → `state.signatures[sig]` for the rich fields.
     If the doc is in `state.deferred`, return `deferred: true` instead.

   - `GET /api/issues/recent?limit=20` → list of the most recent
     ticket actions (created OR commented), newest first, for a
     "Recent triage activity" panel:
     ```json
     [
       { "at":"...","action":"created","docId":"...","signature":"...",
         "issueNumber":42,"issueUrl":"...","issueTitle":"...","severity":"high","count":1 },
       { "at":"...","action":"commented","docId":"...","signature":"...",
         "issueNumber":42,"issueUrl":"...","count":3 }
     ]
     ```
     This is now sourced from `state.attempts[]` (filtered to
     `outcome in ('created','commented')` for backwards-compat) — see
     `/api/triage/attempts` below for the full unfiltered feed.

7. **Telemetry / "did we just create a ticket?" endpoints:**

   - `GET /api/triage/attempts?limit=50&outcome=any` → full per-attempt feed,
     newest first, with `outcome`, `reason`, `model`, `grokMs`, `tokens`,
     `payloadBytes`, `trigger`, `issueNumber|null`, `issueUrl|null`. Optional
     `outcome` filter: `created|commented|skipped|deferred|error|any`.

   - `GET /api/triage/stats` → ready-to-render aggregates from `state.stats`
     plus a derived `last24h` block (computed on the fly from `attempts[]`):
     ```json
     {
       "today":    { "attempts":7,"created":3,"commented":2,"skipped":1,
                     "deferred":1,"errors":0,"tokensTotal":24310,
                     "grokMsTotal":312045,"avgGrokMs":44578,
                     "tokensByModel":{"grok-4.3.0":24310} },
       "last24h":  { "...same shape..." },
       "lifetime": { "...same shape..." },
       "lastTicket": { "<state.lastTicket>" },
       "lastAttempt": { "<attempts[0]>" },
       "quota":    { "used":3, "limit":10, "date":"2026-05-16" }
     }
     ```

   - `GET /api/triage/last-ticket` → just `state.lastTicket` (used by the
     dashboard header strip so the user always sees "✅ Last ticket created:
     #42 · 3h ago · grok-4.3.0 · 4.1k tok · 48s").

8. **Controls endpoints (the "levers"):**

   - `GET  /api/triage/controls` → current `state.controls` block.
   - `POST /api/triage/controls` body: any subset of
     `{pollerEnabled, issueCreationEnabled, skipKnownSignatures,
     skipProcessedDocs, modelOverride, killSwitch}`. Updates persist
     immediately, stamp `updatedAt`/`updatedBy="ui"`, and the running poller
     loop picks them up on its next iteration (it re-reads state every cycle).
   - `POST /api/triage/kill`  → shortcut: `killSwitch=true`,
     `pollerEnabled=false`, `issueCreationEnabled=false`. Big red button.
   - `POST /api/triage/resume` → opposite of kill: clears `killSwitch`,
     re-enables poller + issue creation.

9. **Enforcement order in every triage path** (manual route, poller,
   `POST /api/issues`). Check controls **before** spending Grok tokens:

   ```python
   def _gate(state, doc_id, signature, *, trigger):
       c = state['controls']
       if c['killSwitch']:
           return _record_attempt(state, doc_id, signature, 'skipped-kill-switch',
                                  reason='killSwitch=true', trigger=trigger)
       if c['skipProcessedDocs'] and doc_id in state['processed_docs']:
           prev = state['processed_docs'][doc_id]
           return _record_attempt(state, doc_id, signature, 'skipped-doc-processed',
                                  reason=f"already filed as #{prev.get('issueNumber')}",
                                  issueUrl=prev.get('issueUrl'),
                                  issueNumber=prev.get('issueNumber'),
                                  trigger=trigger)
       if c['skipKnownSignatures'] and signature in state['signatures']:
           # comment-on-dup path still runs, but it does NOT call Grok
           return _comment_on_dup(state, doc_id, signature, trigger=trigger)
       return None  # → proceed to Grok
   ```

   And **`_record_attempt(...)`** is the single place that:
   - appends to `state.attempts[]` (ring-buffer cap 200),
   - bumps `state.stats.today.*` and `state.stats.lifetime.*` counters
     (rolling the day if needed),
   - updates `state.lastTicket` **only** when `outcome == 'created'`.

### Verify

- Trigger the endpoint twice with the same `docId` → second call returns
  `skipped-doc-processed`.
- Trigger with two different `docId`s but the same forged `signature` →
  second call returns `commented` with the same URL; a GitHub comment appears.
- Set `DAILY_ISSUE_QUOTA=1`, create one, then try a third unique signature →
  returns 429 `deferred-quota`; nothing posted to GitHub.
- Wipe `state.json` and re-run a known signature → GitHub-search fallback finds
  the existing issue and re-hydrates state.
- `GET /api/issues/status?docIds=<created>,<deferred>,<unknown>` returns the
  three expected shapes.
- `GET /api/issues/recent` returns at least one entry after a create.
- `GET /api/triage/attempts` shows every attempt (including skips & errors)
  with `outcome`, `reason`, `model`, `grokMs`, `tokens`.
- `GET /api/triage/stats` returns non-zero `today`/`last24h`/`lifetime` counts
  after a few attempts; `lastTicket` matches the most recent `outcome=created`.
- `POST /api/triage/kill` → next call to `POST /api/triage/<id>` returns
  `skipped-kill-switch`; `POST /api/triage/resume` restores normal behavior.
- `POST /api/triage/controls {"modelOverride":"grok-4-latest"}` → next Grok
  call uses `grok-4-latest`, visible in `state.attempts[0].model`, without
  restarting the container.

---

# Session 4 — Background poller

**Outcome:** A daemon thread inside the Flask app continuously polls Couchbase
for error-bearing docs and runs the full pipeline (Sessions 2 + 3) on each new
one. No human click required.

### Tasks

1. New module-level: `_poller_state = {"lastRun": None, "lastError": None,
   "running": False}`.

2. **Discovery query** (re-uses existing `query_couchbase` helper):
   ```sql
   SELECT META(d).id AS docId, d.src, d.user_prompt,
          d.tools_called
   FROM `{bucket}`.`{scope}`.`{collection}` d
   WHERE ANY t IN d.tools_called SATISFIES
         t.error IS NOT MISSING AND t.error != {} END
   ORDER BY META(d).id DESC
   LIMIT 50
   ```

3. **Poller loop** (`threading.Thread(daemon=True)`):
   ```python
   def _poller_loop():
       while True:
           state = _load_state()
           if not state.get('pollerEnabled', True):
               time.sleep(POLL_INTERVAL_SEC); continue
           try:
               for doc in _discover_error_docs():
                   if doc['docId'] in state['processed_docs']:
                       continue
                   bug   = _build_bug_payload(doc)
                   sig   = compute_signature(_first_error_tool(doc),
                                              doc.get('user_prompt',''))

                   # Cheap pre-check: skip Grok entirely if signature
                   # is already known (just comment + record).
                   if sig in state['signatures']:
                       _post_recurrence_comment(state, doc, sig)
                       continue

                   # Quota guard: don't even spend Grok tokens if we
                   # can't create the resulting issue today.
                   if state['quota_used'] >= DAILY_ISSUE_QUOTA:
                       state['deferred'].append({"docId":doc['docId'],
                                                 "signature":sig})
                       _save_state(state); continue

                   repo    = _fetch_repo_context(GITHUB_REPO)
                   triage  = _call_grok(bug, repo)
                   _create_issue_from_triage(doc, bug, triage, sig)

               _poller_state['lastRun']   = datetime.utcnow().isoformat()
               _poller_state['lastError'] = None
           except Exception as e:
               _poller_state['lastError'] = str(e)
               print(f"[poller] error: {e}")
           time.sleep(POLL_INTERVAL_SEC)
   ```
   Note the **two short-circuits** (already-seen signature, over-quota) that
   avoid burning premium Grok tokens.

4. **Boot:** in `if __name__ == '__main__':`, only start the thread if
   `POLLER_ENABLED=true` AND `GROK_API_KEY` AND `GITHUB_TOKEN` are set:
   ```python
   if POLLER_ENABLED and GROK_API_KEY and GITHUB_TOKEN:
       threading.Thread(target=_poller_loop, daemon=True).start()
       print("[poller] started")
   ```

5. **Control endpoints:**
   - `GET  /api/poller/state` → `{enabled, lastRun, lastError, intervalSec, quotaUsed, quotaLimit}`
   - `POST /api/poller/pause` → sets `pollerEnabled=false`, persists.
   - `POST /api/poller/resume` → sets `pollerEnabled=true`, persists.
   - `POST /api/poller/run-now` → trigger a single iteration synchronously.

### Verify

- Insert (or wait for) two test docs with different errors → within
  `POLL_INTERVAL_SEC`, two GitHub issues exist.
- Insert a third doc whose error normalizes to the same signature as #1 → no
  new issue, but a comment is added; quota does **not** increment.
- Pause via `/api/poller/pause` → new error docs are ignored until resume.
- Set `DAILY_ISSUE_QUOTA=1` and exceed it → next iterations log `deferred`,
  `quota_used` does not exceed `1`.

---

# Session 5 — Frontend: per-row ticket badges + observability

**Outcome:** Every error row in the dashboard table shows ticket status at a
glance. Header shows poller status + daily quota. There's a "Recent triage
activity" panel and a deferred-queue view. The manual "Triage now" flow is
still available for one-offs.

### A. Add a new "Ticket" column to the error table

In [`templates/index.html`](../../templates/index.html), extend the table:

```html
<thead>
  <tr>
    <th>Tool Name</th>
    <th>Exec Time</th>
    <th>Doc ID</th>
    <th>Description</th>
    <th>Ticket</th>          <!-- NEW -->
    <th>Actions</th>
  </tr>
</thead>
```

### B. Render per-row ticket badges (4 visual states)

In [`static/js/script.js`](../../static/js/script.js), after `loadData()`
finishes filling `allErrors`, batch-fetch their statuses:

```js
const docIds = [...new Set(allErrors.map(e => e.sessionId))].join(',');
const statusResp = await fetch(`/api/issues/status?docIds=${encodeURIComponent(docIds)}`);
const statusMap  = await statusResp.json();   // { docId: {...} }
window.issueStatusMap = statusMap;
filterTable();                                 // re-render with badges
```

Then in `renderErrorRow(e)` add a "Ticket" cell rendered from
`window.issueStatusMap[e.sessionId]`:

| State                | Badge                                                                                     |
|---|---|
| `hasIssue: true`, `action: "created"` | 🟢 `#42` link · `created 3h ago` · severity pill |
| `hasIssue: true`, `action: "commented"` (dup signature) | 🔁 `#42` link · `seen ×3` · `last 1h ago` |
| `hasIssue: false`, `deferred: true` | ⏳ `Deferred (quota)` |
| `hasIssue: false` | ⚪ `—` plus a `Triage now` button                                              |

Concrete renderer:

```js
function renderTicketCell(s) {
  if (!s) return `<td>⚪ <span class="ticket-none">—</span></td>`;
  if (s.deferred) {
    return `<td class="ticket-deferred" title="${escapeHtml(s.reason || '')}">⏳ Deferred</td>`;
  }
  if (s.hasIssue) {
    const icon = s.action === 'commented' ? '🔁' : '🟢';
    const sev  = `<span class="sev sev-${s.severity || 'medium'}">${s.severity || 'medium'}</span>`;
    const seen = s.count > 1 ? `<small>seen ×${s.count}</small>` : '';
    const when = `<small title="${s.lastSeenAt}">${timeAgo(s.lastSeenAt)}</small>`;
    return `<td class="ticket-cell">
              ${icon}
              <a href="${s.issueUrl}" target="_blank" rel="noopener">#${s.issueNumber}</a>
              ${sev} ${seen} ${when}
            </td>`;
  }
  return `<td>⚪ —</td>`;
}
```

Add a small `timeAgo(iso)` helper (`5m`, `3h`, `2d`, …) and severity pill CSS
in [`static/css/style.css`](../../static/css/style.css).

### C. Show ticket info inside the report modal too

In the "🐛 Triage → GitHub" modal tab (introduced earlier), if the doc already
has a ticket, render a sticky header above the triage form:

```
✅ Issue already filed: #42  ·  created 3h ago  ·  seen ×3
[Open on GitHub ↗]   [Re-run triage]   [Add a comment]
```

If `deferred`, show:

```
⏳ Queued for triage — daily quota (3/10) was reached at 18:33 UTC.
   The poller will create this ticket tomorrow.
[Force create now]   (consumes 1 from tomorrow's quota)
```

### D. Header strip: poller + quota + last-ticket + kill switch

Add above the filters bar:

```html
<div class="triage-bar">
  <span id="pollerBadge">🤖 Poller: …</span>
  <span id="quotaBadge">📊 Today: …/…</span>
  <span id="lastTicketBadge" title="">✅ Last: —</span>
  <button onclick="pollerPause()">⏸ Pause</button>
  <button onclick="pollerResume()">▶ Resume</button>
  <button onclick="pollerRunNow()">⟳ Run now</button>
  <button class="danger" onclick="killSwitch()">🛑 Kill switch</button>
  <button onclick="openControlsPanel()">⚙ Settings</button>
</div>
```

- `#lastTicketBadge` sourced from `GET /api/triage/last-ticket`. Shows
  `"✅ Last: #42 · 3h ago · grok-4.3.0 · 4.1k tok · 48s"`. Click → opens
  the issue in a new tab.
- `🛑 Kill switch` posts to `/api/triage/kill`, turns the whole strip red,
  and changes its own label to `▶ Resume all`.
- Poll `/api/poller/state` + `/api/triage/health` + `/api/triage/last-ticket`
  every 15 s; flip `#pollerBadge` red when paused, amber when `lastError`
  is non-null.

### D.1. Controls panel (modal)

`⚙ Settings` opens a modal bound to `GET/POST /api/triage/controls`. Each
lever is a labeled toggle / dropdown that posts on change:

```
┌─ Triage Controls ────────────────────────────────────┐
│ [☐] 🛑 Kill switch (no Grok, no GitHub)              │
│ [☑] 🤖 Background poller enabled                     │
│ [☑] 🎫 Issue creation enabled                        │
│ [☑] 🚫 Skip already-handled docIds                   │
│ [☑] 🔁 Skip known signatures (don't recreate dups)   │
│ Model override:  [ (use env GROK_MODEL)            ▾]│
│                  options: grok-4-latest, grok-4.2.0, │
│                           grok-4.3.0, (none)         │
│ Last updated:  2026-05-16T12:00:00Z  by ui           │
│                                                      │
│ [Cancel]                                  [Save All] │
└──────────────────────────────────────────────────────┘
```

Implementation: toggles call `POST /api/triage/controls` with just the
changed field (debounced 300 ms). Show a small inline "✓ saved" toast.

### E. "Recent triage attempts" feed (full telemetry)

Under the table, a collapsible panel that hits `/api/triage/attempts?limit=50`
and renders one line per attempt, including outcomes that did NOT create a
ticket (and why):

```
🟢 created  #42  high   add_release_notes failed write   docId=add-…   poller  48.2s  4.1k tok  grok-4.3.0   3h ago
🔁 comment  #42         dup-sig=abcd1234                  docId=ar-2…  poller  0.6s   0 tok    —            18m ago
⚪ skipped  —           skipped-doc-processed             docId=add-…  manual  0.0s   0 tok    —             5m ago
⏳ deferred —           quota 10/10 reached at 18:33      docId=tx-9…  poller  0.0s   0 tok    —             2m ago
🛑 skipped  —           skipped-kill-switch               docId=zz-1…  poller  0.0s   0 tok    —             1m ago
❌ error    —           Grok 503 (timeout after 360s)     docId=tx-7…  manual 360.0s  0 tok    grok-4.3.0    8m ago
```

Outcome filter buttons: `All | Created | Commented | Skipped | Deferred | Errors`.

Hovering any row pops a tooltip with the full attempt JSON
(`payloadBytes`, `githubMs`, `totalMs`, `trigger`, etc.).

### F. Stats panel

Top-of-page collapsible "📊 Triage stats" block sourced from
`GET /api/triage/stats`. Three side-by-side cards (`today`, `last 24h`,
`lifetime`), each showing:

```
┌─ Today ──────────────┐
│ Attempts:        7   │
│   ✓ Created:     3   │  (+ percentage bar)
│   🔁 Commented:  2   │
│   ⚪ Skipped:    1   │
│   ⏳ Deferred:   1   │
│   ❌ Errors:     0   │
│ Quota:        3 / 10 │
│ Tokens:    24,310    │
│   ↳ grok-4.3.0  24k  │
│ Grok time:    5m 12s │
│   avg:        44.6s  │
└──────────────────────┘
```

Plus a "**Last ticket created**" hero card sourced from `lastTicket`:

```
✅ #42 · grok-4.3.0 · 48.2s · 4,100 tokens · severity: high
   "add_release_notes — failed write"
   3h ago · docId=add-release-notes-refactor
   [Open on GitHub ↗]
```

### G. Deferred queue panel

Another collapsible block listing `state.deferred[]` so the user can audit
what's waiting for tomorrow, with an "Force create now" button on each row
(calls `POST /api/issues` with `force: true`, which bypasses the quota check
once and decrements tomorrow's allowance instead — implement in Session 3 as
an optional follow-up).

### H. CSS polish

In [`static/css/style.css`](../../static/css/style.css) add:
- `.triage-bar { display:flex; gap:12px; … }` (with `.danger` red variant)
- `.ticket-cell a { font-weight:600; }`
- `.sev-low/medium/high/critical` color pills
- `.ticket-deferred { color:#caa42b; }`
- `.ticket-none { color:#888; }`
- `.attempt-row.outcome-created { border-left:3px solid #2ea043; }`
  (and similar for commented / skipped / deferred / error)
- `.stat-card .num { font-size:1.4em; font-weight:600; }`

### Verify

- Fresh boot, no tickets yet → all rows show `⚪ —` + "Triage now" button;
  header `0 / 10`, `Poller: running`, `Last: —`. Stats panel all zeros.
- Click "Triage now" on a row → poller short-circuits, issue is created,
  on next refresh the row shows `🟢 #42 high · just now`. Header ticks to
  `1 / 10`, `Last: #42 · just now · grok-X · Yk tok · Zs`.
- Insert a doc with a duplicate signature → after the next poller tick its
  row shows `🔁 #42 seen ×2 · just now`; quota does NOT advance, and the
  attempts feed gets a `🔁 comment` line with `0 tok` (no Grok was called).
- Set `DAILY_ISSUE_QUOTA=1`, force a third unique error → row shows
  `⏳ Deferred`. Deferred panel lists it. Attempts feed shows
  `⏳ deferred — quota 1/1`.
- Hit `🛑 Kill switch` → header turns red, poller pauses, next attempt
  (auto or manual) records `🛑 skipped-kill-switch` with `reason="killSwitch=true"`.
  No Grok or GitHub HTTP calls in the server log during this period.
- Change `Model override` in the Settings modal to `grok-4-latest` and
  re-trigger one triage → attempts feed shows new line with `model=grok-4-latest`.
- Open the modal on a row that already has a ticket → sticky header shows
  the issue link + "Re-run triage" + "Add a comment".
- Toggle off `Skip already-handled docIds` → next manual trigger on the same
  docId actually goes back to Grok (visible in attempts feed); turn it back on.

---

# Session 6 — Hardening & docs

### Tasks

1. **Quota reset job:** rather than relying on the next poller tick to roll the
   day, run `_roll_quota_if_new_day` at the top of every helper that reads
   `state['quota_used']`.
2. **Deferred re-drain:** when a new UTC day rolls over and quota frees up,
   process `state['deferred']` first (oldest first) until quota is exhausted.
3. **Logging:** structured log line per pipeline step:
   `[triage] doc=… sig=… action=created|commented|skipped|deferred url=…
   grokMs=… tokens=… quota=3/10`.
4. **Cost guard:** hard-cap the TOON payload at 32 KB; if larger, drop
   `chatTail` first, then truncate `toolErrors[*].error` strings.
5. **README:** new "Auto-Triage Pipeline" section covering env vars, dedup
   semantics, quota, and how to inspect `state.json`.
6. **Dockerfile / compose:** ensure `/data` dir exists and is writable.
7. **Optional:** small `scripts/dump_state.py` helper to pretty-print
   `state.json` (signatures + counts) so you can audit what's been seen.

### Verify

- Run for a full day with the quota set to 2 — confirm the deferred queue
  drains automatically at UTC midnight on the next iteration.
- Stop and restart the container — counters, signatures, and the deferred
  queue all survive.

---

## Quick resume checklist (paste into a new chat)

- [x] **Session 1** — env vars + `toons` dep + `state.json` (with `controls`, `lastTicket`, `attempts[]`, `stats`) + `/api/triage/health` ✅ done
- [x] **Session 2** — `_build_bug_payload`, `_to_toon`, `_call_grok` (returns telemetry dict: model, grokMs, tokens, payloadBytes), `POST /api/triage/<id>` ✅ done
- [x] **Session 3** — `compute_signature`, GitHub helpers, dedup engine, daily quota, `_gate()` + `_record_attempt()`, `POST /api/issues`, `GET /api/issues/{status,recent}`, `GET /api/triage/{attempts,stats,last-ticket,controls}`, `POST /api/triage/{controls,kill,resume}` ✅ done
- [x] **Session 4** — background poller thread (`_poller_loop` + `_poller_run_once` re-read `state.controls` every tick), `_discover_error_docs` N1QL, `_process_poller_doc` with pre-Grok short-circuits (gate + quota guard), `GET /api/poller/state`, `POST /api/poller/{pause,resume,run-now}`, `_maybe_start_poller()` at import time ✅ done
- [x] **Session 5** — Ticket column (🟢/🔁/⏳/⚪) with severity pills + Triage-now button, `.triage-bar` (poller / quota / Last-ticket badges + Pause/Resume/Run-now/🛑 Kill/⚙ Settings + toast), Controls modal bound to `/api/triage/controls` (debounced per-field save + ✓ toast + model override), `📊 Triage stats` panel (today / 24h / lifetime + Last-ticket hero), `🧾 Recent triage attempts` feed with outcome filter pills + full-attempt JSON tooltips, `⏳ Deferred queue` panel with Force-triage, modal `🐛 Triage → GitHub` tab with sticky header + `triageRow()` manual flow, `refreshTriageBar()` polling every 15 s ✅ done
- [x] **Session 6** — quota rollover, deferred drain, structured logging per attempt, README, Docker volume ✅ done

## Cheat-sheet of all new env vars

| Var | Default | Purpose |
|---|---|---|
| `GROK_API_KEY` | — | xAI key |
| `GROK_MODEL` | `grok-4-latest` | set `grok-4.2.0` or `grok-4.3.0` for premium |
| `GROK_API_URL` | `https://api.x.ai/v1/chat/completions` | endpoint |
| `GROK_TIMEOUT_SEC` | `360` | 6-minute timeout for slow premium calls |
| `GITHUB_TOKEN` | — | fine-grained PAT, Issues RW |
| `GITHUB_REPO` | `Fujio-Turner/continue-vscode-todos-tool` | target |
| `DAILY_ISSUE_QUOTA` | `10` | max issues created per UTC day |
| `POLL_INTERVAL_SEC` | `60` | poller cadence |
| `POLLER_ENABLED` | `true` | start poller on boot |
| `STATE_FILE` | `/data/state.json` | persisted dedup + quota state |

## Reference: existing code touch-points

- Error rows / sessionId list: [`get_errors`](../../app.py#L146).
- Single doc fetch via Data API: [`_fetch_document`](../../app.py#L283).
- Modal & viewer: [`viewReport()`](../../static/js/script.js#L207).
- Cluster config (don't leak `pass`): [`get_clusters_full`](../../app.py#L389).
