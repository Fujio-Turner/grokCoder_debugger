let allErrors = [];
let allReports = [];
let rawData = null;
let sessionCache = {};
let currentSession = null;
let currentCluster = null;

// ECharts instances
let toolErrorChart;

function initCharts() {
    toolErrorChart = echarts.init(document.getElementById('toolErrorChart'));
    window.addEventListener('resize', () => {
        toolErrorChart.resize();
    });
}

function updateCharts(data) {
    // Tool error counts by toolName
    const toolCounts = {};
    data.errors.forEach(e => {
        const name = e.toolName || 'unknown';
        toolCounts[name] = (toolCounts[name] || 0) + 1;
    });

    const chartData = Object.entries(toolCounts)
        .map(([name, value]) => ({ value, name }))
        .sort((a, b) => b.value - a.value);

    toolErrorChart.setOption({
        backgroundColor: 'transparent',
        tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
        legend: { show: false },
        series: [{
            type: 'pie',
            radius: ['35%', '70%'],
            itemStyle: { borderRadius: 4, borderColor: '#252526', borderWidth: 2 },
            label: { show: true, color: '#ccc', fontSize: 11 },
            emphasis: { label: { show: true, fontSize: 13, fontWeight: 'bold' } },
            data: chartData
        }]
    });
}

async function loadClusters() {
    const resp = await fetch('/api/clusters');
    const clusters = await resp.json();
    const select = document.getElementById('clusterSelect');
    select.innerHTML = clusters.map(c => `<option value="${c}">${c}</option>`).join('');
    if (clusters.length > 0) {
        currentCluster = clusters[0];
        select.value = clusters[0];
    }
    select.addEventListener('change', () => {
        currentCluster = select.value;
        loadData();
    });
}

async function checkHealth() {
    const el = document.getElementById('connStatus');
    el.textContent = '⏳';
    el.title = 'Checking connection...';
    el.className = 'conn-status';
    try {
        const resp = await fetch('/api/health');
        const data = await resp.json();
        if (data.ok) {
            el.textContent = '🟢 Connected';
            el.title = 'Couchbase Capella connection OK';
            el.className = 'conn-status ok';
        } else {
            el.textContent = '🔴 Error';
            el.title = data.error || 'Connection failed';
            el.className = 'conn-status fail';
        }
    } catch (e) {
        el.textContent = '🔴 Error';
        el.title = 'Could not reach health endpoint: ' + e.message;
        el.className = 'conn-status fail';
    }
}

async function loadData() {
    const timeRange = document.getElementById('timeRange').value;
    checkHealth();
    const resp = await fetch(`/api/errors?range=${timeRange}`);
    const data = await resp.json();

    document.getElementById('toolErrorCount').textContent = data.stats.toolErrors || 0;
    document.getElementById('totalReportCount').textContent = data.stats.totalReports || 0;
    document.getElementById('uniqueDocCount').textContent = data.stats.uniqueSessions || 0;

    allErrors = data.errors || [];
    allReports = data.reports || [];
    rawData = data;
    updateCharts(data);
    await loadIssueStatuses();
    filterTable();
    // Refresh triage observability panels in parallel.
    loadStats(); loadAttempts(); loadDeferred(); refreshTriageBar();
}

let searchTimer = null;

function filterTable() {
    const search = document.getElementById('searchBox').value.trim();

    // If empty, show the normal errors list
    if (!search) {
        renderTable(allErrors);
        return;
    }

    // Debounce: wait 400ms after typing stops before hitting FTS
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => runFtsSearch(search), 400);
}

