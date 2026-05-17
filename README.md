# grokCoder Debugger

A web dashboard for debugging and inspecting output from the Grok AI Coder tool. View tool execution reports, inspect errors, and browse session data stored in Couchbase Capella.

![Python](https://img.shields.io/badge/Python-3.11+-blue)
![Flask](https://img.shields.io/badge/Flask-latest-green)
![Docker](https://img.shields.io/badge/Docker-supported-blue)

## Features

- Browse Grok AI Coder tool execution reports
- Inspect `tools_called` details — params, responses, errors, exec times
- JSON viewer with full document inspection
- AI Inputs debug view for each report
- Tool error summary with pie chart breakdown
- Multi-cluster support via the Cluster Manager UI
- Filter by time range and search text

## Requirements

- **Python 3.11+** (or Docker)
- **Couchbase Capella** account with Data API enabled
- A bucket/scope/collection set up (default: `grokCoder`.`continue`.`report`)

### Python Dependencies

```
flask
requests
```

## Quick Start (Docker)

This is the recommended way to run the project.

### 1. Clone the repo

```bash
git clone https://github.com/fujio-turner/grokcoder_debugger.git
cd grokcoder_debugger
```

### 2. Create your `.env` file

Copy the example and fill in your Couchbase Capella credentials:

```bash
cp .env.example .env
```

Edit `.env` with your values:

```env
CB_HOST=your-uuid.data.cloud.couchbase.com
CB_USER=your_username
CB_PASS=your_password
CB_BUCKET=grokCoder
CB_SCOPE=continue
CB_COLLECTION=report
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `CB_HOST` | **Yes** | — | Capella Data API hostname (e.g., `abc123.data.cloud.couchbase.com`) |
| `CB_USER` | **Yes** | — | Database credential username |
| `CB_PASS` | **Yes** | — | Database credential password |
| `CB_BUCKET` | No | `grokCoder` | Couchbase bucket name |
| `CB_SCOPE` | No | `continue` | Couchbase scope name |
| `CB_COLLECTION` | No | `report` | Couchbase collection name |
| `GROK_API_KEY` | No | — | xAI API key for the auto-triage pipeline |
| `GROK_MODEL` | No | `grok-4-latest` | Grok model name (e.g. `grok-4.2.0`, `grok-4.3.0`) |
| `GROK_API_URL` | No | `https://api.x.ai/v1/chat/completions` | xAI chat-completions endpoint |
| `GROK_TIMEOUT_SEC` | No | `360` | HTTP timeout for Grok calls (premium models are slow) |
| `GITHUB_TOKEN` | No | — | Fine-grained PAT with `Issues: Read & Write` on the target repo |
| `GITHUB_REPO` | No | `Fujio-Turner/continue-vscode-todos-tool` | `owner/repo` to file triage issues into |
| `DAILY_ISSUE_QUOTA` | No | `40` | Maximum GitHub issues the pipeline may create per UTC day |
| `POLL_INTERVAL_SEC` | No | `60` | Background poller cadence in seconds |
| `POLLER_ENABLED` | No | `true` | Start the background poller on boot |
| `STATE_FILE` | No | `/data/state.json` | Persisted dedup + quota state (Docker volume) |

### 3. Start the app

```bash
docker compose up
```

The dashboard will be available at **http://localhost:7777**

To run in the background:

```bash
docker compose up -d
```

To stop:

```bash
docker compose down
```

To rebuild after code changes:

```bash
docker compose up --build
```

## Quick Start (Local Python)

If you prefer to run without Docker:

### 1. Clone and install

```bash
git clone https://github.com/fujio-turner/grokcoder_debugger.git
cd grokcoder_debugger
python3 -m venv venv
source venv/bin/activate    # macOS/Linux
# venv\Scripts\activate     # Windows
pip install -r requirements.txt
```

### 2. Set environment variables

**macOS / Linux:**

```bash
export CB_HOST=your-uuid.data.cloud.couchbase.com
export CB_USER=your_username
export CB_PASS=your_password
export CB_BUCKET=grokCoder
export CB_SCOPE=continue
export CB_COLLECTION=report
```

**Windows (PowerShell):**

```powershell
$env:CB_HOST="your-uuid.data.cloud.couchbase.com"
$env:CB_USER="your_username"
$env:CB_PASS="your_password"
$env:CB_BUCKET="grokCoder"
$env:CB_SCOPE="continue"
$env:CB_COLLECTION="report"
```

### 3. Run

```bash
python3 app.py
```

Open **http://localhost:7777**

## Couchbase Capella Setup

This app connects to Couchbase Capella using the **Data API**. To verify your credentials work:

```bash
curl --user your_username:your_password \
  https://your-uuid.data.cloud.couchbase.com/v1/callerIdentity
```

You should get a JSON response with your identity info. If not, check:

- The Data API is enabled on your Capella cluster
- Your database credentials have read access to the bucket
- Your IP is allowed in the Capella allowed IP list

### Expected Data Structure

The app queries documents in `grokCoder`.`continue`.`report` with this shape:

```json
{
  "src": "refine_request",
  "user_prompt": "make a plan to refactor...",
  "chat_history": [],
  "tools_called": [
    {
      "name": "refine_request",
      "params": { "request": "..." },
      "response": { "content": [...] },
      "error": {},
      "meta": { "exec_time": 13.634 }
    }
  ],
  "~meta": { "id": "some-id" }
}
```

## Project Structure

```
grokcoder_debugger/
├── app.py               # Flask backend — API routes, Couchbase queries
├── templates/
│   └── index.html       # Dashboard HTML template
├── static/
│   ├── css/style.css    # Dashboard styles
│   └── js/script.js     # Frontend JavaScript
├── requirements.txt     # Python dependencies
├── Dockerfile           # Container image definition
├── docker-compose.yml   # Docker Compose config
├── .dockerignore        # Keeps Docker builds clean
├── .env.example         # Environment variable template (copy to .env)
├── .gitignore           # Git ignore rules
├── schema.json          # Sample document schema reference
├── LICENSE
└── README.md
```

### Files NOT committed (via .gitignore)

| File | Why |
|---|---|
| `.env` | Contains your Couchbase credentials |
| `config.json` | May contain cluster passwords added via UI |
| `venv/` | Local Python virtual environment |
| `__pycache__/` | Python bytecode cache |

## Auto-Triage Pipeline

When `GROK_API_KEY` **and** `GITHUB_TOKEN` are both set, the dashboard runs a
background **auto-triage pipeline** that watches Couchbase for new error-bearing
sessions, asks **Grok** for a root-cause analysis and suggested fix, and files
a deduped GitHub Issue in `GITHUB_REPO`. Manual one-off triage from the UI
("Triage now" / "🐛 Triage → GitHub" tab) goes through the same pipeline.

### Env vars

| Var | Default | Purpose |
|---|---|---|
| `GROK_API_KEY` | — | xAI API key |
| `GROK_MODEL` | `grok-4-latest` | premium models: `grok-4.2.0`, `grok-4.3.0` |
| `GROK_API_URL` | `https://api.x.ai/v1/chat/completions` | xAI endpoint |
| `GROK_TIMEOUT_SEC` | `360` | HTTP timeout (premium models are slow) |
| `GITHUB_TOKEN` | — | Fine-grained PAT with `Issues: Read & Write` |
| `GITHUB_REPO` | `Fujio-Turner/continue-vscode-todos-tool` | target repo |
| `DAILY_ISSUE_QUOTA` | `40` | max issues created per UTC day |
| `POLL_INTERVAL_SEC` | `60` | background poller cadence |
| `POLLER_ENABLED` | `true` | auto-start the poller on boot |
| `STATE_FILE` | `/data/state.json` | persisted dedup + quota state |

### Creating the `GITHUB_TOKEN`

The pipeline authenticates to the GitHub REST API as a Bearer token, so it
needs a personal access token (PAT) with **write access to Issues** on the
target repo. A **fine-grained PAT** is strongly preferred (least privilege):

**Fastest path — direct link:**
[https://github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new)

**Or navigate manually:**

1. Click your profile picture (top-right) → **Settings**.
2. In the **left sidebar**, scroll all the way to the **bottom** and click
   **Developer settings**. (It's the last item in the sidebar and is easy
   to miss — if you don't see it, scroll down further.)
3. In the left sidebar of the Developer settings page, expand
   **Personal access tokens** → click **Fine-grained tokens**.
4. Click **Generate new token**.
5. Fill in the form:
   - **Token name:** something descriptive, e.g. `grokcoder-debugger-triage`.
   - **Expiration:** pick a duration (1–366 days; GitHub no longer allows
     non-expiring fine-grained tokens).
   - **Resource owner:** the owner of `GITHUB_REPO` (e.g. `Fujio-Turner`).
     If this is an organization, an admin may need to approve the token
     before it works.
   - **Repository access:** **Only select repositories** → pick the target
     repo (e.g. `continue-vscode-todos-tool`).
   - **Repository permissions:** expand the section and set **Issues** →
     **Read and write**. (`Metadata` → `Read-only` gets selected
     automatically. Leave everything else as "No access".)
6. Click **Generate token** and copy the `github_pat_…` value immediately
   — it is only shown once.
7. Add it to your `.env`:

   ```env
   GITHUB_TOKEN=github_pat_xxxxxxxxxxxxxxxxxxxxxxxx
   GITHUB_REPO=Fujio-Turner/continue-vscode-todos-tool
   ```

7. Restart the container (`docker compose up -d --build` or
   `docker compose restart`).
8. In the dashboard, click **⚙ Settings → 🔍 Verify token** to confirm the
   token can read the repo and has Issues-write permission. Click
   **🧪 Create test issue** to file a real `grokcoder-test`-labeled issue
   you can close (it does **not** consume the daily quota). The same flow
   is exposed at `POST /api/triage/test-ticket` with `{"mode":"verify"}`
   or `{"mode":"create"}`.

A **classic PAT** with the `repo` scope also works but grants far more
access than the pipeline needs; use fine-grained tokens whenever possible.

> If `GITHUB_TOKEN` is empty:
> - `POST /api/issues` returns **503** with `{"error":"GITHUB_TOKEN is not set"}`.
> - The background poller refuses to start (logs `[poller] not started: GITHUB_TOKEN is empty`).
> - The manual "Triage now" button can still call Grok but cannot file the issue.

### Dedup semantics

Two layers prevent the same bug from getting filed twice:

1. **`processed_docs[docId]`** — exact-match short-circuit. Once a session
   document has been triaged, it is skipped (configurable per-session).
2. **`signatures[<sig>]`** — semantic short-circuit. The signature is a
   SHA-1 over `(tool_name, normalized error message, normalized first stack
   frame, normalized prompt topic)`. Two retries of "the same bug from a
   fresh chat" produce the same signature, comment on the existing GitHub
   issue, and **do not** consume Grok tokens or burn quota.

The GitHub issue body embeds **both** markers so state can be safely
re-hydrated from GitHub after a `state.json` wipe:

```
<!-- grokcoder-debugger-id:<docId> -->
<!-- grokcoder-signature:<sig> -->
```

### Daily quota & deferred queue

- Counters reset at **UTC midnight** (`quota_used`, `stats.today`).
- When the quota is reached, new unique errors are pushed onto
  `state.deferred[]` with `reason="quota"`.
- On the next poller tick (after the day rolls over), the deferred queue
  is **drained oldest-first** before any new error docs are scanned.

### Cost guard

- Each TOON-encoded payload is hard-capped at **32 KB**. If it exceeds the
  cap, `chatTail` is dropped first; then `toolErrors[*].error` strings are
  aggressively truncated. The reduction is logged as
  `[toon] cost-guard: …`.

### Inspecting `state.json`

The state file lives at `STATE_FILE` (default `/data/state.json` inside the
container, `./data/state.json` on the host via the bind mount).

```bash
# Pretty-print the state file:
python scripts/dump_state.py
python scripts/dump_state.py ./data/state.json
```

It shows quota usage, controls, top signatures by occurrence count, the
deferred queue, the most recent triage attempts, and the `lastTicket`.

For live introspection, use the HTTP endpoints:

| Endpoint | What it shows |
|---|---|
| `GET /api/triage/health` | Grok / GitHub / quota / poller config + last error |
| `GET /api/triage/stats` | today / last 24h / lifetime counters + `lastTicket` |
| `GET /api/triage/attempts?limit=50&outcome=…` | per-attempt feed (telemetry) |
| `GET /api/triage/last-ticket` | most recent created ticket |
| `GET /api/triage/controls` (+ `POST`) | kill switch, poller, model override |
| `POST /api/triage/kill` / `POST /api/triage/resume` | global toggle |
| `GET /api/poller/state` | thread alive, last run, quota |
| `POST /api/poller/{pause,resume,run-now}` | poller control |
| `GET /api/issues/status?docIds=a,b,c` | per-doc ticket status |
| `GET /api/issues/recent` | most recent ticket actions |

### Structured logs

Every pipeline step emits one line of the form:

```
[triage] doc=<id> sig=<8chars> action=<outcome> url=<url|->
         grokMs=<n> tokens=<n> quota=<used>/<limit>
         trigger=<manual|poller|poller-drain> reason=<…|->
```

Outcomes: `created`, `commented`, `deferred`, `error`,
`skipped-doc-processed`, `skipped-known-signature`,
`skipped-issue-creation-disabled`, `skipped-kill-switch`.

### Docker persistence

The container mounts `./data` → `/data`, so `state.json` (and therefore the
quota counter, signatures, and deferred queue) **survives `docker compose
down`**. The image declares `/data` as a volume and `chmod 0777`s it at
build time.

## Adding More Clusters

You can connect to multiple Couchbase clusters. The default cluster is configured via environment variables. Additional clusters can be added through the **Cluster Manager** UI (click the `+` button next to the cluster dropdown in the dashboard).

## License

Business Source License 1.1 (BSL-1.1). See [LICENSE](LICENSE).

- **Non-production use** (evaluation, development, testing, personal/internal
  tinkering) is permitted at no cost.
- **Production use** is permitted under the Additional Use Grant for
  non-commercial purposes or internal use within your organization. Offering
  the Licensed Work (or a substantial portion of its functionality) as a
  hosted/managed service to third parties is **not** permitted without a
  commercial license.
- **Change Date:** 2030-05-16. On that date the Licensed Work automatically
  converts to the **Apache License, Version 2.0**.

For commercial licensing inquiries, please contact the Licensor.
