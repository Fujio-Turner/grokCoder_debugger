#!/usr/bin/env python3
"""
Error Dashboard v3 - Dev tool for viewing all bugs and errors from Grok AI Coder sessions.

Features:
- JSON viewer popup with pair highlighting
- Group by session or error type
- Condensed "errors only" view
- **NEW: AI Inputs tab - debug package for AI assistants**

Usage:
    pip install -r requirements.txt
    python error_dashboard.py

Then open http://localhost:7777
"""

import os
import re
import json
import hashlib
import threading
import time
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, jsonify, request
import requests
from requests.auth import HTTPBasicAuth
from urllib.parse import urlparse

try:
    import toons  # https://pypi.org/project/toons/
except ImportError:
    toons = None

app = Flask(__name__, template_folder='templates', static_folder='static')

# Couchbase config from environment variables
CB_HOST = os.environ.get('CB_HOST', '')
CB_USER = os.environ.get('CB_USER', '')
CB_PASS = os.environ.get('CB_PASS', '')
CB_BUCKET = os.environ.get('CB_BUCKET', 'grokCoder')
CB_SCOPE = os.environ.get('CB_SCOPE', 'continue')
CB_COLLECTION = os.environ.get('CB_COLLECTION', 'report')
CB_FTS_INDEX = os.environ.get('CB_FTS_INDEX', 'grokCoder_Dugger_v1')

# ──────────────────────────────────────────────────────────────
# Grok / GitHub / Triage pipeline config (Session 1)
# ──────────────────────────────────────────────────────────────
GROK_API_KEY = os.environ.get('GROK_API_KEY', '')
GROK_MODEL = os.environ.get('GROK_MODEL', 'grok-4-latest')
GROK_API_URL = os.environ.get('GROK_API_URL', 'https://api.x.ai/v1/chat/completions')
GROK_TIMEOUT_SEC = int(os.environ.get('GROK_TIMEOUT_SEC', '360'))

GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')
GITHUB_REPO = os.environ.get('GITHUB_REPO', 'Fujio-Turner/continue-vscode-todos-tool')

DAILY_ISSUE_QUOTA = int(os.environ.get('DAILY_ISSUE_QUOTA', '10'))
POLL_INTERVAL_SEC = int(os.environ.get('POLL_INTERVAL_SEC', '60'))
POLLER_ENABLED = os.environ.get('POLLER_ENABLED', 'true').lower() in ('1', 'true', 'yes', 'on')
STATE_FILE = os.environ.get('STATE_FILE', '/data/state.json')

# ──────────────────────────────────────────────────────────────
# Persisted dedup/quota state (state.json)
# ──────────────────────────────────────────────────────────────
_state_lock = threading.Lock()

_DEFAULT_STATE = {
    "quota_date": "1970-01-01",
    "quota_used": 0,
    "signatures": {},
    "processed_docs": {},
    "deferred": [],
    "controls": {
        "pollerEnabled": POLLER_ENABLED,
        "issueCreationEnabled": True,
        "skipKnownSignatures": True,
        "skipProcessedDocs": True,
        "modelOverride": None,
        "killSwitch": False,
        "updatedAt": None,
        "updatedBy": "boot",
    },
    "lastTicket": None,
    "attempts": [],
    "stats": {
        "today": {
            "date": "1970-01-01",
            "attempts": 0, "created": 0, "commented": 0,
            "skipped": 0, "deferred": 0, "errors": 0,
            "tokensTotal": 0, "grokMsTotal": 0,
        },
        "lifetime": {
            "attempts": 0, "created": 0, "commented": 0,
            "skipped": 0, "deferred": 0, "errors": 0,
            "tokensTotal": 0, "grokMsTotal": 0,
        },
    },
    "pollerEnabled": POLLER_ENABLED,
    "poller": {
        "lastRun": None,
        "lastError": None,
    },
}


def _today_utc() -> str:
    """Current UTC date as YYYY-MM-DD."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


def _now_utc_iso() -> str:
    """Current UTC time as ISO-8601 with trailing Z."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _deep_merge_defaults(target: dict, defaults: dict) -> dict:
    """Ensure all keys from defaults exist in target (recursive for dicts)."""
    for k, v in defaults.items():
        if k not in target:
            target[k] = json.loads(json.dumps(v))  # deep copy
        elif isinstance(v, dict) and isinstance(target[k], dict):
            _deep_merge_defaults(target[k], v)
    return target


def _load_state() -> dict:
    """Load state.json from disk (returns defaults if missing or unreadable)."""
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
            _deep_merge_defaults(state, _DEFAULT_STATE)
            return state
    except Exception as e:
        print(f"[state] failed to load {STATE_FILE}: {e} — using defaults")
    return json.loads(json.dumps(_DEFAULT_STATE))


def _save_state(state: dict) -> None:
    """Atomically persist state to disk."""
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or '.', exist_ok=True)
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"[state] failed to save {STATE_FILE}: {e}")


def _roll_quota_if_new_day(state: dict) -> None:
    """Reset the daily quota counter if the UTC date has changed."""
    today = _today_utc()
    if state.get('quota_date') != today:
        state['quota_date'] = today
        state['quota_used'] = 0
        # also roll the per-day stats card
        stats_today = state.setdefault('stats', {}).setdefault('today', {})
        if stats_today.get('date') != today:
            state['stats']['today'] = {
                "date": today,
                "attempts": 0, "created": 0, "commented": 0,
                "skipped": 0, "deferred": 0, "errors": 0,
                "tokensTotal": 0, "grokMsTotal": 0,
            }


# Initialize state at module load and roll quota for today.
with _state_lock:
    _STATE = _load_state()
    _roll_quota_if_new_day(_STATE)
    _save_state(_STATE)

# Build configs list - env vars create default cluster, config.json can add more
configs = []
if CB_HOST and CB_USER and CB_PASS:
    configs.append({
        'name': 'Default',
        'host': CB_HOST,
        'user': CB_USER,
        'pass': CB_PASS,
        'bucket': CB_BUCKET,
        'scope': CB_SCOPE,
        'collection': CB_COLLECTION,
        'fts_index': CB_FTS_INDEX
    })

# Optionally load additional clusters from config.json
if os.path.exists('config.json'):
    with open('config.json') as f:
        file_configs = json.load(f)
        for fc in file_configs:
            if not any(c['name'] == fc['name'] for c in configs):
                configs.append(fc)

def _capella_error_message(resp):
    """Extract a human-readable error from a Capella Data API error response."""
    # Capella returns {"code": "...", "message": "..."} on errors
    status = resp.status_code
    try:
        body = resp.json()
        code = body.get('code', '')
        msg = body.get('message', '') or body.get('errors', '')
    except Exception:
        code = ''
        msg = resp.text[:300]

    error_map = {
        400: f'Bad request: {msg or "malformed request or SQL++ syntax error"}',
        401: f'Unauthorized: {msg or "invalid credentials — check CB_USER and CB_PASS"}',
        403: f'Forbidden: {msg or "insufficient permissions on this bucket/scope/collection"}',
        404: f'Not found: {msg or "invalid bucket, scope, collection, or document key"}',
        405: f'Method not allowed: {msg or "invalid namespace or keyspace"}',
        409: f'Conflict: {msg or "a conflict occurred (CAS mismatch or duplicate key)"}',
        413: f'Document too large: {msg or "document exceeds maximum size"}',
        500: f'Capella internal error: {msg or "try again later"}',
        503: f'Service unavailable: {msg or "underlying Couchbase service not ready"}',
        504: f'Gateway timeout: {msg or "request timed out on the Capella side"}',
    }
    detail = error_map.get(status, f'HTTP {status}: {msg}')
    if code:
        detail = f'[{code}] {detail}'
    return detail


def _handle_capella_http_error(e, context='request'):
    """Handle requests.exceptions.HTTPError from Capella, return (body, status_code)."""
    status = e.response.status_code
    detail = _capella_error_message(e.response)
    print(f"Capella {context} error ({status}): {detail}")
    return jsonify({'error': detail, 'status': status}), status


def query_couchbase(n1ql: str, params: dict = None, config: dict = None) -> list:
    """Execute N1QL query against Couchbase."""
    if config is None:
        config = {
            'host': CB_HOST,
            'user': CB_USER,
            'pass': CB_PASS,
            'bucket': CB_BUCKET
        }
    host_str = urlparse(config['host']).netloc or config['host']
    url = f"https://{host_str}/_p/query/query/service"
    auth = HTTPBasicAuth(config['user'], config['pass'])
    payload = {"statement": n1ql}
    
    if params:
        for k, v in params.items():
            payload["$" + k] = v
    
    print(f"\n{'='*60}")
    print(f"SQL++ → {url}")
    print(f"{'='*60}")
    print(n1ql.strip())
    if params:
        print(f"Params: {params}")
    print(f"{'='*60}\n")
    
    try:
        resp = requests.post(
            url,
            auth=auth,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=30
        )
        resp.raise_for_status()
        return resp.json().get('results', [])
    except requests.exceptions.HTTPError as e:
        detail = _capella_error_message(e.response)
        print(f"Query error ({e.response.status_code}): {detail}")
        return []
    except requests.exceptions.ConnectionError:
        print(f"Query error: cannot reach {host_str}")
        return []
    except requests.exceptions.Timeout:
        print(f"Query error: request to {host_str} timed out")
        return []
    except Exception as e:
        print(f"Query error: {e}")
        return []
@app.route('/api/errors')
def get_errors():
    cluster = request.args.get('cluster', 'Default')
    cluster_config = next((c for c in configs if c['name'] == cluster), configs[0])
    bucket = cluster_config['bucket']
    scope = cluster_config.get('scope', 'continue')
    collection = cluster_config.get('collection', 'report')
    fqn = f"`{bucket}`.`{scope}`.`{collection}`"
    time_range = request.args.get('range', 'day')
    now = datetime.now(timezone.utc)
    if time_range == 'hour':
        start = now - timedelta(hours=1)
    elif time_range == 'day':
        start = now - timedelta(days=1)
    elif time_range == 'week':
        start = now - timedelta(days=7)
    elif time_range == 'month':
        start = now - timedelta(days=30)
    else:
        start = datetime(2020, 1, 1)
    start_str = start.isoformat() + 'Z'
    # Query tools_called for errors
    tools_errors_query = f"""
        SELECT META(d).id as sessionId, d.src, d.user_prompt,
               t.name as toolName, t.error, t.meta, t.params, t.response
        FROM {fqn} d UNNEST d.tools_called t
        WHERE t.error IS NOT MISSING AND t.error != {{}}
        ORDER BY META(d).id DESC
        LIMIT 200
    """
    # Query all reports for listing
    reports_query = f"""
        SELECT META(d).id as docId, d.src, d.user_prompt, d.`~meta`,
               ARRAY_LENGTH(d.tools_called) as toolCount,
               ARRAY_LENGTH(d.chat_history) as chatHistoryLen
        FROM {fqn} d
        ORDER BY META(d).id DESC
        LIMIT 200
    """
    tools_errors = query_couchbase(tools_errors_query, config=cluster_config)
    reports = query_couchbase(reports_query, config=cluster_config)
    
    session_ids = set()
    all_errors = []
    
    for t in tools_errors:
        sid = t.get('sessionId', '')
        session_ids.add(sid)
        all_errors.append({
            'type': 'tool_error',
            'sessionId': sid,
            'toolName': t.get('toolName', 'unknown'),
            'description': f"[{t.get('toolName', 'unknown')}] {t.get('src', '')}",
            'details': json.dumps(t.get('error', {})),
            'userPrompt': t.get('user_prompt', ''),
            'execTime': (t.get('meta') or {}).get('exec_time', 0),
        })
    
    return jsonify({
        'stats': {
            'toolErrors': len(tools_errors),
            'totalReports': len(reports),
            'uniqueSessions': len(session_ids)
        },
        'errors': all_errors,
        'reports': reports
    })