async function runFtsSearch(query) {
    const tbody = document.getElementById('tableBody');
    tbody.innerHTML = '<tr><td colspan="6" class="empty">🔍 Searching...</td></tr>';

    try {
        const resp = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
        const data = await resp.json();

        if (data.error) {
            tbody.innerHTML = `<tr><td colspan="6" class="empty">Search error: ${escapeHtml(data.error)}</td></tr>`;
            return;
        }

        if (!data.hits || data.hits.length === 0) {
            tbody.innerHTML = `<tr><td colspan="6" class="empty">No results for "${escapeHtml(query)}"</td></tr>`;
            return;
        }

        const rows = data.hits.map(hit => renderSearchHitRow(hit)).join('');
        tbody.innerHTML = `<tr><td colspan="6" style="padding:8px;color:#888;">🔍 ${data.total} results (${(data.took / 1000000).toFixed(1)}ms)</td></tr>` + rows;
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="6" class="empty">Search failed: ${e.message}</td></tr>`;
    }
}

function renderSearchHitRow(hit) {
    const docId = hit.docId || '';
    const fields = hit.fields || {};
    const fragments = hit.fragments || {};

    // Build description from fragments (highlighted) or fields
    let desc = '';
    if (fragments.user_prompt) {
        desc = fragments.user_prompt.join(' ... ');
    } else if (fields.user_prompt) {
        desc = escapeHtml(String(fields.user_prompt).slice(0, 200));
    }

    const src = fields.src || '';
    const score = hit.score || 0;

    return `
        <tr>
            <td><span class="type-badge type-error">${escapeHtml(src)}</span></td>
            <td class="timestamp">${score}</td>
            <td><a class="session-link" onclick="viewReport('${escapeAttr(docId)}')">${docId.slice(0, 20)}...</a></td>
            <td class="description">${desc || escapeHtml(docId)}</td>
            ${renderTicketCell((window.issueStatusMap || {})[docId], docId)}
            <td>
                <button class="view-btn" onclick="viewReport('${escapeAttr(docId)}')">View</button>
            </td>
        </tr>
    `;
}

function renderTable(errors) {
    const tbody = document.getElementById('tableBody');
    if (errors.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty">No errors found 🎉</td></tr>';
        return;
    }
    tbody.innerHTML = errors.map(e => renderErrorRow(e)).join('');
}

function renderErrorRow(e) {
    const docId = e.sessionId || '';
    const toolName = e.toolName || e.type || 'unknown';
    const execTime = e.execTime != null ? e.execTime.toFixed(2) + 's' : '-';
    return `
        <tr>
            <td><span class="type-badge type-error">${escapeHtml(toolName)}</span></td>
            <td class="timestamp">${execTime}</td>
            <td><a class="session-link" onclick="viewReport('${escapeAttr(docId)}')">${docId.slice(0, 12)}...</a></td>
            <td class="description">${escapeHtml(e.description)}</td>
            ${renderTicketCell((window.issueStatusMap || {})[docId], docId)}
            <td>
                <button class="view-btn" onclick="viewReport('${escapeAttr(docId)}')">View</button>
            </td>
        </tr>
    `;
}

// ── Session 5: Ticket cell renderer ─────────────────────────
function renderTicketCell(s, docId) {
    const triageBtn = `<button class="ticket-btn" onclick="event.stopPropagation(); triageRow('${escapeAttr(docId)}')">Triage now</button>`;
    if (!s) return `<td class="ticket-none">⚪ — ${triageBtn}</td>`;
    if (s.deferred) {
        return `<td class="ticket-deferred" title="${escapeAttr(s.reason || '')}">⏳ Deferred${triageBtn}</td>`;
    }
    if (s.hasIssue) {
        const icon = s.action === 'commented' ? '🔁' : '🟢';
        const sev  = s.severity ? `<span class="sev sev-${escapeAttr(s.severity)}">${escapeHtml(s.severity)}</span>` : '';
        const seen = (s.count && s.count > 1) ? `<small>seen ×${s.count}</small>` : '';
        const when = s.lastSeenAt ? `<small title="${escapeAttr(s.lastSeenAt)}">${timeAgo(s.lastSeenAt)}</small>` : '';
        const num  = s.issueNumber ? `#${s.issueNumber}` : '?';
        const href = s.issueUrl || '#';
        return `<td class="ticket-cell">${icon} <a href="${escapeAttr(href)}" target="_blank" rel="noopener">${num}</a> ${sev}${seen}${when}</td>`;
    }
    return `<td class="ticket-none">⚪ — ${triageBtn}</td>`;
}

// ── Session 5: timeAgo helper (5m, 3h, 2d, …) ───────────────
function timeAgo(iso) {
    if (!iso) return '';
    const then = new Date(iso).getTime();
    if (isNaN(then)) return '';
    const sec = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (sec < 5)   return 'just now';
    if (sec < 60)  return sec + 's ago';
    if (sec < 3600)        return Math.floor(sec / 60)   + 'm ago';
    if (sec < 86400)       return Math.floor(sec / 3600) + 'h ago';
    if (sec < 86400 * 14)  return Math.floor(sec / 86400) + 'd ago';
    return Math.floor(sec / 86400) + 'd ago';
}

// ── Session 5: batch-fetch issue status for visible docIds ──
async function loadIssueStatuses() {
    if (!allErrors || allErrors.length === 0) {
        window.issueStatusMap = {};
        return;
    }
    const docIds = [...new Set(allErrors.map(e => e.sessionId).filter(Boolean))];
    if (docIds.length === 0) { window.issueStatusMap = {}; return; }
    try {
        const resp = await fetch(`/api/issues/status?docIds=${encodeURIComponent(docIds.join(','))}`);
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        window.issueStatusMap = await resp.json();
    } catch (e) {
        console.warn('loadIssueStatuses failed:', e);
        window.issueStatusMap = {};
    }
}

function escapeAttr(str) {
    if (!str) return '';
    return str.replace(/'/g, "\\'").replace(/"/g, '&quot;');
}

function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

async function viewReport(docId) {
    document.getElementById('modalSessionId').textContent = docId.slice(0, 16) + '...';
    document.getElementById('jsonModal').classList.add('active');

    document.getElementById('tabFull').innerHTML = 'Loading report data...';
    document.getElementById('tabAi').innerHTML = 'Loading...';

    // Find the error and report for this docId
    const error = allErrors.find(e => e.sessionId === docId);
    const report = allReports.find(r => r.docId === docId);

    // Try loading full session doc
    let session = sessionCache[docId];
    if (!session) {
        try {
            const resp = await fetch(`/api/session/${encodeURIComponent(docId)}`);
            if (resp.ok) {
                session = await resp.json();
                sessionCache[docId] = session;
            } else {
                const errBody = await resp.json().catch(() => ({}));
                console.warn(`GET session ${docId}: ${resp.status}`, errBody.error || errBody);
                session = null;
            }
        } catch (err) {
            console.warn(`GET session ${docId} failed:`, err);
            session = null;
        }
    }
    currentSession = session;

    // Render Full JSON tab
    const fullData = session || report || error || {};
    renderFullJson(fullData);

    // Render AI Inputs tab
    renderAiInputs(error, report, session);

    // Render Triage tab (sticky-header for existing tickets + Triage button)
    renderTriageTab(docId, error, report, session);

    switchTab('ai');
}

function renderFullJson(data) {
    const container = document.getElementById('tabFull');
    const json = JSON.stringify(data, null, 2);
    const lines = json.split('\n');

    const html = lines.map((line, i) => {
        return `<div class="json-line" id="line-${i}">${i + 1}: ${syntaxHighlight(escapeHtml(line))}</div>`;
    }).join('');

    container.innerHTML = html;
}

