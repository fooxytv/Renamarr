const API = '';
let currentFilter = 'all';
let currentTab = 'files';
let pollInterval = null;

// API helpers
async function api(method, path, body) {
    const opts = { method, headers: { 'Content-Type': 'application/json' } };
    if (body) opts.body = JSON.stringify(body);
    const r = await fetch(API + path, opts);
    return r.json();
}

// Tab switching
function switchTab(tab) {
    currentTab = tab;
    document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.id === 'tab-' + tab));
}

// Format file size
function formatSize(bytes) {
    if (!bytes) return '-';
    const units = ['B', 'KB', 'MB', 'GB'];
    let i = 0;
    let size = bytes;
    while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
    return size.toFixed(1) + ' ' + units[i];
}

// Refresh status bar
async function refreshStatus() {
    const s = await api('GET', '/api/status');
    const correct = s.total_files - s.pending - s.approved - s.rejected - s.completed - s.failed;

    document.getElementById('stat-total').textContent = s.total_files;
    document.getElementById('stat-pending').textContent = s.pending;
    document.getElementById('stat-approved').textContent = s.approved;
    document.getElementById('stat-rejected').textContent = s.rejected;
    document.getElementById('stat-completed').textContent = s.completed;
    document.getElementById('stat-correct').textContent = correct > 0 ? correct : 0;

    document.getElementById('btn-scan').disabled = s.scanning;
    document.getElementById('btn-execute').disabled = s.scanning || s.approved === 0;

    if (s.scanning) {
        startPolling();
    } else {
        stopPolling();
    }
    return s;
}

// Start/stop polling during scan
function startPolling() {
    if (pollInterval) return;
    pollInterval = setInterval(async () => {
        const s = await refreshStatus();
        if (!s.scanning) {
            stopPolling();
            await loadScan();
        }
    }, 3000);
}

function stopPolling() {
    if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
}

// Load scan results
async function loadScan() {
    try {
        const scan = await api('GET', '/api/scan/current');
        if (scan.detail) {
            showEmpty('files', 'No scan results yet. Click "Scan Now" to start.');
            showEmpty('duplicates', 'No scan results yet.');
            return;
        }

        if (scan.status === 'running') {
            document.getElementById('tab-files').innerHTML = '<div class="scanning"><div class="spinner"></div><p>Scanning your media library...</p></div>';
            document.getElementById('tab-duplicates').innerHTML = '<div class="scanning"><div class="spinner"></div><p>Scanning...</p></div>';
            return;
        }

        renderFiles(scan.files);
        renderDuplicates(scan.duplicates);
        updateTabBadges(scan);
    } catch (e) {
        showEmpty('files', 'No scan results yet. Click "Scan Now" to start.');
        showEmpty('duplicates', 'No scan results yet.');
    }
    await refreshStatus();
}

function showEmpty(tab, message) {
    document.getElementById('tab-' + tab).innerHTML = '<div class="empty-state"><p>' + message + '</p></div>';
}

function updateTabBadges(scan) {
    const pending = scan.files.filter(f => f.status === 'pending').length;
    const dups = scan.duplicates.length;
    document.getElementById('badge-files').textContent = scan.files.length;
    document.getElementById('badge-duplicates').textContent = dups;
}

// Render file table
function renderFiles(files) {
    const filtered = currentFilter === 'all' ? files : files.filter(f => f.status === currentFilter);

    let html = '<div class="toolbar">';
    html += '<div class="filter-group">';
    ['all', 'pending', 'approved', 'rejected', 'correct', 'completed'].forEach(f => {
        const count = f === 'all' ? files.length : files.filter(x => x.status === f).length;
        html += '<button class="filter-btn' + (currentFilter === f ? ' active' : '') + '" onclick="setFilter(\'' + f + '\')">' + f + ' (' + count + ')</button>';
    });
    html += '</div>';
    html += '<div class="toolbar-spacer"></div>';
    html += '<button class="btn btn-success btn-sm" onclick="approveAll()">Approve All</button>';
    html += '<button class="btn btn-danger btn-sm" onclick="rejectAll()">Reject All</button>';
    html += '</div>';

    html += '<table class="file-table"><thead><tr>';
    html += '<th>Status</th><th>Type</th><th>Current Name</th><th></th><th>New Name</th><th>Quality</th><th>Size</th><th>Actions</th>';
    html += '</tr></thead><tbody>';

    for (const f of filtered) {
        html += '<tr>';
        html += '<td><span class="badge badge-' + f.status + '">' + f.status + '</span></td>';
        html += '<td>' + (f.media_type === 'movie' ? 'Movie' : 'TV') + '</td>';
        html += '<td class="filename" title="' + esc(f.source_path) + '">' + esc(f.source_filename) + '</td>';
        html += '<td class="arrow">&rarr;</td>';
        html += '<td class="filename" title="' + esc(f.destination_path) + '">' + esc(f.destination_filename) + '</td>';
        html += '<td>' + (f.resolution || '-') + '</td>';
        html += '<td>' + formatSize(f.file_size) + '</td>';
        html += '<td>';
        if (f.status === 'pending') {
            html += '<button class="btn btn-success btn-sm" onclick="approveFile(\'' + f.id + '\')">Approve</button> ';
            html += '<button class="btn btn-danger btn-sm" onclick="rejectFile(\'' + f.id + '\')">Reject</button>';
        } else if (f.status === 'approved' || f.status === 'rejected') {
            html += '<button class="btn btn-outline btn-sm" onclick="resetFile(\'' + f.id + '\')">Undo</button>';
        }
        html += '</td>';
        html += '</tr>';
    }

    html += '</tbody></table>';

    if (filtered.length === 0) {
        html += '<div class="empty-state"><p>No files match this filter.</p></div>';
    }

    document.getElementById('tab-files').innerHTML = html;
}