@app.route('/api/search')
def search_fts():
    """Full-text search against the Capella FTS index."""
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'hits': [], 'total': 0})

    cluster = request.args.get('cluster', 'Default')
    cluster_config = next((c for c in configs if c['name'] == cluster), configs[0])

    host_str = urlparse(cluster_config['host']).netloc or cluster_config['host']
    bucket = cluster_config['bucket']
    scope = cluster_config.get('scope', 'continue')
    fts_index = cluster_config.get('fts_index', CB_FTS_INDEX)

    url = f"https://{host_str}/_p/fts/api/bucket/{bucket}/scope/{scope}/index/{fts_index}/query"
    auth = HTTPBasicAuth(cluster_config['user'], cluster_config['pass'])

    payload = {
        "query": {"query": q},
        "size": 50,
        "fields": ["*"],
        "highlight": {
            "style": "html",
            "fields": ["user_prompt", "src", "tools_called.name", "tools_called.params"]
        }
    }

    print(f"\n{'='*60}")
    print(f"FTS → {url}")
    print(f"{'='*60}")
    print(json.dumps(payload, indent=2))
    print(f"{'='*60}\n")

    try:
        resp = requests.post(
            url,
            auth=auth,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()

        hits = []
        for hit in data.get('hits', []):
            hits.append({
                'docId': hit.get('id', ''),
                'score': round(hit.get('score', 0), 4),
                'fields': hit.get('fields', {}),
                'fragments': hit.get('fragments', {}),
            })

        return jsonify({
            'hits': hits,
            'total': data.get('total_hits', 0),
            'took': data.get('took', 0),
        })
    except requests.exceptions.HTTPError as e:
        return _handle_capella_http_error(e, context='FTS search')
    except requests.exceptions.ConnectionError:
        return jsonify({'error': f'Cannot reach {host_str}'}), 503
    except requests.exceptions.Timeout:
        return jsonify({'error': 'FTS search timed out'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _fetch_document(config, doc_key):
    """Fetch a document from Capella Data API. Returns (doc_dict, None) or (None, error_response)."""
    bucket = config['bucket']
    scope = config.get('scope', 'continue')
    collection = config.get('collection', 'report')
    host_str = config['host']
    if host_str.startswith('couchbases://'):
        host_str = host_str.replace('couchbases://', '')
    elif host_str.startswith('https://'):
        host_str = host_str.replace('https://', '')
    elif host_str.startswith('http://'):
        host_str = host_str.replace('http://', '')
    url = f"https://{host_str}/v1/buckets/{bucket}/scopes/{scope}/collections/{collection}/documents/{doc_key}"
    print(f"GET doc → {url}")
    auth = HTTPBasicAuth(config['user'], config['pass'])
    try:
        resp = requests.get(url, auth=auth, timeout=30)
        print(f"GET doc ← {resp.status_code} ({len(resp.content)} bytes)")
        resp.raise_for_status()
        return resp.json(), None
    except requests.exceptions.HTTPError as e:
        print(f"GET doc ← {e.response.status_code}: {e.response.text[:300]}")
        return None, _handle_capella_http_error(e, context=f'GET document {doc_key}')
    except requests.exceptions.ConnectionError:
        msg = f'Cannot reach {host_str}. Check CB_HOST.'
        return None, (jsonify({'error': msg}), 503)
    except requests.exceptions.Timeout:
        msg = f'Request to {host_str} timed out.'
        return None, (jsonify({'error': msg}), 504)
    except Exception as e:
        return None, (jsonify({'error': str(e)}), 500)


@app.route('/api/session/<session_id>')
def get_session(session_id):
    config = configs[0]
    doc, err = _fetch_document(config, session_id)
    if err:
        return err
    return jsonify(doc)

@app.route('/api/audit/<session_id>')
def get_audit(session_id):
    """Get the audit document for a session (debug:{sessionId})."""
    config = configs[0]
    audit_key = f'debug:{session_id}'
    doc, err = _fetch_document(config, audit_key)
    if err:
        return err
    return jsonify(doc)

@app.route('/api/session/<session_id>/flow')
def get_session_flow(session_id):
    """Generate Sankey flow data from a session's tools_called for chat flow visualization."""
    config = configs[0]
    session, err = _fetch_document(config, session_id)
    if err:
        return err
    
    tools_called = session.get('tools_called', [])
    user_prompt = session.get('user_prompt', '')
    src = session.get('src', '')
    
    node_names = []
    links = []
    error_count = 0
    
    prompt_label = "User Prompt"
    node_names.append(prompt_label)
    
    prev_node = prompt_label
    for i, tool in enumerate(tools_called):
        tool_name = tool.get('name', f'tool_{i}')
        node_label = f"{tool_name} #{i}"
        has_error = bool(tool.get('error'))
        if has_error:
            error_count += 1
            node_label += " ⚠"
        node_names.append(node_label)
        exec_time = (tool.get('meta') or {}).get('exec_time', 0) or 0
        value = max(int(exec_time * 1000), 100)
        links.append({
            'source': node_names.index(prev_node),
            'target': node_names.index(node_label),
            'value': value
        })
        prev_node = node_label
    
    sankey_nodes = [{'name': n} for n in node_names]
    stats = {
        'totalTools': len(tools_called),
        'errorCount': error_count,
        'src': src,
        'userPrompt': user_prompt[:200] if user_prompt else ''
    }
    
    return jsonify({
        'nodes': sankey_nodes,
        'links': links,
        'stats': stats
    })

@app.route('/api/clusters')
def get_clusters():
    return jsonify([c['name'] for c in configs])

@app.route('/api/clusters/full')
def get_clusters_full():
    safe_configs = [{k: v for k, v in c.items() if k != 'pass'} for c in configs]
    return jsonify(safe_configs)

@app.route('/api/clusters', methods=['POST'])
def add_cluster():
    data = request.json
    if not all(k in data for k in ['name', 'host', 'user', 'pass', 'bucket']):
        return jsonify({'error': 'Missing fields'}), 400
    if any(c['name'] == data['name'] for c in configs):
        return jsonify({'error': 'Name already exists'}), 400
    configs.append(data)
    with open('config.json', 'w') as f:
        json.dump(configs, f, indent=4)
    return jsonify({'success': True})

@app.route('/api/clusters/<name>', methods=['PUT'])
def update_cluster(name):
    data = request.json
    for c in configs:
        if c['name'] == name:
            for key in ['host', 'user', 'pass', 'bucket']:
                if key in data:
                    c[key] = data[key]
            with open('config.json', 'w') as f:
                json.dump(configs, f, indent=4)
            return jsonify({'success': True})
    return jsonify({'error': 'Cluster not found'}), 404

@app.route('/api/clusters/<name>', methods=['DELETE'])
def delete_cluster(name):
    global configs
    configs = [c for c in configs if c['name'] != name]
    with open('config.json', 'w') as f:
        json.dump(configs, f, indent=4)
    return jsonify({'success': True})

@app.route('/api/clusters/test', methods=['POST'])
def test_cluster_route():
    data = request.json
    config = {
        'host': data.get('host'),
        'user': data.get('user'),
        'pass': data.get('pass'),
        'bucket': data.get('bucket')
    }
    if not all(config.values()):
        return jsonify({'success': False, 'error': 'Missing fields'}), 400
    test_n1ql = "SELECT 1 as ok"
    result = query_couchbase(test_n1ql, config=config)
    if result and len(result) > 0 and result[0].get('ok') == 1:
        return jsonify({'success': True, 'message': 'Connection successful'})
    else:
        return jsonify({'success': False, 'error': 'Failed to connect or query'})
@app.route('/api/health')
def health_check():
    """Check connection to the current cluster's Couchbase Capella endpoint."""
    cluster = request.args.get('cluster', 'Default')
    cluster_config = next((c for c in configs if c['name'] == cluster), None)
    if not cluster_config:
        return jsonify({'ok': False, 'error': 'No cluster configured. Set CB_HOST, CB_USER, CB_PASS env vars.'})
    host_str = urlparse(cluster_config['host']).netloc or cluster_config['host']
    url = f"https://{host_str}/v1/callerIdentity"
    try:
        resp = requests.get(
            url,
            auth=HTTPBasicAuth(cluster_config['user'], cluster_config['pass']),
            timeout=10
        )
        resp.raise_for_status()
        identity = resp.json()
        return jsonify({'ok': True, 'identity': identity})
    except requests.exceptions.ConnectionError:
        return jsonify({'ok': False, 'error': f'Cannot reach {host_str}. Check CB_HOST.'})
    except requests.exceptions.HTTPError as e:
        detail = _capella_error_message(e.response)
        return jsonify({'ok': False, 'error': detail})
    except requests.exceptions.Timeout:
        return jsonify({'ok': False, 'error': f'Connection to {host_str} timed out.'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

# ──────────────────────────────────────────────────────────────
# Session 2 — TOON bug-payload builder + Grok client
# ──────────────────────────────────────────────────────────────

# Hard caps per the plan
_MAX_FIELD_BYTES = 4 * 1024          # 4 KB per string field
_MAX_PAYLOAD_BYTES = 32 * 1024       # 32 KB total payload
_CHAT_TAIL_N = 6                     # last N chat entries
_REPO_CTX_TTL_SEC = 600              # 10 min cache for repo context
_REPO_CTX_README_BYTES = 8 * 1024    # first 8 KB of README
_REPO_CTX_MAX_TREE_PATHS = 200

_repo_ctx_cache = {}                 # repo -> (expires_epoch, payload_dict)
_repo_ctx_lock = threading.Lock()


def _truncate_str(s, limit=_MAX_FIELD_BYTES):
    if not isinstance(s, str):
        return s
    if len(s) <= limit:
        return s
    return s[: max(0, limit - len('...[truncated]'))] + '...[truncated]'


def _short_error(err):
    """Compact JSON-ish string of an error object, capped to _MAX_FIELD_BYTES."""
    if err is None:
        return ''
    if isinstance(err, str):
        return _truncate_str(err)
    try:
        return _truncate_str(json.dumps(err, sort_keys=True, default=str))
    except Exception:
        return _truncate_str(str(err))


def _normalize_session_doc(doc: dict) -> dict:
    """Adapter: convert a 'Continue'-shaped chat doc (root `chatHistory[]`
    with `toolCallStates[]` errors) into the flat `report` shape the rest
    of the triage pipeline expects (`user_prompt`, `chat_history[]`,
    `tools_called[]`). Pass-through for already-flat docs.

    Continue shape (per `sample/chat_sample.json`):
      { chatHistory: [ {
          message: { role, content|toolCalls[] },
          toolCallStates: [ {
            tool: { function: { name } },
            parsedArgs, status, error: { code, message, rawOutput }, output[]
          } ],
          promptLogs[]
      } ] }
    """
    if not isinstance(doc, dict):
        return doc
    # Already-flat report shape: nothing to do.
    if 'tools_called' in doc or 'user_prompt' in doc:
        return doc
    chat = doc.get('chatHistory') or doc.get('chat_history')
    if not isinstance(chat, list):
        return doc

    out = dict(doc)  # shallow copy; we only set top-level keys.

    def _extract_text(content):
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict):
                    parts.append(p.get('text') or p.get('content') or '')
                else:
                    parts.append(str(p))
            return ' '.join(p for p in parts if p)
        return ''

    # 1) Synthesize user_prompt = first user-role message.
    user_prompt = ''
    for turn in chat:
        msg = (turn or {}).get('message') or {}
        if msg.get('role') == 'user':
            user_prompt = _extract_text(msg.get('content'))
            if user_prompt:
                break

    # 2) Synthesize tools_called from every toolCallStates entry. Errored
    #    states keep their `error` dict; successful ones get `error: {}` so
    #    they're skipped by `_first_error_tool` / `_build_bug_payload` but
    #    still visible to downstream code that wants the full sequence.
    tools_called = []
    for turn in chat:
        for state in ((turn or {}).get('toolCallStates') or []):
            if not isinstance(state, dict):
                continue
            fn = ((state.get('tool') or {}).get('function') or {})
            name = fn.get('name') or 'unknown'
            err = state.get('error') if state.get('status') == 'errored' else {}
            tools_called.append({
                'name': name,
                'error': err or {},
                'params': state.get('parsedArgs') or {},
                'response': state.get('output') or [],
                'meta': {},  # Continue format does not record exec_time.
            })

    # 3) Synthesize flat chat_history (role + content) for the chat tail.
    chat_flat = []
    for turn in chat:
        msg = (turn or {}).get('message') or {}
        role = msg.get('role') or ''
        content = _extract_text(msg.get('content'))
        if not content and msg.get('toolCalls'):
            # Surface the tool call so the tail isn't empty for assistant turns.
            names = []
            for tc in msg['toolCalls']:
                if isinstance(tc, dict):
                    names.append(((tc.get('function') or {}).get('name')) or 'tool')
            content = f"<toolCalls: {', '.join(names)}>"
        if role:
            chat_flat.append({'role': role, 'content': content})

    out['user_prompt'] = user_prompt
    out['tools_called'] = tools_called
    out['chat_history'] = chat_flat
    out['_continue_normalized'] = True  # diagnostic flag
    return out


def _build_bug_payload(session_doc: dict) -> dict:
    """Shape a session document into a compact, TOON-friendly bug payload."""
    session_doc = _normalize_session_doc(session_doc)
    doc_id = (
        session_doc.get('docId')
        or session_doc.get('id')
        or (session_doc.get('~meta') or {}).get('id')
        or ''
    )
    src = session_doc.get('src', '') or ''
    user_prompt = _truncate_str(session_doc.get('user_prompt', '') or '')

    tool_errors = []
    for i, t in enumerate(session_doc.get('tools_called') or []):
        err = t.get('error')
        # Skip tools with no error / empty error
        if not err or (isinstance(err, dict) and not err):
            continue
        exec_time = ((t.get('meta') or {}).get('exec_time') or 0)
        tool_errors.append({
            'i': i,
            'name': t.get('name', 'unknown'),
            'error': _short_error(err),
            'execTime': exec_time,
        })

    chat_history = session_doc.get('chat_history') or []
    chat_tail = []
    for entry in chat_history[-_CHAT_TAIL_N:]:
        if isinstance(entry, dict):
            role = entry.get('role') or entry.get('type') or ''
            content = entry.get('content') or entry.get('message') or ''
            if not isinstance(content, str):
                try:
                    content = json.dumps(content, default=str)
                except Exception:
                    content = str(content)
            chat_tail.append({
                'role': role,
                'content': _truncate_str(content),
            })
        else:
            chat_tail.append({'role': '', 'content': _truncate_str(str(entry))})

    payload = {
        'docId': doc_id,
        'src': src,
        'userPrompt': user_prompt,
        'toolErrors': tool_errors,
        'chatTail': chat_tail,
    }

    # Cost guard: drop chatTail first, then truncate errors if still too big.
    if len(json.dumps(payload, default=str)) > _MAX_PAYLOAD_BYTES:
        payload['chatTail'] = []
    if len(json.dumps(payload, default=str)) > _MAX_PAYLOAD_BYTES:
        for te in payload['toolErrors']:
            te['error'] = _truncate_str(te.get('error') or '', limit=1024)

    return payload


def _fetch_repo_context(repo: str) -> dict:
    """Fetch README + top-level file tree for a GitHub repo. Cached 10 min."""
    if not repo:
        return {'repo': '', 'branch': '', 'readme': '', 'tree': []}

    now = time.time()
    with _repo_ctx_lock:
        cached = _repo_ctx_cache.get(repo)
        if cached and cached[0] > now:
            return cached[1]

    headers = {'Accept': 'application/vnd.github+json'}
    if GITHUB_TOKEN:
        headers['Authorization'] = f'Bearer {GITHUB_TOKEN}'

    branch = 'main'
    readme = ''
    tree_paths = []

    try:
        r = requests.get(f'https://api.github.com/repos/{repo}', headers=headers, timeout=15)
        if r.status_code == 200:
            branch = r.json().get('default_branch', 'main')
    except Exception as e:
        print(f"[repo-ctx] default_branch lookup failed for {repo}: {e}")

    try:
        r = requests.get(
            f'https://api.github.com/repos/{repo}/readme',
            headers={**headers, 'Accept': 'application/vnd.github.raw'},
            timeout=15,
        )
        if r.status_code == 200:
            readme = r.text[:_REPO_CTX_README_BYTES]
    except Exception as e:
        print(f"[repo-ctx] readme fetch failed for {repo}: {e}")

    try:
        r = requests.get(
            f'https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1',
            headers=headers,
            timeout=20,
        )
        if r.status_code == 200:
            entries = r.json().get('tree', []) or []
            tree_paths = [e.get('path', '') for e in entries if e.get('type') == 'blob']
            tree_paths = tree_paths[:_REPO_CTX_MAX_TREE_PATHS]
    except Exception as e:
        print(f"[repo-ctx] tree fetch failed for {repo}: {e}")

    ctx = {
        'repo': repo,
        'branch': branch,
        'readme': readme,
        'tree': tree_paths,
    }
    with _repo_ctx_lock:
        _repo_ctx_cache[repo] = (now + _REPO_CTX_TTL_SEC, ctx)
    return ctx


def _to_toon(payload: dict, max_bytes: int = _MAX_PAYLOAD_BYTES) -> str:
    """Encode payload as TOON, logging the size reduction vs raw JSON.
    Cost guard: if the encoded result exceeds `max_bytes`, progressively
    drop `chatTail` then truncate `toolErrors[*].error` strings and
    re-encode (in-place mutation of payload)."""
    if toons is None:
        raise RuntimeError("toons package is not installed (pip install 'toons>=0.5.0')")
    toon_str = toons.dumps(payload)

    if isinstance(payload, dict) and len(toon_str) > max_bytes:
        # 1st pass: drop chatTail
        if payload.get('chatTail'):
            payload['chatTail'] = []
            toon_str = toons.dumps(payload)
            print(f"[toon] cost-guard: dropped chatTail (now {len(toon_str)} bytes)")
        # 2nd pass: aggressively truncate tool error strings
        if len(toon_str) > max_bytes and payload.get('toolErrors'):
            for te in payload['toolErrors']:
                te['error'] = _truncate_str(te.get('error') or '', limit=512)
            toon_str = toons.dumps(payload)
            print(f"[toon] cost-guard: truncated toolErrors (now {len(toon_str)} bytes)")

    json_len = len(json.dumps(payload, default=str))
    toon_len = len(toon_str)
    saved = json_len - toon_len
    pct = (saved / json_len * 100) if json_len else 0
    print(f"[toon] json={json_len} bytes  toon={toon_len} bytes  saved={saved} ({pct:.1f}%)")
    return toon_str


def _call_grok(bug_payload: dict, repo_context: dict, model_override: str = None) -> dict:
    """Call Grok with TOON-encoded payloads. Returns a telemetry dict."""
    t0 = time.monotonic()
    model = model_override or GROK_MODEL

    try:
        bug_toon = _to_toon(bug_payload)
        repo_toon = _to_toon(repo_context)
    except Exception as e:
        return {
            'triage': None,
            'model': model,
            'grokMs': int((time.monotonic() - t0) * 1000),
            'tokens': {'prompt': 0, 'completion': 0, 'total': 0},
            'payloadBytes': {'jsonEquivalent': 0, 'toonSent': 0},
            'error': f'toon-encoding-failed: {e}',
        }

    payload_bytes = {
        'jsonEquivalent': len(json.dumps(bug_payload, default=str))
            + len(json.dumps(repo_context, default=str)),
        'toonSent': len(bug_toon) + len(repo_toon),
    }

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
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }

    try:
        r = requests.post(
            GROK_API_URL,
            headers={
                "Authorization": f"Bearer {GROK_API_KEY}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=GROK_TIMEOUT_SEC,
        )
        r.raise_for_status()
        resp_json = r.json()
    except requests.exceptions.HTTPError as e:
        body_txt = ''
        try:
            body_txt = e.response.text[:500]
        except Exception:
            pass
        return {
            'triage': None,
            'model': model,
            'grokMs': int((time.monotonic() - t0) * 1000),
            'tokens': {'prompt': 0, 'completion': 0, 'total': 0},
            'payloadBytes': payload_bytes,
            'error': f'grok-http-{e.response.status_code}: {body_txt}',
        }
    except Exception as e:
        return {
            'triage': None,
            'model': model,
            'grokMs': int((time.monotonic() - t0) * 1000),
            'tokens': {'prompt': 0, 'completion': 0, 'total': 0},
            'payloadBytes': payload_bytes,
            'error': f'grok-request-failed: {e}',
        }

    usage = resp_json.get('usage', {}) or {}
    actual_model = resp_json.get('model') or model
    try:
        content = resp_json['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError) as e:
        return {
            'triage': None,
            'model': actual_model,
            'grokMs': int((time.monotonic() - t0) * 1000),
            'tokens': {
                'prompt': usage.get('prompt_tokens', 0),
                'completion': usage.get('completion_tokens', 0),
                'total': usage.get('total_tokens', 0),
            },
            'payloadBytes': payload_bytes,
            'error': f'grok-bad-response-shape: {e}',
        }

    try:
        triage = json.loads(content)
    except json.JSONDecodeError as e:
        triage = {'_parseError': str(e), '_raw': content}

    grok_ms = int((time.monotonic() - t0) * 1000)
    tokens = {
        'prompt': usage.get('prompt_tokens', 0),
        'completion': usage.get('completion_tokens', 0),
        'total': usage.get('total_tokens', 0),
    }
    print(f"[grok] model={actual_model} ms={grok_ms} tokens={tokens['total']} "
          f"(prompt={tokens['prompt']} completion={tokens['completion']})")

    return {
        'triage': triage,
        'model': actual_model,
        'grokMs': grok_ms,
        'tokens': tokens,
        'payloadBytes': payload_bytes,
    }


# ──────────────────────────────────────────────────────────────
# Session 3 — Dedup engine + GitHub + daily quota + telemetry
# ──────────────────────────────────────────────────────────────

_ATTEMPTS_CAP = 200
_VALID_OUTCOMES = {
    'created', 'commented', 'deferred', 'error',
    'skipped-doc-processed', 'skipped-known-signature',
    'skipped-issue-creation-disabled', 'skipped-kill-switch',
}
_OUTCOME_BUCKET = {
    'created': 'created',
    'commented': 'commented',
    'deferred': 'deferred',
    'error': 'errors',
    # all 'skipped-*' variants map to the 'skipped' counter
}


# ── Signature ─────────────────────────────────────────────────

def _normalize(s):
    if not s:
        return ''
    s = str(s).lower()
    s = re.sub(r'/[\w./-]+:\d+:\d+', '<loc>', s)         # file:line:col
    s = re.sub(r'\b0x[0-9a-f]+\b', '<hex>', s)            # pointers
    s = re.sub(r'\b\d{6,}\b', '<num>', s)                 # long ids
    s = re.sub(r'"[^"]{12,}"', '"<str>"', s)              # long quoted strings
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def compute_signature(tool_call, user_prompt=''):
    """SHA-1 over (tool_name, normalized error message, normalized first
    stack frame, normalized topic). Stable across re-runs of the same bug."""
    tc = tool_call or {}
    err = tc.get('error') or {}
    if isinstance(err, dict):
        msg = err.get('message') or json.dumps(err, sort_keys=True, default=str)[:500]
        stack = err.get('stack') or err.get('stacktrace') or ''
    elif isinstance(err, str):
        msg = err
        stack = ''
    else:
        msg = str(err)
        stack = ''
    first_frame = (stack.splitlines()[0] if isinstance(stack, str) and stack else '')
    topic = (user_prompt or '')[:80]
    raw = '|'.join([
        tc.get('name', ''),
        _normalize(msg),
        _normalize(first_frame),
        _normalize(topic),
    ])
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()


def _first_error_tool(session_doc):
    """Return the first tool_call with a non-empty error, or {}."""
    session_doc = _normalize_session_doc(session_doc)
    for t in (session_doc.get('tools_called') or []):
        err = t.get('error')
        if err and not (isinstance(err, dict) and not err):
            return t
    return {}


def _signature_for_doc(session_doc):
    session_doc = _normalize_session_doc(session_doc)
    return compute_signature(_first_error_tool(session_doc),
                             session_doc.get('user_prompt', ''))


# ── GitHub helpers ────────────────────────────────────────────

def _gh_headers():
    h = {'Accept': 'application/vnd.github+json'}
    if GITHUB_TOKEN:
        h['Authorization'] = f'Bearer {GITHUB_TOKEN}'
    return h


def _github_search_issue_by_marker(repo, marker_value, marker_key):
    """Search issues containing the literal marker `<key>:<value>`.
    Returns the first hit (open or closed), or None."""
    if not GITHUB_TOKEN or not repo:
        return None
    q = f'repo:{repo} "{marker_key}:{marker_value}"'
    try:
        r = requests.get(
            'https://api.github.com/search/issues',
            headers=_gh_headers(),
            params={'q': q},
            timeout=15,
        )
        if r.status_code != 200:
            print(f"[github] search {marker_key}={marker_value[:12]}.. -> {r.status_code} {r.text[:200]}")
            return None
        items = (r.json() or {}).get('items') or []
        return items[0] if items else None
    except Exception as e:
        print(f"[github] search failed: {e}")
        return None


def _github_search_issue_by_signature(repo, sig):
    return _github_search_issue_by_marker(repo, sig, 'grokcoder-signature')


def _github_search_issue_by_doc(repo, doc_id):
    return _github_search_issue_by_marker(repo, doc_id, 'grokcoder-debugger-id')


def _github_get_issue(repo, number):
    if not repo or not number:
        return None
    try:
        r = requests.get(
            f'https://api.github.com/repos/{repo}/issues/{number}',
            headers=_gh_headers(),
            timeout=15,
        )
        if r.status_code == 200:
            return r.json()
        return None
    except Exception as e:
        print(f"[github] get_issue {number} failed: {e}")
        return None


def _github_create_issue(repo, title, body, labels=None):
    if not GITHUB_TOKEN or not repo:
        raise RuntimeError('GITHUB_TOKEN or GITHUB_REPO not configured')
    payload = {'title': title, 'body': body}
    if labels:
        payload['labels'] = list(labels)
    r = requests.post(
        f'https://api.github.com/repos/{repo}/issues',
        headers=_gh_headers(),
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _github_comment_issue(repo, number, body):
    if not GITHUB_TOKEN or not repo:
        raise RuntimeError('GITHUB_TOKEN or GITHUB_REPO not configured')
    r = requests.post(
        f'https://api.github.com/repos/{repo}/issues/{number}/comments',
        headers=_gh_headers(),
        json={'body': body},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


# ── Issue body templates ──────────────────────────────────────

def _format_issue_body(triage, bug, sig):
    triage = triage or {}
    doc_id = bug.get('docId', '')
    src = bug.get('src', '')

    problems = triage.get('problems') or []
    problems_md = '\n'.join(f'- {p}' for p in problems) if problems else '- (none returned)'

    fixes = triage.get('fixes') or []
    fixes_md_parts = []
    for fx in fixes:
        if not isinstance(fx, dict):
            fixes_md_parts.append(f'- {fx}')
            continue
        bits = []
        if fx.get('file'):
            bits.append(f"- **file:** `{fx['file']}`")
        if fx.get('description'):
            bits.append(f"  {fx['description']}")
        if fx.get('patch'):
            bits.append('  ```diff')
            bits.append(f"  {fx['patch']}".replace('\n', '\n  '))
            bits.append('  ```')
        fixes_md_parts.append('\n'.join(bits) if bits else f'- {fx}')
    fixes_md = '\n'.join(fixes_md_parts) if fixes_md_parts else '- (none returned)'

    rca = triage.get('rca') or '(none returned)'

    bug_toon = ''
    try:
        if toons is not None:
            bug_toon = toons.dumps(bug)
    except Exception as e:
        bug_toon = f'<toon encoding failed: {e}>'

    body = f"""> Auto-filed by **grokCoder Debugger**.
> <!-- grokcoder-debugger-id:{doc_id} -->
> <!-- grokcoder-signature:{sig} -->
> First seen in session: `{doc_id}` (`src={src}`)

## Problems
{problems_md}

## Root-Cause Analysis
{rca}

## Suggested Fix(es)
{fixes_md}

---
<details><summary>Original bug payload (TOON)</summary>

```toon
{bug_toon}
```
</details>
"""
    return body


def _format_recurrence_comment(doc_id, count, src=''):
    return (
        "🔁 **Re-occurred** in a new debugger session.\n"
        f"- docId: `{doc_id}`\n"
        f"- src: `{src}`\n"
        f"- total observed occurrences: **{count}**\n"
        "- (filed automatically by grokCoder Debugger)\n"
    )


# ── State mutators (attempts ring buffer + stats + lastTicket) ─

def _record_attempt(state, *, doc_id, signature, outcome,
                    reason=None, issue_number=None, issue_url=None,
                    issue_title=None, severity=None, model=None,
                    grok_ms=0, github_ms=0, total_ms=0, tokens=None,
                    payload_bytes=None, trigger='manual'):
    """Append an attempt to state.attempts[] (ring-buffer cap 200) and bump
    per-day + lifetime counters. Updates state.lastTicket only on 'created'.
    Caller is responsible for holding _state_lock and saving state."""
    if outcome not in _VALID_OUTCOMES:
        print(f"[attempts] invalid outcome={outcome}, coercing to 'error'")
        outcome = 'error'

    _roll_quota_if_new_day(state)
    now = _now_utc_iso()
    safe_doc = (doc_id or 'unknown')[:24]
    att = {
        'id': f"att-{now.replace(':', '-')}-{safe_doc}",
        'at': now,
        'docId': doc_id,
        'signature': signature,
        'outcome': outcome,
        'reason': reason,
        'issueNumber': issue_number,
        'issueUrl': issue_url,
        'issueTitle': issue_title,
        'severity': severity,
        'model': model,
        'grokMs': int(grok_ms or 0),
        'githubMs': int(github_ms or 0),
        'totalMs': int(total_ms or 0),
        'tokens': tokens or {'prompt': 0, 'completion': 0, 'total': 0},
        'payloadBytes': payload_bytes or {'jsonEquivalent': 0, 'toonSent': 0},
        'trigger': trigger,
    }
    attempts = state.setdefault('attempts', [])
    attempts.insert(0, att)
    if len(attempts) > _ATTEMPTS_CAP:
        del attempts[_ATTEMPTS_CAP:]

    bucket_key = _OUTCOME_BUCKET.get(outcome, 'skipped')
    for slot in ('today', 'lifetime'):
        s = state.setdefault('stats', {}).setdefault(slot, {})
        s['attempts'] = s.get('attempts', 0) + 1
        s[bucket_key] = s.get(bucket_key, 0) + 1
        s['tokensTotal'] = s.get('tokensTotal', 0) + (tokens or {}).get('total', 0)
        s['grokMsTotal'] = s.get('grokMsTotal', 0) + int(grok_ms or 0)
        if slot == 'today':
            s['date'] = state.get('quota_date')

    if outcome == 'created':
        state['lastTicket'] = {
            'issueNumber': issue_number,
            'issueUrl': issue_url,
            'issueTitle': issue_title,
            'docId': doc_id,
            'signature': signature,
            'severity': severity,
            'at': now,
            'model': model,
            'grokMs': int(grok_ms or 0),
            'tokens': tokens or {'prompt': 0, 'completion': 0, 'total': 0},
        }

    # Structured per-pipeline-step log line.
    sig_short = (signature or '')[:8]
    tok_total = (tokens or {}).get('total', 0)
    quota = f"{state.get('quota_used', 0)}/{DAILY_ISSUE_QUOTA}"
    print(
        f"[triage] doc={doc_id} sig={sig_short} action={outcome} "
        f"url={issue_url or '-'} grokMs={int(grok_ms or 0)} tokens={tok_total} "
        f"quota={quota} trigger={trigger} reason={reason or '-'}"
    )
    return att


def _gate(state, *, doc_id, signature, trigger):
    """Pre-Grok gate. Returns an attempt dict if blocked, or None to proceed.
    Must be called with _state_lock held; caller saves state."""
    c = state.get('controls') or {}

    if c.get('killSwitch'):
        return _record_attempt(state, doc_id=doc_id, signature=signature,
                               outcome='skipped-kill-switch',
                               reason='killSwitch=true', trigger=trigger)

    if c.get('skipProcessedDocs', True) and doc_id in (state.get('processed_docs') or {}):
        prev = state['processed_docs'][doc_id] or {}
        return _record_attempt(state, doc_id=doc_id, signature=signature,
                               outcome='skipped-doc-processed',
                               reason=f"already filed as #{prev.get('issueNumber')}",
                               issue_number=prev.get('issueNumber'),
                               issue_url=prev.get('issueUrl'),
                               trigger=trigger)

    if c.get('skipKnownSignatures', True) and signature in (state.get('signatures') or {}):
        sig_entry = state['signatures'][signature] or {}
        return _record_attempt(state, doc_id=doc_id, signature=signature,
                               outcome='skipped-known-signature',
                               reason=f"known signature → #{sig_entry.get('issueNumber')}",
                               issue_number=sig_entry.get('issueNumber'),
                               issue_url=sig_entry.get('issueUrl'),
                               issue_title=sig_entry.get('issueTitle'),
                               severity=sig_entry.get('severity'),
                               trigger=trigger)

    return None


# ── Dedup engine: POST /api/issues ────────────────────────────

def _hydrate_signature_from_github(state, repo, signature, doc_id):
    """If state was wiped but GitHub still has the issue, re-hydrate state."""
    existing = _github_search_issue_by_signature(repo, signature)
    if not existing:
        return None
    entry = {
        'issueNumber': existing.get('number'),
        'issueUrl': existing.get('html_url'),
        'issueTitle': existing.get('title'),
        'firstDocId': doc_id,
        'firstSeenAt': _now_utc_iso(),
        'lastDocId': doc_id,
        'lastSeenAt': _now_utc_iso(),
        'count': 1,
        'severity': None,
        'labels': [l.get('name') for l in (existing.get('labels') or [])],
    }
    state.setdefault('signatures', {})[signature] = entry
    return entry


@app.route('/api/issues', methods=['POST'])
def file_issue():
    """Dedup + create-or-comment + quota. Body: {docId, triage, bug, signature?,
    trigger?, model?, grokMs?, tokens?, payloadBytes?}."""
    if not GITHUB_TOKEN:
        return jsonify({'error': 'GITHUB_TOKEN is not set'}), 503
    if not GITHUB_REPO:
        return jsonify({'error': 'GITHUB_REPO is not set'}), 503

    data = request.get_json(silent=True) or {}
    doc_id = data.get('docId')
    triage = data.get('triage') or {}
    bug = data.get('bug') or {}
    if not doc_id:
        return jsonify({'error': 'docId is required'}), 400

    trigger = data.get('trigger') or 'manual'
    model = data.get('model')
    grok_ms = data.get('grokMs', 0)
    tokens = data.get('tokens') or {'prompt': 0, 'completion': 0, 'total': 0}
    payload_bytes = data.get('payloadBytes') or {'jsonEquivalent': 0, 'toonSent': 0}

    # Recompute signature defense-in-depth
    sig = data.get('signature')
    if not sig:
        # Build a synthetic tool_call from the bug payload's first error
        first = (bug.get('toolErrors') or [{}])[0] if bug.get('toolErrors') else {}
        synthetic = {'name': first.get('name', ''),
                     'error': {'message': first.get('error', '')}}
        sig = compute_signature(synthetic, bug.get('userPrompt', ''))

    severity = triage.get('severity')
    title = triage.get('suggestedTitle') or f"[grokCoder] {first_words(triage.get('signatureHint', '') or doc_id, 12)}"
    repo = GITHUB_REPO

    t0 = time.monotonic()
    with _state_lock:
        _roll_quota_if_new_day(_STATE)

        controls = _STATE.get('controls') or {}

        # (0) Kill switch
        if controls.get('killSwitch'):
            att = _record_attempt(_STATE, doc_id=doc_id, signature=sig,
                                  outcome='skipped-kill-switch',
                                  reason='killSwitch=true', trigger=trigger,
                                  model=model, tokens=tokens,
                                  payload_bytes=payload_bytes,
                                  total_ms=int((time.monotonic() - t0) * 1000))
            _save_state(_STATE)
            return jsonify({'status': 'skipped-kill-switch', 'attempt': att}), 200

        # (0b) Issue creation disabled
        if not controls.get('issueCreationEnabled', True):
            att = _record_attempt(_STATE, doc_id=doc_id, signature=sig,
                                  outcome='skipped-issue-creation-disabled',
                                  reason='issueCreationEnabled=false',
                                  trigger=trigger, model=model, tokens=tokens,
                                  payload_bytes=payload_bytes,
                                  total_ms=int((time.monotonic() - t0) * 1000))
            _save_state(_STATE)
            return jsonify({'status': 'skipped-issue-creation-disabled',
                            'attempt': att}), 200

        # (a) Exact docId already processed
        if controls.get('skipProcessedDocs', True) and doc_id in (_STATE.get('processed_docs') or {}):
            prev = _STATE['processed_docs'][doc_id]
            att = _record_attempt(_STATE, doc_id=doc_id, signature=sig,
                                  outcome='skipped-doc-processed',
                                  reason=f"already filed as #{prev.get('issueNumber')}",
                                  issue_number=prev.get('issueNumber'),
                                  issue_url=prev.get('issueUrl'),
                                  trigger=trigger, model=model, tokens=tokens,
                                  payload_bytes=payload_bytes,
                                  total_ms=int((time.monotonic() - t0) * 1000))
            _save_state(_STATE)
            return jsonify({'status': 'skipped-doc-processed',
                            'url': prev.get('issueUrl'),
                            'number': prev.get('issueNumber'),
                            'attempt': att}), 200

        # (b) Known signature → comment on existing issue, no quota
        sig_entry = (_STATE.get('signatures') or {}).get(sig)

        # (c) Belt-and-suspenders: re-hydrate from GitHub if state was wiped
        if not sig_entry and controls.get('skipKnownSignatures', True):
            sig_entry = _hydrate_signature_from_github(_STATE, repo, sig, doc_id)

        if sig_entry and controls.get('skipKnownSignatures', True):
            number = sig_entry.get('issueNumber')
            url = sig_entry.get('issueUrl')
            sig_entry['count'] = sig_entry.get('count', 0) + 1
            sig_entry['lastDocId'] = doc_id
            sig_entry['lastSeenAt'] = _now_utc_iso()

            gh_t0 = time.monotonic()
            github_err = None
            try:
                _github_comment_issue(repo, number,
                                      _format_recurrence_comment(doc_id, sig_entry['count'],
                                                                 bug.get('src', '')))
            except Exception as e:
                github_err = str(e)
                print(f"[github] comment failed on #{number}: {e}")
            github_ms = int((time.monotonic() - gh_t0) * 1000)

            _STATE.setdefault('processed_docs', {})[doc_id] = {
                'signature': sig,
                'issueNumber': number,
                'issueUrl': url,
                'action': 'commented',
                'at': _now_utc_iso(),
            }

            outcome = 'error' if github_err else 'commented'
            att = _record_attempt(_STATE, doc_id=doc_id, signature=sig,
                                  outcome=outcome,
                                  reason=github_err or f"dup-sig={sig[:8]}",
                                  issue_number=number, issue_url=url,
                                  issue_title=sig_entry.get('issueTitle'),
                                  severity=sig_entry.get('severity'),
                                  trigger=trigger, model=model,
                                  grok_ms=grok_ms, github_ms=github_ms,
                                  tokens=tokens, payload_bytes=payload_bytes,
                                  total_ms=int((time.monotonic() - t0) * 1000))
            _save_state(_STATE)
            return jsonify({'status': outcome, 'url': url, 'number': number,
                            'occurrences': sig_entry['count'],
                            'error': github_err, 'attempt': att}), 200

        # (d) Net-new — quota check
        if _STATE.get('quota_used', 0) >= DAILY_ISSUE_QUOTA:
            _STATE.setdefault('deferred', []).append({
                'docId': doc_id, 'signature': sig,
                'at': _now_utc_iso(), 'reason': 'quota',
            })
            att = _record_attempt(_STATE, doc_id=doc_id, signature=sig,
                                  outcome='deferred',
                                  reason=f"quota {_STATE['quota_used']}/{DAILY_ISSUE_QUOTA} reached",
                                  trigger=trigger, model=model, tokens=tokens,
                                  payload_bytes=payload_bytes,
                                  total_ms=int((time.monotonic() - t0) * 1000))
            _save_state(_STATE)
            return jsonify({'status': 'deferred-quota',
                            'used': _STATE['quota_used'],
                            'limit': DAILY_ISSUE_QUOTA,
                            'attempt': att}), 429

        # (e) Create
        labels = ['bug', 'grok-triage',
                  f"severity:{severity or 'medium'}",
                  f"grok-sig:{sig[:8]}"]
        body_md = _format_issue_body(triage, bug, sig)
        gh_t0 = time.monotonic()
        try:
            issue = _github_create_issue(repo, title, body_md, labels=labels)
        except Exception as e:
            github_ms = int((time.monotonic() - gh_t0) * 1000)
            att = _record_attempt(_STATE, doc_id=doc_id, signature=sig,
                                  outcome='error',
                                  reason=f'github-create-failed: {e}',
                                  trigger=trigger, model=model,
                                  grok_ms=grok_ms, github_ms=github_ms,
                                  tokens=tokens, payload_bytes=payload_bytes,
                                  total_ms=int((time.monotonic() - t0) * 1000))
            _save_state(_STATE)
            return jsonify({'status': 'error', 'error': str(e), 'attempt': att}), 502
        github_ms = int((time.monotonic() - gh_t0) * 1000)

        number = issue.get('number')
        url = issue.get('html_url')
        now_iso = _now_utc_iso()
        _STATE.setdefault('signatures', {})[sig] = {
            'issueNumber': number,
            'issueUrl': url,
            'issueTitle': title,
            'firstDocId': doc_id,
            'firstSeenAt': now_iso,
            'lastDocId': doc_id,
            'lastSeenAt': now_iso,
            'count': 1,
            'severity': severity,
            'labels': labels,
        }
        _STATE.setdefault('processed_docs', {})[doc_id] = {
            'signature': sig,
            'issueNumber': number,
            'issueUrl': url,
            'action': 'created',
            'at': now_iso,
        }
        _STATE['quota_used'] = _STATE.get('quota_used', 0) + 1
        att = _record_attempt(_STATE, doc_id=doc_id, signature=sig,
                              outcome='created',
                              issue_number=number, issue_url=url,
                              issue_title=title, severity=severity,
                              trigger=trigger, model=model,
                              grok_ms=grok_ms, github_ms=github_ms,
                              tokens=tokens, payload_bytes=payload_bytes,
                              total_ms=int((time.monotonic() - t0) * 1000))
        _save_state(_STATE)
        return jsonify({'status': 'created', 'url': url, 'number': number,
                        'quotaUsed': _STATE['quota_used'],
                        'quotaLimit': DAILY_ISSUE_QUOTA,
                        'attempt': att}), 200


def first_words(s, n):
    if not s:
        return ''
    parts = re.split(r'\s+', str(s).strip())
    return ' '.join(parts[:n])


# ── Read-only status / telemetry / controls ───────────────────

@app.route('/api/issues/status')
def issues_status():
    """Per-docId triage status. Query: ?docIds=a,b,c"""
    raw = request.args.get('docIds', '')
    doc_ids = [d.strip() for d in raw.split(',') if d.strip()]
    out = {}
    with _state_lock:
        processed = _STATE.get('processed_docs') or {}
        signatures = _STATE.get('signatures') or {}
        deferred_by_doc = {d.get('docId'): d for d in (_STATE.get('deferred') or [])}
        for did in doc_ids:
            entry = processed.get(did)
            if entry:
                sig = entry.get('signature')
                sig_entry = signatures.get(sig) if sig else None
                out[did] = {
                    'hasIssue': bool(entry.get('issueNumber')),
                    'action': entry.get('action'),
                    'issueNumber': entry.get('issueNumber'),
                    'issueUrl': entry.get('issueUrl'),
                    'issueTitle': (sig_entry or {}).get('issueTitle'),
                    'signature': sig,
                    'firstSeenAt': (sig_entry or {}).get('firstSeenAt'),
                    'lastSeenAt': (sig_entry or {}).get('lastSeenAt') or entry.get('at'),
                    'count': (sig_entry or {}).get('count', 1),
                    'severity': (sig_entry or {}).get('severity'),
                }
            elif did in deferred_by_doc:
                d = deferred_by_doc[did]
                out[did] = {'hasIssue': False, 'deferred': True,
                            'reason': d.get('reason', 'quota'),
                            'at': d.get('at')}
            else:
                out[did] = {'hasIssue': False}
    return jsonify(out)


@app.route('/api/issues/recent')
def issues_recent():
    """Most recent ticket actions (created / commented), newest first."""
    try:
        limit = int(request.args.get('limit', '20'))
    except ValueError:
        limit = 20
    with _state_lock:
        attempts = list(_STATE.get('attempts') or [])
    items = [a for a in attempts if a.get('outcome') in ('created', 'commented')]
    items = items[:max(1, min(limit, 200))]
    return jsonify([
        {
            'at': a.get('at'),
            'action': a.get('outcome'),
            'docId': a.get('docId'),
            'signature': a.get('signature'),
            'issueNumber': a.get('issueNumber'),
            'issueUrl': a.get('issueUrl'),
            'issueTitle': a.get('issueTitle'),
            'severity': a.get('severity'),
            'count': 1,
        }
        for a in items
    ])


@app.route('/api/triage/attempts')
def triage_attempts():
    """Per-attempt feed for the Recent triage attempts panel."""
    try:
        limit = int(request.args.get('limit', '50'))
    except ValueError:
        limit = 50
    outcome_filter = (request.args.get('outcome') or 'any').lower()
    with _state_lock:
        attempts = list(_STATE.get('attempts') or [])
    if outcome_filter not in ('any', '', 'all'):
        if outcome_filter == 'skipped':
            attempts = [a for a in attempts if (a.get('outcome') or '').startswith('skipped')]
        elif outcome_filter == 'errors':
            attempts = [a for a in attempts if a.get('outcome') == 'error']
        else:
            attempts = [a for a in attempts if a.get('outcome') == outcome_filter]
    return jsonify(attempts[:max(1, min(limit, 200))])


def _empty_stats():
    return {'attempts': 0, 'created': 0, 'commented': 0,
            'skipped': 0, 'deferred': 0, 'errors': 0,
            'tokensTotal': 0, 'grokMsTotal': 0,
            'avgGrokMs': 0, 'tokensByModel': {}}


def _aggregate(attempts, *, since=None):
    s = _empty_stats()
    grok_calls = 0
    for a in attempts:
        if since and a.get('at') and a['at'] < since:
            continue
        s['attempts'] += 1
        outcome = a.get('outcome') or ''
        bucket = _OUTCOME_BUCKET.get(outcome, 'skipped')
        s[bucket] = s.get(bucket, 0) + 1
        tok = (a.get('tokens') or {}).get('total', 0)
        s['tokensTotal'] += tok
        s['grokMsTotal'] += int(a.get('grokMs') or 0)
        if a.get('grokMs'):
            grok_calls += 1
        if tok and a.get('model'):
            s['tokensByModel'][a['model']] = s['tokensByModel'].get(a['model'], 0) + tok
    s['avgGrokMs'] = int(s['grokMsTotal'] / grok_calls) if grok_calls else 0
    return s


@app.route('/api/triage/stats')
def triage_stats():
    with _state_lock:
        _roll_quota_if_new_day(_STATE)
        attempts = list(_STATE.get('attempts') or [])
        today_stats = dict(_STATE.get('stats', {}).get('today') or _empty_stats())
        lifetime_stats = dict(_STATE.get('stats', {}).get('lifetime') or _empty_stats())
        last_ticket = _STATE.get('lastTicket')
        quota = {'used': _STATE.get('quota_used', 0),
                 'limit': DAILY_ISSUE_QUOTA,
                 'date': _STATE.get('quota_date')}
        _save_state(_STATE)

    since_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%SZ')
    last24h = _aggregate(attempts, since=since_24h)
    last_attempt = attempts[0] if attempts else None

    # Derive avg / tokensByModel for today + lifetime by re-walking attempts
    # (the counters only track totals; this is cheap given the 200-entry cap).
    today_all = _aggregate(attempts)
    today_stats['avgGrokMs'] = today_all['avgGrokMs']
    today_stats['tokensByModel'] = today_all['tokensByModel']
    lifetime_stats.setdefault('avgGrokMs', today_all['avgGrokMs'])
    lifetime_stats.setdefault('tokensByModel', today_all['tokensByModel'])

    return jsonify({
        'today': today_stats,
        'last24h': last24h,
        'lifetime': lifetime_stats,
        'lastTicket': last_ticket,
        'lastAttempt': last_attempt,
        'quota': quota,
    })


@app.route('/api/triage/last-ticket')
def triage_last_ticket():
    with _state_lock:
        return jsonify(_STATE.get('lastTicket'))


@app.route('/api/triage/controls', methods=['GET', 'POST'])
def triage_controls():
    if request.method == 'GET':
        with _state_lock:
            return jsonify(_STATE.get('controls') or {})
    data = request.get_json(silent=True) or {}
    allowed = {'pollerEnabled', 'issueCreationEnabled', 'skipKnownSignatures',
               'skipProcessedDocs', 'modelOverride', 'killSwitch'}
    with _state_lock:
        controls = _STATE.setdefault('controls', {})
        for k, v in data.items():
            if k in allowed:
                controls[k] = v
        controls['updatedAt'] = _now_utc_iso()
        controls['updatedBy'] = data.get('updatedBy') or 'ui'
        # mirror pollerEnabled to legacy top-level key
        if 'pollerEnabled' in data:
            _STATE['pollerEnabled'] = bool(data['pollerEnabled'])
        _save_state(_STATE)
        return jsonify(controls)


@app.route('/api/triage/kill', methods=['POST'])
def triage_kill():
    with _state_lock:
        c = _STATE.setdefault('controls', {})
        c['killSwitch'] = True
        c['pollerEnabled'] = False
        c['issueCreationEnabled'] = False
        c['updatedAt'] = _now_utc_iso()
        c['updatedBy'] = (request.get_json(silent=True) or {}).get('updatedBy') or 'ui'
        _STATE['pollerEnabled'] = False
        _save_state(_STATE)
        return jsonify(c)


@app.route('/api/triage/resume', methods=['POST'])
def triage_resume():
    with _state_lock:
        c = _STATE.setdefault('controls', {})
        c['killSwitch'] = False
        c['pollerEnabled'] = True
        c['issueCreationEnabled'] = True
        c['updatedAt'] = _now_utc_iso()
        c['updatedBy'] = (request.get_json(silent=True) or {}).get('updatedBy') or 'ui'
        _STATE['pollerEnabled'] = True
        _save_state(_STATE)
        return jsonify(c)


# ──────────────────────────────────────────────────────────────
# POST /api/triage/<doc_id> — manual Grok triage (gated)
# ──────────────────────────────────────────────────────────────

@app.route('/api/triage/<doc_id>', methods=['POST'])
def triage_doc(doc_id):
    """Manually triage a single session document with Grok. Honors controls
    (kill switch, skipProcessedDocs, skipKnownSignatures). No GitHub call."""
    if not GROK_API_KEY:
        return jsonify({'error': 'GROK_API_KEY is not set'}), 503

    config = configs[0] if configs else None
    if not config:
        return jsonify({'error': 'No Couchbase cluster configured'}), 503

    doc, err = _fetch_document(config, doc_id)
    if err:
        return err

    bug = _build_bug_payload(doc)
    sig = _signature_for_doc(doc)
    t0 = time.monotonic()

    # Pre-Grok gate: kill switch, already-processed doc, known signature.
    with _state_lock:
        model_override = (_STATE.get('controls') or {}).get('modelOverride')
        gate_att = _gate(_STATE, doc_id=doc_id, signature=sig, trigger='manual')
        if gate_att:
            _save_state(_STATE)
            return jsonify({
                'docId': doc_id,
                'signature': sig,
                'bug': bug,
                'status': gate_att.get('outcome'),
                'reason': gate_att.get('reason'),
                'issueNumber': gate_att.get('issueNumber'),
                'issueUrl': gate_att.get('issueUrl'),
                'triage': None,
                'attempt': gate_att,
            }), 200

    repo_ctx = _fetch_repo_context(GITHUB_REPO)
    telemetry = _call_grok(bug, repo_ctx, model_override=model_override)
    total_ms = int((time.monotonic() - t0) * 1000)

    # Record errors as their own attempt so they show up in the stats UI.
    if telemetry.get('error'):
        with _state_lock:
            _record_attempt(_STATE, doc_id=doc_id, signature=sig,
                            outcome='error', reason=telemetry['error'],
                            model=telemetry.get('model'),
                            grok_ms=telemetry.get('grokMs', 0),
                            tokens=telemetry.get('tokens'),
                            payload_bytes=telemetry.get('payloadBytes'),
                            total_ms=total_ms, trigger='manual')
            _save_state(_STATE)

    status = 502 if telemetry.get('error') else 200
    return jsonify({
        'docId': doc_id,
        'signature': sig,
        'bug': bug,
        'triage': telemetry.get('triage'),
        'model': telemetry.get('model'),
        'tokens': telemetry.get('tokens'),
        'payloadBytes': telemetry.get('payloadBytes'),
        'grokMs': telemetry.get('grokMs'),
        'totalMs': total_ms,
        'error': telemetry.get('error'),
    }), status


@app.route('/api/triage/test-ticket', methods=['POST'])
def triage_test_ticket():
    """Smoke-test the GitHub credentials.

    Body: {"mode": "verify"} (default) or {"mode": "create"}.

      - "verify": GETs /repos/<repo> with the token; confirms read access
        and that the token has issues-write permission. No issue created.
      - "create": files a real GitHub issue titled "[grokCoder] test ticket"
        labelled `grokcoder-test`, with a body explaining how to delete it.
        Does NOT consume the daily quota, does NOT touch state.signatures /
        state.processed_docs, but DOES record a telemetry attempt so the
        action shows up in the Recent triage attempts feed.
    """
    if not GITHUB_TOKEN:
        return jsonify({'ok': False, 'error': 'GITHUB_TOKEN is not set'}), 503
    if not GITHUB_REPO:
        return jsonify({'ok': False, 'error': 'GITHUB_REPO is not set'}), 503

    body = request.get_json(silent=True) or {}
    mode = (body.get('mode') or 'verify').lower()
    repo = GITHUB_REPO
    t0 = time.monotonic()

    # 1) Always verify read access + permissions first.
    try:
        r = requests.get(
            f'https://api.github.com/repos/{repo}',
            headers=_gh_headers(),
            timeout=15,
        )
    except Exception as e:
        return jsonify({'ok': False, 'mode': mode,
                        'error': f'github-unreachable: {e}'}), 502

    if r.status_code == 401:
        return jsonify({'ok': False, 'mode': mode, 'status': 401,
                        'error': 'GITHUB_TOKEN is invalid or expired'}), 401
    if r.status_code == 404:
        return jsonify({'ok': False, 'mode': mode, 'status': 404,
                        'error': f'Repo {repo} not visible to this token '
                                 '(check resource-owner + repo selection)'}), 404
    if r.status_code != 200:
        return jsonify({'ok': False, 'mode': mode, 'status': r.status_code,
                        'error': f'github-{r.status_code}: {r.text[:200]}'}), r.status_code

    repo_json = r.json() or {}
    perms = repo_json.get('permissions') or {}
    can_write = bool(perms.get('push') or perms.get('admin') or perms.get('maintain'))
    verify = {
        'repo': repo_json.get('full_name'),
        'private': repo_json.get('private'),
        'default_branch': repo_json.get('default_branch'),
        'permissions': perms,
        'canIssuesWrite': can_write,
        'githubMs': int((time.monotonic() - t0) * 1000),
    }

    if mode != 'create':
        return jsonify({
            'ok': True, 'mode': 'verify',
            'message': 'Token can read the repo. '
                       'No issue was created. '
                       'Use mode="create" to file a real test issue.',
            **verify,
        })

    # 2) Create-mode: file a real test issue (no quota, no dedup-state).
    now = _now_utc_iso()
    title = f'[grokCoder] test ticket — {now}'
    body_md = (
        '> 🧪 **This is a test ticket** filed by grokCoder Debugger from the '
        '⚙ Settings → "Create test issue" button.\n\n'
        f'- created at: `{now}`\n'
        f'- repo: `{repo}`\n\n'
        'It exists only to confirm the `GITHUB_TOKEN` and `GITHUB_REPO` env '
        'vars are wired up correctly.\n\n'
        '**You can close or delete this issue at any time.** It is NOT '
        'tracked in `state.signatures` / `state.processed_docs` and did NOT '
        'consume any of today\'s daily issue quota.\n'
    )
    gh_t0 = time.monotonic()
    try:
        issue = _github_create_issue(repo, title, body_md,
                                     labels=['grokcoder-test'])
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else 500
        text = e.response.text[:200] if e.response is not None else ''
        return jsonify({'ok': False, 'mode': 'create', 'status': status,
                        'error': f'github-create-failed-{status}: {text}',
                        **verify}), 502
    except Exception as e:
        return jsonify({'ok': False, 'mode': 'create',
                        'error': f'github-create-failed: {e}',
                        **verify}), 502
    github_ms = int((time.monotonic() - gh_t0) * 1000)

    number = issue.get('number')
    url = issue.get('html_url')

    # Record telemetry so the test shows in the attempts feed, but with a
    # synthetic docId so it doesn't pollute processed_docs / signatures.
    with _state_lock:
        _record_attempt(
            _STATE,
            doc_id=f'test-ticket:{now}',
            signature='test',
            outcome='created',
            issue_number=number, issue_url=url, issue_title=title,
            severity='low', trigger='test',
            github_ms=github_ms,
            total_ms=int((time.monotonic() - t0) * 1000),
            reason='manual test ticket (Settings → Create test issue)',
        )
        _save_state(_STATE)

    return jsonify({
        'ok': True, 'mode': 'create',
        'message': f'Test issue #{number} created. You can close it.',
        'issueNumber': number, 'issueUrl': url, 'issueTitle': title,
        'githubMs': github_ms,
        **verify,
    })


@app.route('/api/triage/health')
def triage_health():
    """Report Grok / GitHub / quota / poller health for the auto-triage pipeline."""
    with _state_lock:
        _roll_quota_if_new_day(_STATE)
        quota_used = _STATE.get('quota_used', 0)
        quota_date = _STATE.get('quota_date', _today_utc())
        poller_block = _STATE.get('poller', {}) or {}
        controls = _STATE.get('controls', {}) or {}
        _save_state(_STATE)
    return jsonify({
        'grok': {
            'configured': bool(GROK_API_KEY),
            'model': controls.get('modelOverride') or GROK_MODEL,
            'timeoutSec': GROK_TIMEOUT_SEC,
        },
        'github': {
            'configured': bool(GITHUB_TOKEN),
            'repo': GITHUB_REPO,
        },
        'quota': {
            'used': quota_used,
            'limit': DAILY_ISSUE_QUOTA,
            'date': quota_date,
        },
        'poller': {
            'enabled': bool(controls.get('pollerEnabled', POLLER_ENABLED)),
            'intervalSec': POLL_INTERVAL_SEC,
            'lastRun': poller_block.get('lastRun'),
            'lastError': poller_block.get('lastError'),
        },
    })


# ──────────────────────────────────────────────────────────────
# Session 4 — Background poller
# ──────────────────────────────────────────────────────────────

_poller_state = {
    'lastRun': None,
    'lastError': None,
    'running': False,
    'thread': None,
}
_poller_lock = threading.Lock()


def _discover_error_docs(limit=50):
    """N1QL query for sessions whose tools_called[*].error is non-empty."""
    if not configs:
        return []
    cfg = configs[0]
    bucket = cfg['bucket']
    scope = cfg.get('scope', 'continue')
    collection = cfg.get('collection', 'report')
    fqn = f"`{bucket}`.`{scope}`.`{collection}`"
    # Match both shapes:
    #   1) flat 'report' shape: tools_called[*].error != {}
    #   2) Continue chat shape: chatHistory[*].toolCallStates[*].status='errored'
    n1ql = f"""
        SELECT META(d).id AS docId, d.src, d.user_prompt,
               d.tools_called, d.chat_history, d.chatHistory
        FROM {fqn} d
        WHERE (ANY t IN d.tools_called SATISFIES
                 t.error IS NOT MISSING AND t.error != {{}} END)
           OR (ANY h IN d.chatHistory SATISFIES
                 ANY s IN h.toolCallStates SATISFIES
                   s.status = "errored" END
               END)
        ORDER BY META(d).id DESC
        LIMIT {int(limit)}
    """
    return query_couchbase(n1ql, config=cfg) or []


def _process_poller_doc(doc, trigger='poller'):
    """Run the full triage pipeline for one error-bearing doc.

    Two short-circuits that avoid spending Grok tokens:
      1. signature already known  → comment via /api/issues
      2. daily quota exhausted    → record 'deferred' and stop
    Otherwise: call Grok, then file via /api/issues (which handles
    create-vs-comment + state mutations + telemetry recording).
    Returns the resulting outcome string.
    """
    doc_id = doc.get('docId') or doc.get('id') or ''
    if not doc_id:
        return 'skipped-no-doc-id'
    bug = _build_bug_payload(doc)
    sig = _signature_for_doc(doc)

    # Pre-Grok gate (controls + processed-doc + known-sig).
    with _state_lock:
        _roll_quota_if_new_day(_STATE)
        controls = _STATE.get('controls') or {}
        gate_att = _gate(_STATE, doc_id=doc_id, signature=sig, trigger=trigger)
        if gate_att:
            _save_state(_STATE)
            return gate_att.get('outcome')
        # Quota guard before spending Grok tokens.
        if _STATE.get('quota_used', 0) >= DAILY_ISSUE_QUOTA:
            _STATE.setdefault('deferred', []).append({
                'docId': doc_id, 'signature': sig,
                'at': _now_utc_iso(), 'reason': 'quota',
            })
            _record_attempt(
                _STATE, doc_id=doc_id, signature=sig, outcome='deferred',
                reason=f"quota {_STATE['quota_used']}/{DAILY_ISSUE_QUOTA} reached",
                trigger=trigger,
            )
            _save_state(_STATE)
            return 'deferred'
        model_override = controls.get('modelOverride')

    # Grok call (outside lock — long-running).
    t0 = time.monotonic()
    repo_ctx = _fetch_repo_context(GITHUB_REPO)
    telemetry = _call_grok(bug, repo_ctx, model_override=model_override)
    total_ms = int((time.monotonic() - t0) * 1000)

    if telemetry.get('error'):
        with _state_lock:
            _record_attempt(
                _STATE, doc_id=doc_id, signature=sig, outcome='error',
                reason=telemetry['error'],
                model=telemetry.get('model'),
                grok_ms=telemetry.get('grokMs', 0),
                tokens=telemetry.get('tokens'),
                payload_bytes=telemetry.get('payloadBytes'),
                total_ms=total_ms, trigger=trigger,
            )
            _save_state(_STATE)
        return 'error'

    # File via the existing dedup engine (handles create vs comment vs
    # defer, plus state mutations + telemetry). Use Flask test_client to
    # avoid duplicating that logic.
    payload = {
        'docId': doc_id,
        'signature': sig,
        'triage': telemetry.get('triage'),
        'bug': bug,
        'trigger': trigger,
        'model': telemetry.get('model'),
        'grokMs': telemetry.get('grokMs'),
        'tokens': telemetry.get('tokens'),
        'payloadBytes': telemetry.get('payloadBytes'),
    }
    try:
        with app.test_client() as client:
            resp = client.post('/api/issues', json=payload)
            return (resp.get_json() or {}).get('status', 'unknown')
    except Exception as e:
        print(f"[poller] /api/issues failed for {doc_id}: {e}")
        with _state_lock:
            _record_attempt(
                _STATE, doc_id=doc_id, signature=sig, outcome='error',
                reason=f'file-issue-failed: {e}',
                model=telemetry.get('model'),
                grok_ms=telemetry.get('grokMs', 0),
                tokens=telemetry.get('tokens'),
                payload_bytes=telemetry.get('payloadBytes'),
                total_ms=total_ms, trigger=trigger,
            )
            _save_state(_STATE)
        return 'error'


def _drain_deferred():
    """Re-drain deferred docs (oldest first) when quota frees up.
    Called at the top of every poller tick — also handles UTC-midnight
    rollover naturally because _roll_quota_if_new_day zeroes quota_used."""
    with _state_lock:
        _roll_quota_if_new_day(_STATE)
        if not _STATE.get('deferred'):
            return {'drained': 0, 'skipped': 'empty'}
        if _STATE.get('quota_used', 0) >= DAILY_ISSUE_QUOTA:
            return {'drained': 0, 'skipped': 'quota-still-full'}
        # Snapshot + clear; failed ones will get re-deferred by _process_poller_doc.
        snapshot = list(_STATE['deferred'])
        _STATE['deferred'] = []
        _save_state(_STATE)
        cfg = configs[0] if configs else None

    if not cfg:
        return {'drained': 0, 'skipped': 'no-cluster'}

    outcomes = {}
    drained = 0
    for entry in snapshot:
        with _state_lock:
            if _STATE.get('quota_used', 0) >= DAILY_ISSUE_QUOTA:
                # Quota filled up mid-drain — push remaining back to deferred.
                remaining = snapshot[snapshot.index(entry):]
                _STATE.setdefault('deferred', []).extend(remaining)
                _save_state(_STATE)
                outcomes['re-deferred'] = outcomes.get('re-deferred', 0) + len(remaining)
                break

        doc_id = entry.get('docId')
        if not doc_id:
            continue
        doc, err = _fetch_document(cfg, doc_id)
        if err or not doc:
            # Source doc gone — drop the deferred entry.
            outcomes['drop-missing'] = outcomes.get('drop-missing', 0) + 1
            print(f"[poller] drain: dropping missing deferred doc {doc_id}")
            continue
        doc.setdefault('docId', doc_id)
        try:
            out = _process_poller_doc(doc, trigger='poller-drain')
        except Exception as e:
            print(f"[poller] drain error {doc_id}: {e}")
            out = 'error'
        outcomes[out] = outcomes.get(out, 0) + 1
        drained += 1

    if outcomes:
        print(f"[poller] drained={drained} outcomes={outcomes}")
    return {'drained': drained, 'outcomes': outcomes}


def _poller_run_once():
    """Single poller iteration (used by the loop and /api/poller/run-now)."""
    with _poller_lock:
        if _poller_state.get('running'):
            return {'skipped': 'already-running'}
        _poller_state['running'] = True
    try:
        # Re-read controls every tick so GUI changes apply immediately.
        with _state_lock:
            controls = _STATE.get('controls') or {}
            enabled = controls.get('pollerEnabled', POLLER_ENABLED)
            kill = controls.get('killSwitch', False)
        if kill or not enabled:
            return {'skipped': 'disabled', 'killSwitch': kill, 'enabled': enabled}

        # Drain any deferred docs first (oldest first) so a fresh UTC day
        # immediately works through yesterday's backlog before scanning for
        # newer ones.
        drain_result = _drain_deferred()

        docs = _discover_error_docs(limit=50)
        outcomes = {}
        for doc in docs:
            try:
                out = _process_poller_doc(doc, trigger='poller')
            except Exception as e:
                print(f"[poller] doc-error {doc.get('docId')}: {e}")
                out = 'error'
            outcomes[out] = outcomes.get(out, 0) + 1
            # Re-check kill switch / poller flag between docs.
            with _state_lock:
                c = _STATE.get('controls') or {}
                if c.get('killSwitch') or not c.get('pollerEnabled', POLLER_ENABLED):
                    break

        now = _now_utc_iso()
        with _state_lock:
            p = _STATE.setdefault('poller', {})
            p['lastRun'] = now
            p['lastError'] = None
            _save_state(_STATE)
        _poller_state['lastRun'] = now
        _poller_state['lastError'] = None
        print(f"[poller] tick scanned={len(docs)} outcomes={outcomes} "
              f"drained={drain_result.get('drained', 0)}")
        return {'docsScanned': len(docs), 'outcomes': outcomes,
                'deferredDrain': drain_result, 'at': now}
    except Exception as e:
        msg = str(e)
        _poller_state['lastError'] = msg
        try:
            with _state_lock:
                _STATE.setdefault('poller', {})['lastError'] = msg
                _save_state(_STATE)
        except Exception:
            pass
        print(f"[poller] error: {e}")
        return {'error': msg}
    finally:
        with _poller_lock:
            _poller_state['running'] = False


def _poller_loop():
    print(f"[poller] loop started (interval={POLL_INTERVAL_SEC}s)")
    while True:
        try:
            _poller_run_once()
        except Exception as e:
            print(f"[poller] loop error: {e}")
        time.sleep(POLL_INTERVAL_SEC)


def _maybe_start_poller():
    """Start the poller thread once if all required env is configured."""
    if not POLLER_ENABLED:
        print("[poller] not started: POLLER_ENABLED=false")
        return
    if not GROK_API_KEY:
        print("[poller] not started: GROK_API_KEY is empty")
        return
    if not GITHUB_TOKEN:
        print("[poller] not started: GITHUB_TOKEN is empty")
        return
    with _poller_lock:
        existing = _poller_state.get('thread')
        if existing and existing.is_alive():
            print("[poller] already running")
            return
        t = threading.Thread(target=_poller_loop, daemon=True, name='triage-poller')
        _poller_state['thread'] = t
        t.start()
    print("[poller] started")


# ── Poller control endpoints ──────────────────────────────────

@app.route('/api/poller/state')
def poller_get_state():
    with _state_lock:
        _roll_quota_if_new_day(_STATE)
        controls = _STATE.get('controls') or {}
        quota_used = _STATE.get('quota_used', 0)
        poller_block = _STATE.get('poller', {}) or {}
        _save_state(_STATE)
    return jsonify({
        'enabled': bool(controls.get('pollerEnabled', POLLER_ENABLED)),
        'killSwitch': bool(controls.get('killSwitch', False)),
        'intervalSec': POLL_INTERVAL_SEC,
        'lastRun': poller_block.get('lastRun') or _poller_state.get('lastRun'),
        'lastError': poller_block.get('lastError') or _poller_state.get('lastError'),
        'running': bool(_poller_state.get('running')),
        'threadAlive': bool(_poller_state.get('thread') and _poller_state['thread'].is_alive()),
        'quotaUsed': quota_used,
        'quotaLimit': DAILY_ISSUE_QUOTA,
        'configured': bool(GROK_API_KEY and GITHUB_TOKEN),
    })


@app.route('/api/poller/pause', methods=['POST'])
def poller_pause():
    with _state_lock:
        c = _STATE.setdefault('controls', {})
        c['pollerEnabled'] = False
        c['updatedAt'] = _now_utc_iso()
        c['updatedBy'] = (request.get_json(silent=True) or {}).get('updatedBy') or 'api'
        _STATE['pollerEnabled'] = False
        _save_state(_STATE)
    return jsonify({'pollerEnabled': False})


@app.route('/api/poller/resume', methods=['POST'])
def poller_resume():
    with _state_lock:
        c = _STATE.setdefault('controls', {})
        c['pollerEnabled'] = True
        c['updatedAt'] = _now_utc_iso()
        c['updatedBy'] = (request.get_json(silent=True) or {}).get('updatedBy') or 'api'
        _STATE['pollerEnabled'] = True
        _save_state(_STATE)
    # Make sure a thread is actually running to honor the new state.
    _maybe_start_poller()
    return jsonify({'pollerEnabled': True})


@app.route('/api/poller/run-now', methods=['POST'])
def poller_run_now():
    """Trigger a single iteration synchronously (does NOT need the thread)."""
    result = _poller_run_once()
    return jsonify(result)


@app.route('/')
def index():
    clusters = [c['name'] for c in configs]
    return render_template('index.html', clusters=clusters)


# Start the poller at import time so it works under both `python app.py`
# and a WSGI server (gunicorn/uwsgi), but guard against double-starts.
_maybe_start_poller()


if __name__ == '__main__':
    print("=" * 60)
    print("🐛 Error Dashboard v3 for Grok AI Coder")
    print("=" * 60)
    print(f"Couchbase: {CB_HOST} / {CB_BUCKET}")
    print()
    print("NEW: AI Inputs tab - generates debug package for AI assistants")
    print()
    print(f"Open: http://localhost:7777")
    print("=" * 60)
    app.run(host='0.0.0.0', port=7777, debug=True, use_reloader=False)