function tryParseJson(val) {
    if (typeof val !== 'string') return val;
    try {
        const parsed = JSON.parse(val);
        if (typeof parsed === 'object' && parsed !== null) return parsed;
    } catch (e) {}
    return val;
}

function renderJsonValue(val) {
    val = tryParseJson(val);
    if (typeof val === 'object' && val !== null) {
        return `<pre class="debug-json">${syntaxHighlight(escapeHtml(JSON.stringify(val, null, 2)))}</pre>`;
    }
    return `<pre class="debug-json">${escapeHtml(String(val))}</pre>`;
}

function renderToolCard(tool, index) {
    const name = tool.name || `tool_${index}`;
    const hasError = tool.error && Object.keys(tool.error).length > 0;
    const execTime = (tool.meta || {}).exec_time;
    const priorityClass = hasError ? 'priority-high' : 'priority-low';
    const icon = hasError ? '⚠️' : '🔧';
    const timeStr = execTime != null ? `${execTime}s` : '';

    let body = '';

    if (tool.params) {
        body += `<div class="debug-label">Params</div><div class="debug-value">${renderJsonValue(tool.params)}</div>`;
    }
    if (hasError) {
        body += `<div class="debug-label">Error</div><div class="debug-value error-highlight">${renderJsonValue(tool.error)}</div>`;
    }
    if (tool.response) {
        let responseDisplay = tool.response;
        if (responseDisplay.content && Array.isArray(responseDisplay.content)) {
            responseDisplay.content = responseDisplay.content.map(c => {
                if (c.text) c.text = tryParseJson(c.text);
                return c;
            });
        }
        if (responseDisplay.structuredContent) {
            responseDisplay = responseDisplay.structuredContent;
        }
        body += `<div class="debug-label">Response</div><div class="debug-value">${renderJsonValue(responseDisplay)}</div>`;
    }

    return `
        <div class="debug-section ${priorityClass}">
            <div class="debug-section-header tool-card-header" onclick="this.parentElement.classList.toggle('collapsed')">
                <span>${icon} #${index} — ${escapeHtml(name)} ${timeStr ? `<span class="tool-time">(${timeStr})</span>` : ''}</span>
                <span class="collapse-arrow">▼</span>
            </div>
            <div class="debug-section-body tool-card-body">${body}</div>
        </div>
    `;
}

function renderChatEntry(entry, index) {
    const role = entry.role || 'unknown';
    const icon = role === 'user' ? '👤' : role === 'assistant' ? '🤖' : '💬';
    let content = entry.content || '';
    content = tryParseJson(content);

    return `
        <div class="debug-section priority-low">
            <div class="debug-section-header tool-card-header" onclick="this.parentElement.classList.toggle('collapsed')">
                <span>${icon} #${index} — ${escapeHtml(role)}</span>
                <span class="collapse-arrow">▼</span>
            </div>
            <div class="debug-section-body tool-card-body">${renderJsonValue(content)}</div>
        </div>
    `;
}

function renderAiInputs(error, report, session) {
    const container = document.getElementById('tabAi');
    const data = session || {};

    const userPrompt = data.user_prompt || error?.userPrompt || report?.user_prompt || '';
    const src = data.src || report?.src || '';
    const toolsCalled = data.tools_called || [];
    const chatHistory = data.chat_history || [];

    let html = `<h3>🤖 Session Details</h3>`;

    // Overview card
    html += `
        <div class="debug-section priority-medium">
            <div class="debug-section-header"><span>📋 Overview</span></div>
            <div class="debug-section-body">
                <div class="overview-grid">
                    <div class="debug-label">Source</div>
                    <div class="debug-value">${escapeHtml(src) || '-'}</div>
                    <div class="debug-label">Tools Called</div>
                    <div class="debug-value">${toolsCalled.length}</div>
                    <div class="debug-label">Chat History</div>
                    <div class="debug-value">${chatHistory.length} entries</div>
                </div>
            </div>
        </div>
    `;

    // User Prompt
    if (userPrompt) {
        html += `
            <div class="debug-section priority-high">
                <div class="debug-section-header">
                    <span>📝 User Prompt</span>
                    <button class="copy-btn" onclick="event.stopPropagation(); copyToClipboard(this, ${JSON.stringify(JSON.stringify(userPrompt))})">📋</button>
                </div>
                <div class="debug-section-body">
                    <pre class="debug-json">${escapeHtml(userPrompt)}</pre>
                </div>
            </div>
        `;
    }

    // Tools Called
    if (toolsCalled.length > 0) {
        html += `<h3>🔧 Tools Called (${toolsCalled.length})</h3>`;
        html += toolsCalled.map((t, i) => renderToolCard(t, i)).join('');
    }

    // Chat History
    if (chatHistory.length > 0) {
        html += `<h3>💬 Chat History (${chatHistory.length})</h3>`;
        html += chatHistory.map((c, i) => renderChatEntry(c, i)).join('');
    }

    // Copy full JSON button at bottom
    const fullJson = JSON.stringify(data, null, 2);
    html += `
        <div class="ai-inputs-intro" style="margin-top: 20px;">
            <p><strong>Copy full session JSON for your AI assistant.</strong></p>
            <button class="copy-btn large" onclick="copyToClipboard(this, ${JSON.stringify(fullJson)})">
                📋 Copy Full JSON
            </button>
        </div>
    `;

    container.innerHTML = html;
}

