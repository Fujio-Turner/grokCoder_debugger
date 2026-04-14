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

function filterTable() {
    const search = document.getElementById('searchBox').value.toLowerCase();

    let filtered = allErrors;

    if (search) {
        filtered = filtered.filter(e =>
            (e.description || '').toLowerCase().includes(search) ||
            (e.sessionId || '').toLowerCase().includes(search) ||
            (e.toolName || '').toLowerCase().includes(search) ||
            (e.details || '').toLowerCase().includes(search)
        );
    }

    renderTable(filtered);
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
            const resp = await fetch(`/api/session/${docId}`);
            session = await resp.json();
            sessionCache[docId] = session;
        } catch (err) {
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

function renderAiInputs(error, report, session) {
    const container = document.getElementById('tabAi');

    const debugPackage = {
        _instructions: "This is a debug package for analyzing a tool error in grokCoder. Use this context to help debug the issue.",
        error_summary: error ? {
            type: error.type,
            toolName: error.toolName,
            description: error.description,
            execTime: error.execTime,
            sessionId: error.sessionId
        } : null,
        user_prompt: error?.userPrompt || report?.user_prompt || 'N/A',
        report_context: report ? {
            docId: report.docId,
            src: report.src,
            toolCount: report.toolCount,
            chatHistoryLen: report.chatHistoryLen,
            meta: report['~meta'] || null
        } : null,
        full_session: session || null
    };

    const packageJson = JSON.stringify(debugPackage, null, 2);

    let html = `
        <h3>🤖 AI Debug Inputs</h3>
        
        <div class="ai-inputs-intro">
            <p><strong>Copy this debug package and paste into your AI assistant.</strong></p>
            <p>It contains all the context needed to analyze this tool error.</p>
            <button class="copy-btn large" onclick="copyToClipboard(this, ${JSON.stringify(packageJson)})">
                📋 Copy Full Debug Package
            </button>
        </div>
    `;

    if (debugPackage.error_summary) {
        html += `
            <div class="debug-section priority-high">
                <div class="debug-section-header">
                    <span>🚨 Error Summary</span>
                </div>
                <div class="debug-section-body">
                    <div class="debug-label">Tool</div>
                    <div class="debug-value">${escapeHtml(debugPackage.error_summary.toolName || '')}</div>
                    <div class="debug-label">Description</div>
                    <div class="debug-value">${escapeHtml(debugPackage.error_summary.description || '')}</div>
                    <div class="debug-label">Exec Time</div>
                    <div class="debug-value">${debugPackage.error_summary.execTime != null ? debugPackage.error_summary.execTime + 's' : '-'}</div>
                </div>
            </div>
        `;
    }

    html += `
        <div class="debug-section priority-high">
            <div class="debug-section-header">
                <span>📝 User Prompt</span>
                <button class="copy-btn" onclick="copyToClipboard(this, ${JSON.stringify(debugPackage.user_prompt)})">📋</button>
            </div>
            <div class="debug-section-body">
                <pre>${escapeHtml(debugPackage.user_prompt)}</pre>
            </div>
        </div>
    `;

    if (debugPackage.report_context) {
        html += `
            <div class="debug-section priority-medium">
                <div class="debug-section-header">
                    <span>📦 Report Context</span>
                </div>
                <div class="debug-section-body">
                    <pre>${escapeHtml(JSON.stringify(debugPackage.report_context, null, 2))}</pre>
                </div>
            </div>
        `;
    }

    html += `
        <div class="debug-section priority-low">
            <div class="debug-section-header">
                <span>📊 Full Debug Package (JSON)</span>
                <button class="copy-btn" onclick="copyToClipboard(this, ${JSON.stringify(packageJson)})">📋</button>
            </div>
            <div class="debug-section-body">
                <pre>${escapeHtml(packageJson)}</pre>
            </div>
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
