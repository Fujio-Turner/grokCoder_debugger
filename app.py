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
import json
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, jsonify, request
import requests
from requests.auth import HTTPBasicAuth
from urllib.parse import urlparse

app = Flask(__name__, template_folder='templates', static_folder='static')

# Couchbase config from environment variables
CB_HOST = os.environ.get('CB_HOST', '')
CB_USER = os.environ.get('CB_USER', '')
CB_PASS = os.environ.get('CB_PASS', '')
CB_BUCKET = os.environ.get('CB_BUCKET', 'grokCoder')
CB_SCOPE = os.environ.get('CB_SCOPE', 'continue')
CB_COLLECTION = os.environ.get('CB_COLLECTION', 'report')

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
        'collection': CB_COLLECTION
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

def _fetch_document(config, doc_key):
    """Fetch a document from Capella Data API. Returns (doc_dict, None) or (None, error_response)."""
    bucket = config['bucket']
    scope = config.get('scope', 'continue')
    collection = config.get('collection', 'report')
    host_str = config['host']
    if host_str.startswith('couchbases://'):
        host_str = host_str.replace('couchbases://', '')
    url = f"https://{host_str}/v1/buckets/{bucket}/scopes/{scope}/collections/{collection}/documents/{doc_key}"
    auth = HTTPBasicAuth(config['user'], config['pass'])
    try:
        resp = requests.get(url, auth=auth, timeout=30)
        resp.raise_for_status()
        return resp.json(), None
    except requests.exceptions.HTTPError as e:
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

@app.route('/')
def index():
    clusters = [c['name'] for c in configs]
    return render_template('index.html', clusters=clusters)


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