function syntaxHighlight(json) {
    return json
        .replace(/"([^"]+)":/g, '<span class="json-key">"$1"</span>:')
        .replace(/: "([^"]*)"/g, ': <span class="json-string">"$1"</span>')
        .replace(/: (\d+)/g, ': <span class="json-number">$1</span>')
        .replace(/: (true|false)/g, ': <span class="json-boolean">$1</span>')
        .replace(/: (null)/g, ': <span class="json-null">$1</span>');
}

function switchTab(tabName) {
    document.querySelectorAll('.modal-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.modal-body > div').forEach(d => d.style.display = 'none');

    document.querySelector(`.modal-tab[data-tab="${tabName}"]`)?.classList.add('active');

    const tabMap = { full: 'tabFull', ai: 'tabAi', triage: 'tabTriage' };
    const el = document.getElementById(tabMap[tabName]);
    if (el) el.style.display = 'block';
}

function closeModal() {
    document.getElementById('jsonModal').classList.remove('active');
}

function copyToClipboard(btn, text) {
    navigator.clipboard.writeText(text).then(() => {
        const originalText = btn.textContent;
        btn.classList.add('copied');
        btn.textContent = '✓ Copied!';
        setTimeout(() => {
            btn.classList.remove('copied');
            btn.textContent = originalText;
        }, 2000);
    });
}

document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
document.getElementById('jsonModal').addEventListener('click', e => {
    if (e.target.classList.contains('modal-overlay')) closeModal();
});

initCharts();
loadData();

// Cluster Manager Functions
async function openClusterManager() {
    try {
        const modal = document.getElementById('clusterManager');
        if (modal) {
            modal.classList.add('active');
        } else {
            throw new Error('clusterManager element not found');
        }
        await loadClusterList();
    } catch (e) {
        console.error('openClusterManager error:', e);
    }
}

async function loadClusterList() {
    const listEl = document.getElementById('clusterList');
    if (!listEl) {
        console.error('clusterList element not found');
        return;
    }
    try {
        const resp = await fetch('/api/clusters/full');
        if (!resp.ok) {
            throw new Error(`HTTP ${resp.status}: ${resp.statusText}`);
        }
        let clusters = await resp.json();
        if (!Array.isArray(clusters)) {
            throw new Error(`Expected clusters array, got ${typeof clusters}`);
        }
        const html = '<h3>Existing Clusters</h3>' + clusters.map(c => renderClusterItem(c)).join('');
        listEl.innerHTML = html;
        document.querySelectorAll('.editClusterForm').forEach(form => {
            form.onsubmit = updateCluster;
        });
    } catch (e) {
        console.error('loadClusterList error:', e);
        listEl.innerHTML = `<h3>Existing Clusters</h3><p class="test-result error">Failed to load clusters: ${e.message}</p>`;
    }
}

function closeClusterManager() {
    document.getElementById('clusterManager').classList.remove('active');
}

function renderClusterItem(cluster) {
    return `
        <div class="cluster-item">
            <h4>${cluster.name}</h4>
            <form class="editClusterForm" data-name="${cluster.name}">
                <label>Host:</label>
                <input type="text" name="host" value="${cluster.host}" required>
                <label>User:</label>
                <input type="text" name="user" value="${cluster.user}" required>
                <label>Password:</label>
                <input type="password" name="pass" placeholder="Leave blank to keep current">
                <label>Bucket:</label>
                <input type="text" name="bucket" value="${cluster.bucket}" required>
                <div class="form-buttons">
                    <button type="button" onclick="testCluster(this.form)">Test</button>
                    <button type="submit">Save</button>
                    <button type="button" class="delete-btn" onclick="deleteCluster('${cluster.name}')">Delete</button>
                </div>
            </form>
            <div class="test-result" id="testResult-${cluster.name}"></div>
        </div>
    `;
}

document.addEventListener('DOMContentLoaded', () => {
    const manager = document.getElementById('clusterManager');
    manager.addEventListener('click', e => {
        if (e.target.classList.contains('modal-overlay')) closeClusterManager();
    });
});

