# Grok-Powered Bug Triage → GitHub Issue Pipeline (v2.1)

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
     "pollerEnabled": true
   }
   ```
   Every code path that mutates state **must** stamp `at` / `firstSeenAt` /
   `lastSeenAt` (UTC, ISO-8601) and store `action` (one of
   `created` | `commented` | `deferred` | `skipped-doc-processed`).
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
   503 if `GROK_API_KEY` is empty.

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
     To support this, append every action to a bounded ring buffer
     `state['recentActions']` (cap at 100) when mutating state.

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

### D. Header strip: poller + quota status

Add above the filters bar:

```html
<div class="triage-bar">
  <span id="pollerBadge">🤖 Poller: …</span>
  <span id="quotaBadge">📊 Today: …/…</span>
  <button onclick="pollerPause()">⏸ Pause</button>
  <button onclick="pollerResume()">▶ Resume</button>
  <button onclick="pollerRunNow()">⟳ Run now</button>
</div>
```

Poll `/api/poller/state` + `/api/triage/health` every 15 s; flip
`#pollerBadge` red when paused, amber when `lastError` is non-null.

### E. "Recent triage activity" feed

Under the table, a collapsible panel that hits `/api/issues/recent?limit=20`
and renders one line per action:

```
🟢 created  #42  high   add_release_notes — failed write    docId=add-…   3h ago
🔁 comment  #42         (same as #42, signature=abcd…)       docId=ar-2…  18m ago
⏳ defer    —           tool_x error                          docId=tx-9…   2m ago
```

### F. Deferred queue panel

Another collapsible block listing `state.deferred[]` so the user can audit
what's waiting for tomorrow, with an "Force create now" button on each row
(calls `POST /api/issues` with `force: true`, which bypasses the quota check
once and decrements tomorrow's allowance instead — implement in Session 3 as
an optional follow-up).

### G. CSS polish

In [`static/css/style.css`](../../static/css/style.css) add:
- `.triage-bar { display:flex; gap:12px; … }`
- `.ticket-cell a { font-weight:600; }`
- `.sev-low/medium/high/critical` color pills
- `.ticket-deferred { color:#caa42b; }`
- `.ticket-none { color:#888; }`

### Verify

- Fresh boot, no tickets yet → all rows show `⚪ —` + "Triage now" button;
  header `0 / 10`, `Poller: running`.
- Click "Triage now" on a row → poller short-circuits, issue is created,
  on next refresh the row shows `🟢 #42 high · just now`. Header ticks to `1 / 10`.
- Insert a doc with a duplicate signature → after the next poller tick its
  row shows `🔁 #42 seen ×2 · just now`; quota does NOT advance.
- Set `DAILY_ISSUE_QUOTA=1`, force a third unique error → row shows
  `⏳ Deferred`. Deferred panel lists it.
- Open the modal on a row that already has a ticket → sticky header shows
  the issue link + "Re-run triage" + "Add a comment".
- Pause poller → header badge turns red; row "Triage now" buttons still work
  manually.

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

- [ ] **Session 1** — env vars + `toons` dep + `state.json` + `/api/triage/health`
- [ ] **Session 2** — `_build_bug_payload`, `_to_toon`, `_call_grok`, `POST /api/triage/<id>`
- [ ] **Session 3** — `compute_signature`, GitHub helpers, dedup engine, daily quota, `POST /api/issues`, `GET /api/issues/status`, `GET /api/issues/recent`
- [ ] **Session 4** — background poller thread + control endpoints
- [ ] **Session 5** — UI: per-row Ticket column with 4 badge states (🟢 created / 🔁 commented / ⏳ deferred / ⚪ none), modal sticky-header for existing tickets, poller + quota header bar, recent-activity feed, deferred-queue panel
- [ ] **Session 6** — quota rollover, deferred drain, logging, README, Docker volume

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