// Render duplicates
function renderDuplicates(duplicates) {
    if (!duplicates || duplicates.length === 0) {
        showEmpty('duplicates', 'No duplicates found.');
        return;
    }

    let html = '';
    for (const group of duplicates) {
        html += '<div class="dup-card">';
        html += '<h3>' + esc(group.identifier) + '</h3>';
        html += '<div class="dup-files">';
        for (const f of group.files) {
            const isBest = f.id === group.best_file_id;
            html += '<div class="dup-file' + (isBest ? ' best' : '') + '">';
            html += '<span class="dup-quality">' + (f.resolution || '?') + '</span>';
            html += '<span class="dup-score">Score: ' + f.quality_score + '</span>';
            html += '<span class="dup-filename" title="' + esc(f.source_path) + '">' + esc(f.source_filename) + '</span>';
            html += '<span>' + formatSize(f.file_size) + '</span>';
            if (isBest) {
                html += '<span class="dup-best-tag">BEST</span>';
            }
            html += '</div>';
        }
        html += '</div>';
        html += '<div style="margin-top:10px">';
        html += '<button class="btn btn-success btn-sm" onclick="keepBest(\'' + group.best_file_id + '\',' + JSON.stringify(group.files.filter(f => f.id !== group.best_file_id).map(f => f.id)) + ')">Keep Best, Reject Others</button>';
        html += '</div>';
        html += '</div>';
    }

    document.getElementById('tab-duplicates').innerHTML = html;
}

// Load history
async function loadHistory() {
    const history = await api('GET', '/api/history');
    if (!history || history.length === 0) {
        showEmpty('history', 'No scan history.');
        return;
    }

    let html = '';
    for (const h of history) {
        html += '<div class="history-item">';
        html += '<span class="history-date">' + new Date(h.started_at).toLocaleString() + '</span>';
        html += '<span class="badge badge-' + (h.status === 'completed' ? 'completed' : 'failed') + '">' + h.status + '</span>';
        html += '<span class="history-count">' + h.total_files + ' files</span>';
        html += '<span class="history-count">' + h.duplicates + ' duplicates</span>';
        html += '</div>';
    }

    document.getElementById('tab-history').innerHTML = html;
}

// Actions
async function triggerScan() {
    await api('POST', '/api/scan');
    document.getElementById('btn-scan').disabled = true;
    document.getElementById('tab-files').innerHTML = '<div class="scanning"><div class="spinner"></div><p>Scanning your media library...</p></div>';
    startPolling();
}

async function approveFile(id) {
    await api('POST', '/api/files/' + id + '/approve');
    await loadScan();
}

async function rejectFile(id) {
    await api('POST', '/api/files/' + id + '/reject');
    await loadScan();
}

async function resetFile(id) {
    await api('POST', '/api/files/' + id + '/pending');
    await loadScan();
}

async function approveAll() {
    await api('POST', '/api/files/approve-all');
    await loadScan();
}

async function rejectAll() {
    await api('POST', '/api/files/reject-all');
    await loadScan();
}

async function keepBest(bestId, rejectIds) {
    await api('POST', '/api/files/' + bestId + '/approve');
    for (const id of rejectIds) {
        await api('POST', '/api/files/' + id + '/reject');
    }
    await loadScan();
}

async function executeApproved() {
    if (!confirm('This will rename all approved files. Continue?')) return;
    document.getElementById('btn-execute').disabled = true;
    const result = await api('POST', '/api/execute');
    alert('Done! ' + result.completed + ' renamed, ' + result.failed + ' failed.');
    await loadScan();
}

function setFilter(f) {
    currentFilter = f;
    loadScan();
}

function esc(s) {
    if (!s) return '';
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// Init
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.tab').forEach(t => {
        t.addEventListener('click', () => {
            switchTab(t.dataset.tab);
            if (t.dataset.tab === 'history') loadHistory();
        });
    });

    loadScan();
});