async function addCluster(e) {
    e.preventDefault();
    const form = e.target;
    const data = getFormData(form);
    const resp = await fetch('/api/clusters', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
    if (resp.ok) {
        await loadClusters();
        await loadClusterList();
        form.reset();
        showTestResult('addTestResult', 'Cluster added successfully', true);
    } else {
        const err = await resp.json();
        showTestResult('addTestResult', err.error, false);
    }
}

async function testCluster(formOrId) {
    let form;
    if (typeof formOrId === 'string') {
        form = document.getElementById(formOrId);
    } else {
        form = formOrId;
    }
    if (!form) {
        console.error('testCluster: form not found', formOrId);
        return;
    }
    const data = getFormData(form);
    const resultId = form.id === 'addClusterForm' ? 'addTestResult' : `testResult-${form.dataset.name || ''}`;
    try {
        const resp = await fetch('/api/clusters/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });
        if (!resp.ok) {
            throw new Error(`HTTP ${resp.status}: ${resp.statusText}`);
        }
        const result = await resp.json();
        showTestResult(resultId, result.message || result.error || 'Unknown response', !!result.success);
    } catch (e) {
        console.error('testCluster error:', e);
        showTestResult(resultId, 'Test failed: ' + e.message, false);
    }
}

async function updateCluster(e) {
    e.preventDefault();
    const form = e.target;
    const name = form.dataset.name;
    const data = getFormData(form);
    const resp = await fetch(`/api/clusters/${name}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
    if (resp.ok) {
        await loadClusters();
        await loadClusterList();
        showTestResult(`testResult-${name}`, 'Changes saved', true);
    } else {
        const err = await resp.json();
        showTestResult(`testResult-${name}`, err.error, false);
    }
}

async function deleteCluster(name) {
    if (!confirm(`Delete cluster ${name}?`)) return;
    const resp = await fetch(`/api/clusters/${name}`, { method: 'DELETE' });
    if (resp.ok) {
        await loadClusters();
        await loadClusterList();
    }
}

function getFormData(form) {
    const data = {};
    new FormData(form).forEach((value, key) => { data[key] = value; });
    return data;
}

function showTestResult(id, message, success) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = message;
    el.className = 'test-result' + (success ? '' : ' error');
}

/* ─────────────────────────────────────────────────────────── */
/* Session 5: Triage observability + controls                  */
/* ─────────────────────────────────────────────────────────── */

let attemptsFilter = 'any';

function showToast(elId, msg, isError) {
    const el = document.getElementById(elId);
    if (!el) return;
    el.textContent = msg;
    el.classList.toggle('error', !!isError);
    el.classList.add('show');
    clearTimeout(el._toastTimer);
    el._toastTimer = setTimeout(() => el.classList.remove('show'), 1800);
}

function fmtMs(ms) {
    if (ms == null || isNaN(ms)) return '—';
    if (ms < 1000) return ms + 'ms';
    const s = ms / 1000;
    if (s < 60) return s.toFixed(1) + 's';
    const m = Math.floor(s / 60), rs = Math.floor(s % 60);
    return `${m}m ${rs}s`;
}

function fmtTokens(n) {
    if (n == null || isNaN(n)) return '0';
    if (n < 1000) return String(n);
    if (n < 1e6)  return (n / 1000).toFixed(1) + 'k';
    return (n / 1e6).toFixed(2) + 'M';
}

// ── Header strip ────────────────────────────────────────────

async function refreshTriageBar() {
    try {
        const [pollerR, lastR] = await Promise.all([
            fetch('/api/poller/state'),
            fetch('/api/triage/last-ticket'),
        ]);
        const poller = pollerR.ok ? await pollerR.json() : null;
        const last   = lastR.ok   ? await lastR.json()   : null;

        const bar = document.getElementById('triageBar');
        const pollerBadge = document.getElementById('pollerBadge');
        const quotaBadge = document.getElementById('quotaBadge');
        const lastBadge  = document.getElementById('lastTicketBadge');
        const killBtn    = document.getElementById('killSwitchBtn');

        if (!poller) {
            pollerBadge.textContent = '🤖 Poller: ?';
            pollerBadge.className = 'triage-badge fail';
            return;
        }

        bar.classList.toggle('killed', !!poller.killSwitch);
        killBtn.textContent = poller.killSwitch ? '▶ Resume all' : '🛑 Kill switch';
        killBtn.onclick = poller.killSwitch ? resumeAll : killSwitch;

        let pollerText, pollerCls;
        if (poller.killSwitch) { pollerText = '🛑 Killed';           pollerCls = 'fail'; }
        else if (!poller.enabled) { pollerText = '🤖 Poller: paused'; pollerCls = 'warn'; }
        else if (poller.lastError) { pollerText = '🤖 Poller: error';  pollerCls = 'warn'; }
        else if (!poller.configured) { pollerText = '🤖 Poller: (env not set)'; pollerCls = 'warn'; }
        else                   { pollerText = '🤖 Poller: running';   pollerCls = 'ok'; }
        pollerBadge.textContent = pollerText;
        pollerBadge.className = 'triage-badge ' + pollerCls;
        pollerBadge.title = poller.lastError ? ('lastError: ' + poller.lastError) :
                            poller.lastRun ? ('last run: ' + poller.lastRun) : '';

        quotaBadge.textContent = `📊 Today: ${poller.quotaUsed}/${poller.quotaLimit}`;
        quotaBadge.className = 'triage-badge' +
            (poller.quotaUsed >= poller.quotaLimit ? ' warn' : '');

        if (last && last.issueNumber) {
            const ago = timeAgo(last.at);
            const tok = fmtTokens((last.tokens || {}).total);
            const ms  = fmtMs(last.grokMs);
            lastBadge.innerHTML = `✅ Last: <a href="${escapeAttr(last.issueUrl || '#')}" target="_blank" rel="noopener">#${last.issueNumber}</a> · ${ago} · ${escapeHtml(last.model || '?')} · ${tok} tok · ${ms}`;
            lastBadge.className = 'triage-badge ok clickable';
            lastBadge.title = last.issueTitle || '';
        } else {
            lastBadge.textContent = '✅ Last: —';
            lastBadge.className = 'triage-badge';
        }
    } catch (e) {
        console.warn('refreshTriageBar failed:', e);
    }
}

async function pollerPause() {
    const r = await fetch('/api/poller/pause', { method: 'POST' });
    showToast('triageToast', r.ok ? 'Poller paused' : 'Pause failed', !r.ok);
    refreshTriageBar();
}
async function pollerResume() {
    const r = await fetch('/api/poller/resume', { method: 'POST' });
    showToast('triageToast', r.ok ? 'Poller resumed' : 'Resume failed', !r.ok);
    refreshTriageBar();
}
async function pollerRunNow() {
    showToast('triageToast', 'Running…', false);
    const r = await fetch('/api/poller/run-now', { method: 'POST' });
    const data = await r.json().catch(() => ({}));
    const summary = data.outcomes ? Object.entries(data.outcomes).map(([k, v]) => `${k}=${v}`).join(' ')
                                  : (data.skipped ? `skipped: ${data.skipped}` : '(no docs)');
    showToast('triageToast', `Run: ${summary}`, !!data.error);
    refreshTriageBar(); loadAttempts(); loadStats();
}
async function killSwitch() {
    if (!confirm('Activate kill switch?\n\nNo Grok calls, no GitHub writes, poller paused.')) return;
    const r = await fetch('/api/triage/kill', { method: 'POST' });
    showToast('triageToast', r.ok ? 'Kill switch active' : 'Failed', !r.ok);
    refreshTriageBar();
}
async function resumeAll() {
    const r = await fetch('/api/triage/resume', { method: 'POST' });
    showToast('triageToast', r.ok ? 'Resumed' : 'Failed', !r.ok);
    refreshTriageBar();
}

// ── Stats panel ─────────────────────────────────────────────

async function loadStats() {
    try {
        const r = await fetch('/api/triage/stats');
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const stats = await r.json();
        renderStatsPanel(stats);
    } catch (e) {
        document.getElementById('statsCards').innerHTML =
            `<div class="empty">Stats unavailable: ${escapeHtml(e.message)}</div>`;
    }
}

function statCardHtml(title, s) {
    s = s || {};
    const total = s.attempts || 0;
    const pct = (n) => total ? Math.round((n || 0) * 100 / total) : 0;
    const tbm = s.tokensByModel || {};
    const models = Object.keys(tbm).slice(0, 3)
        .map(m => `<tr><td>↳ ${escapeHtml(m)}</td><td>${fmtTokens(tbm[m])}</td></tr>`).join('');
    return `
      <div class="stat-card triage">
        <h5>${escapeHtml(title)}</h5>
        <div class="num">${total} attempts</div>
        <table>
          <tr><td>🟢 Created</td><td>${s.created || 0} <span class="muted">(${pct(s.created)}%)</span></td></tr>
          <tr><td>🔁 Commented</td><td>${s.commented || 0} <span class="muted">(${pct(s.commented)}%)</span></td></tr>
          <tr><td>⚪ Skipped</td><td>${s.skipped || 0}</td></tr>
          <tr><td>⏳ Deferred</td><td>${s.deferred || 0}</td></tr>
          <tr><td>❌ Errors</td><td>${s.errors || 0}</td></tr>
          <tr><td>Tokens</td><td>${fmtTokens(s.tokensTotal)}</td></tr>
          ${models}
          <tr><td>Grok time</td><td>${fmtMs(s.grokMsTotal)}</td></tr>
          <tr><td>avg</td><td>${fmtMs(s.avgGrokMs)}</td></tr>
        </table>
      </div>
    `;
}

function renderStatsPanel(stats) {
    const cards = document.getElementById('statsCards');
    cards.innerHTML =
        statCardHtml('Today',    stats.today) +
        statCardHtml('Last 24h', stats.last24h) +
        statCardHtml('Lifetime', stats.lifetime);

    const today = stats.today || {};
    document.getElementById('statsSummaryLine').textContent =
        ` · today: ${today.created || 0} created · ${today.commented || 0} commented · ` +
        `quota ${stats.quota?.used || 0}/${stats.quota?.limit || 0}`;

    const hero = document.getElementById('lastTicketHero');
    const lt = stats.lastTicket;
    if (lt && lt.issueNumber) {
        const tok = fmtTokens((lt.tokens || {}).total);
        const ms  = fmtMs(lt.grokMs);
        hero.classList.remove('empty-state');
        hero.innerHTML = `
          ✅ <a href="${escapeAttr(lt.issueUrl || '#')}" target="_blank" rel="noopener">#${lt.issueNumber}</a>
          · ${escapeHtml(lt.model || '?')} · ${ms} · ${tok} tokens
          · severity: <span class="sev sev-${escapeAttr(lt.severity || 'medium')}">${escapeHtml(lt.severity || 'medium')}</span>
          <br><strong>${escapeHtml(lt.issueTitle || '')}</strong>
          <br><span class="muted">${timeAgo(lt.at)} · docId=${escapeHtml(lt.docId || '')}</span>
        `;
    } else {
        hero.classList.add('empty-state');
        hero.innerHTML = '✅ Last ticket created: — (none yet)';
    }
}

// ── Recent triage attempts feed ─────────────────────────────

function setAttemptsFilter(outcome) {
    attemptsFilter = outcome;
    document.querySelectorAll('#attemptsFilterBar .filter-pill').forEach(b => {
        b.classList.toggle('active', b.dataset.outcome === outcome);
    });
    loadAttempts();
}

async function loadAttempts() {
    try {
        const r = await fetch(`/api/triage/attempts?limit=50&outcome=${encodeURIComponent(attemptsFilter)}`);
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const items = await r.json();
        renderAttempts(items);
    } catch (e) {
        document.getElementById('attemptsList').innerHTML =
            `<div class="empty">Attempts unavailable: ${escapeHtml(e.message)}</div>`;
    }
}

function outcomeIcon(o) {
    if (!o) return '·';
    if (o === 'created')   return '🟢';
    if (o === 'commented') return '🔁';
    if (o === 'deferred')  return '⏳';
    if (o === 'error')     return '❌';
    if (o === 'skipped-kill-switch') return '🛑';
    if (o.startsWith('skipped'))     return '⚪';
    return '·';
}

function outcomeBucket(o) {
    if (!o) return 'skipped';
    if (o === 'created' || o === 'commented' || o === 'deferred' || o === 'error') return o;
    return 'skipped';
}

function renderAttempts(items) {
    const list = document.getElementById('attemptsList');
    if (!items || items.length === 0) {
        list.innerHTML = `<div class="empty">No attempts yet.</div>`;
        document.getElementById('attemptsSummaryLine').textContent = ' · 0';
        return;
    }
    document.getElementById('attemptsSummaryLine').textContent = ` · ${items.length}`;
    list.innerHTML = items.map(a => {
        const o = a.outcome || '';
        const cls = 'outcome-' + outcomeBucket(o);
        const num = a.issueNumber ? `<a href="${escapeAttr(a.issueUrl || '#')}" target="_blank" rel="noopener">#${a.issueNumber}</a>` : '—';
        const sev = a.severity ? `<span class="sev sev-${escapeAttr(a.severity)}">${escapeHtml(a.severity)}</span>` : '';
        const tok = fmtTokens((a.tokens || {}).total);
        const ms  = fmtMs(a.grokMs);
        const ago = timeAgo(a.at);
        const doc = (a.docId || '').slice(0, 24);
        const tip = JSON.stringify(a, null, 2);
        return `
          <div class="attempt-row ${cls}" title="${escapeAttr(tip)}">
            <span class="ar-out">${outcomeIcon(o)} ${escapeHtml(o)}</span>
            <span class="ar-num">${num}${sev}</span>
            <span class="ar-reason">${escapeHtml(a.reason || '')}</span>
            <span class="ar-doc">docId=${escapeHtml(doc)}</span>
            <span class="ar-meta">${escapeHtml(a.trigger || '')} · ${ms} · ${tok} tok · ${escapeHtml(a.model || '—')} · ${ago}</span>
          </div>
        `;
    }).join('');
}

// ── Deferred queue ──────────────────────────────────────────

async function loadDeferred() {
    try {
        // No dedicated endpoint — derive from /api/triage/stats + attempts.
        // Use /api/issues/status to look up which docIds have deferred=true,
        // but that requires knowing the docIds up front. So we read attempts
        // (filter outcome=deferred) which captures the same queue.
        const r = await fetch('/api/triage/attempts?limit=100&outcome=deferred');
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const items = await r.json();
        renderDeferred(items);
    } catch (e) {
        document.getElementById('deferredList').innerHTML =
            `<div class="empty">Deferred queue unavailable: ${escapeHtml(e.message)}</div>`;
    }
}

function renderDeferred(items) {
    const list = document.getElementById('deferredList');
    if (!items || items.length === 0) {
        list.innerHTML = `<div class="empty">No deferred tickets — quota has not been exhausted.</div>`;
        document.getElementById('deferredSummaryLine').textContent = ' · 0';
        return;
    }
    document.getElementById('deferredSummaryLine').textContent = ` · ${items.length}`;
    list.innerHTML = items.map(a => {
        const doc = (a.docId || '').slice(0, 32);
        const ago = timeAgo(a.at);
        return `
          <div class="attempt-row outcome-deferred">
            <span class="ar-out">⏳ deferred</span>
            <span class="ar-doc">docId=${escapeHtml(doc)}</span>
            <span class="ar-reason">${escapeHtml(a.reason || '')}</span>
            <span class="ar-meta">${ago}</span>
            <button class="ticket-btn" onclick="triageRow('${escapeAttr(a.docId || '')}')">Force triage</button>
          </div>
        `;
    }).join('');
}

// ── Controls modal ──────────────────────────────────────────

async function openControlsPanel() {
    try {
        const r = await fetch('/api/triage/controls');
        const c = r.ok ? await r.json() : {};
        document.getElementById('ctrl_killSwitch').checked = !!c.killSwitch;
        document.getElementById('ctrl_pollerEnabled').checked = !!c.pollerEnabled;
        document.getElementById('ctrl_issueCreationEnabled').checked = !!c.issueCreationEnabled;
        document.getElementById('ctrl_skipProcessedDocs').checked = !!c.skipProcessedDocs;
        document.getElementById('ctrl_skipKnownSignatures').checked = !!c.skipKnownSignatures;
        document.getElementById('ctrl_modelOverride').value = c.modelOverride || '';
        document.getElementById('controlsMeta').textContent =
            c.updatedAt ? `Last updated: ${c.updatedAt} by ${c.updatedBy || '?'}` : '';
    } catch (e) {
        console.warn('openControlsPanel failed:', e);
    }
    document.getElementById('controlsModal').classList.add('active');
}

function closeControlsPanel() {
    document.getElementById('controlsModal').classList.remove('active');
}

let _saveTimers = {};
function saveControl(key, value) {
    clearTimeout(_saveTimers[key]);
    _saveTimers[key] = setTimeout(async () => {
        try {
            const body = {}; body[key] = value;
            const r = await fetch('/api/triage/controls', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!r.ok) throw new Error('HTTP ' + r.status);
            const c = await r.json();
            document.getElementById('controlsMeta').textContent =
                `Last updated: ${c.updatedAt} by ${c.updatedBy}`;
            showToast('controlsToast', '✓ saved');
            refreshTriageBar();
        } catch (e) {
            showToast('controlsToast', 'Save failed: ' + e.message, true);
        }
    }, 300);
}

async function testGithubConnection(mode) {
    const out = document.getElementById('testTicketResult');
    if (!out) return;
    const label = mode === 'create' ? '🧪 Creating test issue…' : '🔍 Verifying token…';
    out.textContent = label;
    out.style.color = '';
    if (mode === 'create' && !confirm(
        'This will create a real GitHub issue in the configured repo.\n\n' +
        'The issue will be labeled "grokcoder-test" and the body explains ' +
        'how to delete it. It does NOT consume your daily quota.\n\nProceed?'
    )) {
        out.textContent = '';
        return;
    }
    try {
        const r = await fetch('/api/triage/test-ticket', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok || data.ok === false) {
            out.style.color = '#ff6b6b';
            out.textContent = `❌ ${data.error || ('HTTP ' + r.status)}`;
            return;
        }
        out.style.color = '#2ea043';
        if (mode === 'create' && data.issueUrl) {
            out.innerHTML = `✅ Issue #${data.issueNumber} created` +
                ` (${data.githubMs}ms). ` +
                `<a href="${data.issueUrl}" target="_blank" rel="noopener">Open ↗</a><br>` +
                `<small>repo=${data.repo} · canIssuesWrite=${data.canIssuesWrite}</small>`;
            // refresh attempts feed so the new line shows up
            if (typeof loadAttempts === 'function') loadAttempts();
            if (typeof refreshTriageBar === 'function') refreshTriageBar();
        } else {
            const perms = data.permissions || {};
            out.innerHTML = `✅ Token valid for ${data.repo}` +
                ` (${data.githubMs}ms)<br>` +
                `<small>canIssuesWrite=${data.canIssuesWrite}` +
                ` · push=${!!perms.push}` +
                ` · admin=${!!perms.admin}</small>`;
        }
    } catch (e) {
        out.style.color = '#ff6b6b';
        out.textContent = `❌ ${e.message}`;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const cm = document.getElementById('controlsModal');
    if (cm) cm.addEventListener('click', e => {
        if (e.target.classList.contains('modal-overlay')) closeControlsPanel();
    });
});

// ── Manual "Triage now" from a table row ────────────────────

async function triageRow(docId) {
    if (!docId) return;
    showToast('triageToast', `Triaging ${docId.slice(0, 12)}…`);
    try {
        // 1) Grok call via manual triage route.
        const r1 = await fetch(`/api/triage/${encodeURIComponent(docId)}`, { method: 'POST' });
        const d1 = await r1.json().catch(() => ({}));
        if (!r1.ok && r1.status !== 200) {
            showToast('triageToast', `Triage failed: ${d1.error || r1.status}`, true);
            return;
        }
        // If gate short-circuited (skipped-doc-processed etc.), just refresh.
        if (!d1.triage) {
            showToast('triageToast', `${d1.status || 'done'} (${d1.reason || ''})`);
            await loadIssueStatuses(); filterTable(); refreshTriageBar(); loadAttempts();
            return;
        }
        // 2) File the issue.
        const r2 = await fetch('/api/issues', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                docId: d1.docId,
                signature: d1.signature,
                triage: d1.triage,
                bug: d1.bug,
                trigger: 'manual',
                model: d1.model,
                grokMs: d1.grokMs,
                tokens: d1.tokens,
                payloadBytes: d1.payloadBytes,
            }),
        });
        const d2 = await r2.json().catch(() => ({}));
        showToast('triageToast', `${d2.status || 'done'}${d2.number ? ' #' + d2.number : ''}`,
                  !r2.ok && r2.status !== 200);
        await loadIssueStatuses();
        filterTable();
        refreshTriageBar(); loadAttempts(); loadStats();
    } catch (e) {
        showToast('triageToast', 'Triage error: ' + e.message, true);
    }
}

