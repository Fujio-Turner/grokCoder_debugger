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

## Adding More Clusters

You can connect to multiple Couchbase clusters. The default cluster is configured via environment variables. Additional clusters can be added through the **Cluster Manager** UI (click the `+` button next to the cluster dropdown in the dashboard).

## License

MIT License
