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
    filterTable();
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
    tbody.innerHTML = '<tr><td colspan="5" class="empty">🔍 Searching...</td></tr>';

    try {
        const resp = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
        const data = await resp.json();

        if (data.error) {
            tbody.innerHTML = `<tr><td colspan="5" class="empty">Search error: ${escapeHtml(data.error)}</td></tr>`;
            return;
        }

        if (!data.hits || data.hits.length === 0) {
            tbody.innerHTML = `<tr><td colspan="5" class="empty">No results for "${escapeHtml(query)}"</td></tr>`;
            return;
        }

        const rows = data.hits.map(hit => renderSearchHitRow(hit)).join('');
        tbody.innerHTML = `<tr><td colspan="5" style="padding:8px;color:#888;">🔍 ${data.total} results (${(data.took / 1000000).toFixed(1)}ms)</td></tr>` + rows;
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="5" class="empty">Search failed: ${e.message}</td></tr>`;
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
            <td>
                <button class="view-btn" onclick="viewReport('${escapeAttr(docId)}')">View</button>
            </td>
        </tr>
    `;
}

function renderTable(errors) {
    const tbody = document.getElementById('tableBody');
    if (errors.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="empty">No errors found 🎉</td></tr>';
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
            <td>
                <button class="view-btn" onclick="viewReport('${escapeAttr(docId)}')">View</button>
            </td>
        </tr>
    `;
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

    const tabMap = { full: 'tabFull', ai: 'tabAi' };
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