// ── Triage tab inside report modal ──────────────────────────

function renderTriageTab(docId, error, report, session) {
    const container = document.getElementById('tabTriage');
    const status = (window.issueStatusMap || {})[docId];

    let header;
    if (status && status.hasIssue) {
        const sev = status.severity ? `<span class="sev sev-${escapeAttr(status.severity)}">${escapeHtml(status.severity)}</span>` : '';
        const ago = timeAgo(status.lastSeenAt || status.firstSeenAt);
        const seen = (status.count && status.count > 1) ? ` · seen ×${status.count}` : '';
        header = `
          <div class="triage-sticky-header">
            ✅ Issue already filed: <a href="${escapeAttr(status.issueUrl)}" target="_blank" rel="noopener">#${status.issueNumber}</a>
            ${sev} · <span class="muted">${status.action || 'created'} ${ago}${seen}</span>
            <br><strong>${escapeHtml(status.issueTitle || '')}</strong>
            <div class="triage-tab-actions">
              <a class="triage-btn" href="${escapeAttr(status.issueUrl)}" target="_blank" rel="noopener">Open on GitHub ↗</a>
              <button class="triage-btn" onclick="triageRow('${escapeAttr(docId)}')">Re-run triage</button>
            </div>
          </div>`;
    } else if (status && status.deferred) {
        header = `
          <div class="triage-sticky-header deferred">
            ⏳ Queued for triage — ${escapeHtml(status.reason || 'deferred')}.
            The poller will create this ticket tomorrow.
            <div class="triage-tab-actions">
              <button class="triage-btn" onclick="triageRow('${escapeAttr(docId)}')">Force triage</button>
            </div>
          </div>`;
    } else {
        header = `
          <div class="triage-sticky-header none">
            ⚪ No GitHub issue yet for this session.
            <div class="triage-tab-actions">
              <button class="triage-btn" onclick="triageRow('${escapeAttr(docId)}')">🐛 Triage → GitHub</button>
            </div>
          </div>`;
    }

    container.innerHTML = header +
        `<div class="triage-result-block">
           <strong>Doc:</strong> ${escapeHtml(docId)}<br>
           <span class="muted">Click "Triage" above to send this session to Grok and (depending on quota / dedup) open or comment on a GitHub issue. Watch the "Recent triage attempts" panel below for the result.</span>
         </div>`;
}

// ── Boot ────────────────────────────────────────────────────

// Periodic refresh of the triage bar (15 s).
setInterval(refreshTriageBar, 15000);
refreshTriageBar();
