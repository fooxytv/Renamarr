const API = '';
let currentFilter = 'all';
let currentTypeFilter = 'all';
let currentTab = 'files';
let currentView = localStorage.getItem('renamarr_view') || 'cards';
let searchQuery = '';
let currentSort = localStorage.getItem('renamarr_sort') || 'default';
let pollInterval = null;
let apiKey = localStorage.getItem('renamarr_api_key') || '';
let cachedFiles = [];

// API helpers
function getHeaders(extra) {
    const headers = { 'Content-Type': 'application/json' };
    if (apiKey) headers['X-Api-Key'] = apiKey;
    if (extra) Object.assign(headers, extra);
    return headers;
}

async function api(method, path, body) {
    const opts = { method, headers: getHeaders() };
    if (body) opts.body = JSON.stringify(body);
    const r = await fetch(API + path, opts);
    if (r.status === 401) {
        const key = prompt('API key required. Enter your RENAMARR_API_KEY:');
        if (key) {
            apiKey = key.trim();
            localStorage.setItem('renamarr_api_key', apiKey);
            return api(method, path, body);
        }
    }
    return r.json();
}

// Tab switching
function switchTab(tab) {
    currentTab = tab;
    document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.id === 'tab-' + tab));
}

// View switching
function setView(view) {
    currentView = view;
    localStorage.setItem('renamarr_view', view);
    renderFiles(cachedFiles);
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

    if (s.version) document.getElementById('version-stamp').textContent = 'Renamarr v' + s.version;
    document.getElementById('btn-scan').disabled = s.scanning;
    document.getElementById('btn-cancel').style.display = s.scanning ? '' : 'none';
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

        const status = await refreshStatus();
        if (scan.status === 'running' && status.scanning) {
            document.getElementById('tab-files').innerHTML = '<div class="scanning"><div class="spinner"></div><p>Scanning your media library...</p></div>';
            document.getElementById('tab-duplicates').innerHTML = '<div class="scanning"><div class="spinner"></div><p>Scanning...</p></div>';
            return;
        }

        cachedFiles = scan.files;
        window._cachedDuplicates = scan.duplicates;
        renderFiles(scan.files);
        renderDuplicates(scan.duplicates);
        updateTabBadges(scan);
    } catch (e) {
        showEmpty('files', 'No scan results yet. Click "Scan Now" to start.');
        showEmpty('duplicates', 'No scan results yet.');
    }
}

function showEmpty(tab, message) {
    document.getElementById('tab-' + tab).innerHTML = '<div class="empty-state"><p>' + message + '</p></div>';
}

function updateTabBadges(scan) {
    const dups = scan.duplicates.length;
    document.getElementById('badge-files').textContent = scan.files.length;
    document.getElementById('badge-duplicates').textContent = dups;
}

// Build toolbar HTML (shared between views)
function buildToolbar(files, typeFiltered) {
    const movieCount = files.filter(f => f.media_type === 'movie').length;
    const tvCount = files.filter(f => f.media_type === 'episode').length;

    let html = '<div class="toolbar">';
    html += '<input type="text" class="search-box" placeholder="Search files..." value="' + esc(searchQuery) + '" oninput="setSearch(this.value)">';
    html += '<div class="filter-group">';
    html += '<span class="filter-label">Type:</span>';
    [['all', 'All (' + files.length + ')'], ['movie', 'Movies (' + movieCount + ')'], ['episode', 'TV (' + tvCount + ')']].forEach(([key, label]) => {
        html += '<button class="filter-btn' + (currentTypeFilter === key ? ' active' : '') + '" onclick="setTypeFilter(\'' + key + '\')">' + label + '</button>';
    });
    html += '</div>';
    html += '<div class="toolbar-spacer"></div>';
    html += '<div class="view-toggle">';
    html += '<button class="view-toggle-btn' + (currentSort === 'default' ? ' active' : '') + '" onclick="setSort(\'default\')">Default</button>';
    html += '<button class="view-toggle-btn' + (currentSort === 'alpha' ? ' active' : '') + '" onclick="setSort(\'alpha\')">A-Z</button>';
    html += '</div>';
    html += '<div class="view-toggle">';
    html += '<button class="view-toggle-btn' + (currentView === 'cards' ? ' active' : '') + '" onclick="setView(\'cards\')" title="Card view">Cards</button>';
    html += '<button class="view-toggle-btn' + (currentView === 'table' ? ' active' : '') + '" onclick="setView(\'table\')" title="Table view">Table</button>';
    html += '</div>';
    html += '</div>';

    html += '<div class="toolbar">';
    html += '<div class="filter-group">';
    html += '<span class="filter-label">Status:</span>';
    ['all', 'pending', 'approved', 'rejected', 'correct', 'completed'].forEach(f => {
        const count = f === 'all' ? typeFiltered.length : typeFiltered.filter(x => x.status === f).length;
        html += '<button class="filter-btn' + (currentFilter === f ? ' active' : '') + '" onclick="setFilter(\'' + f + '\')">' + f + ' (' + count + ')</button>';
    });
    html += '</div>';
    html += '<div class="toolbar-spacer"></div>';
    html += '<button class="btn btn-success btn-sm" onclick="approveAll()">Approve All</button>';
    html += '<button class="btn btn-danger btn-sm" onclick="rejectAll()">Reject All</button>';
    html += '</div>';

    return html;
}

// Render file cards (poster view)
function renderCards(filtered) {
    let html = '<div class="card-grid">';

    for (const f of filtered) {
        html += '<div class="media-card">';
        html += '<div class="poster-wrapper">';

        if (f.poster_url) {
            html += '<img src="' + esc(f.poster_url) + '" alt="' + esc(f.title) + '" loading="lazy">';
        } else {
            html += '<div class="poster-placeholder">';
            html += '<div class="placeholder-icon">' + (f.media_type === 'movie' ? '&#127909;' : '&#128250;') + '</div>';
            html += '<div>' + esc(f.title || f.source_filename) + '</div>';
            html += '</div>';
        }

        // Status badge
        html += '<div class="card-badge"><span class="badge badge-' + f.status + '">' + f.status + '</span></div>';
        // Type badge
        html += '<span class="card-type-badge card-type-' + f.media_type + '">' + (f.media_type === 'movie' ? 'Movie' : 'TV') + '</span>';

        // Quality overlay
        if (f.resolution) {
            html += '<div class="card-overlay"><span class="card-quality">' + f.resolution + '</span></div>';
        }

        html += '</div>'; // poster-wrapper

        html += '<div class="card-info">';
        html += '<div class="card-title" title="' + esc(f.title) + '">' + esc(f.title || 'Unknown') + '</div>';

        let subtitle = '';
        if (f.media_type === 'movie' && f.year) {
            subtitle = '(' + f.year + ')';
        } else if (f.media_type === 'episode' && f.season != null) {
            subtitle = 'S' + String(f.season).padStart(2, '0') + 'E' + String(f.episode).padStart(2, '0');
        }
        if (subtitle) {
            html += '<div class="card-subtitle">' + subtitle + '</div>';
        }

        html += '<div class="card-meta">';
        html += '<span>' + formatSize(f.file_size) + '</span>';
        html += '<span class="card-meta-actions">';
        html += '<button class="btn-icon" onclick="retryLookup(\'' + f.id + '\')" title="Retry API lookup">&#x21bb;</button>';
        html += '<button class="btn-icon" onclick="editMetadata(\'' + f.id + '\', \'' + esc(f.title || '') + '\', ' + (f.year || 'null') + ')" title="Edit metadata">&#x270E;</button>';
        html += '</span>';
        html += '</div>';

        html += '<div class="card-rename">';
        html += '<div class="rename-from" title="' + esc(f.source_path) + '">' + esc(f.source_filename) + '</div>';
        html += '<div class="rename-arrow">&darr;</div>';
        html += '<div class="rename-to" title="' + esc(f.destination_path) + '">' + esc(f.destination_filename) + '</div>';
        html += '</div>';
        html += '</div>'; // card-info

        html += '<div class="card-actions">';
        if (f.status === 'pending') {
            html += '<button class="btn btn-success" onclick="approveFile(\'' + f.id + '\')">Approve</button>';
            html += '<button class="btn btn-danger" onclick="rejectFile(\'' + f.id + '\')">Reject</button>';
        } else if (f.status === 'approved' || f.status === 'rejected') {
            html += '<button class="btn btn-outline" onclick="resetFile(\'' + f.id + '\')">Undo</button>';
        } else {
            html += '<button class="btn btn-outline" disabled>' + f.status + '</button>';
        }
        html += '</div>';

        html += '</div>'; // media-card
    }

    html += '</div>';
    return html;
}

// Render file table
function renderTable(filtered) {
    let html = '<table class="file-table"><thead><tr>';
    html += '<th>Status</th><th>Type</th><th>Current Name</th><th></th><th>New Name</th><th>Quality</th><th>Size</th><th>Metadata</th><th>Actions</th>';
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
        html += '<td class="meta-actions">';
        html += '<button class="btn-icon" onclick="retryLookup(\'' + f.id + '\')" title="Retry API lookup">&#x21bb;</button>';
        html += '<button class="btn-icon" onclick="editMetadata(\'' + f.id + '\', \'' + esc(f.title || '') + '\', ' + (f.year || 'null') + ')" title="Edit metadata">&#x270E;</button>';
        html += '</td>';
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
    return html;
}

// Render files (dispatches to card or table view)
function renderFiles(files) {
    cachedFiles = files;
    const typeFiltered = currentTypeFilter === 'all' ? files : files.filter(f => f.media_type === currentTypeFilter);
    let filtered = currentFilter === 'all' ? typeFiltered : typeFiltered.filter(f => f.status === currentFilter);
    if (searchQuery) {
        const q = searchQuery.toLowerCase();
        filtered = filtered.filter(f =>
            (f.source_filename && f.source_filename.toLowerCase().includes(q)) ||
            (f.destination_filename && f.destination_filename.toLowerCase().includes(q)) ||
            (f.title && f.title.toLowerCase().includes(q))
        );
    }

    filtered = sortFiles(filtered);

    let html = buildToolbar(files, typeFiltered);

    if (filtered.length === 0) {
        html += '<div class="empty-state"><p>No files match this filter.</p></div>';
    } else if (currentSort === 'alpha') {
        // Build letter groups
        const letters = [];
        const groups = {};
        for (const f of filtered) {
            const letter = getLetterKey(f.title || f.source_filename);
            if (!groups[letter]) { groups[letter] = []; letters.push(letter); }
            groups[letter].push(f);
        }

        html += '<div class="alpha-layout">';
        html += '<div class="alpha-content">';
        for (const letter of letters) {
            html += '<div class="alpha-section" id="alpha-' + letter + '">';
            html += '<div class="alpha-section-header">' + letter + '</div>';
            if (currentView === 'cards') {
                html += renderCards(groups[letter]);
            } else {
                html += renderTable(groups[letter]);
            }
            html += '</div>';
        }
        html += '</div>';

        // Alphabet sidebar
        html += '<div class="alpha-sidebar">';
        var allLetters = ['#'];
        for (var c = 65; c <= 90; c++) allLetters.push(String.fromCharCode(c));
        for (const l of allLetters) {
            const hasFiles = !!groups[l];
            html += '<a class="alpha-link' + (hasFiles ? '' : ' alpha-link-dim') + '" ' +
                (hasFiles ? 'href="#alpha-' + l + '" onclick="scrollToLetter(\'' + l + '\'); return false;"' : '') +
                '>' + l + '</a>';
        }
        html += '</div>';
        html += '</div>';
    } else if (currentView === 'cards') {
        html += renderCards(filtered);
    } else {
        html += renderTable(filtered);
    }

    document.getElementById('tab-files').innerHTML = html;
}

function scrollToLetter(letter) {
    const el = document.getElementById('alpha-' + letter);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// Render duplicates
function renderDuplicates(duplicates) {
    if (!duplicates || duplicates.length === 0) {
        showEmpty('duplicates', 'No duplicates found.');
        return;
    }

    let html = '';
    for (const group of duplicates) {
        // Find poster from best file or first file with a poster
        const posterFile = group.files.find(f => f.poster_url) || group.files[0];
        const poster = posterFile ? posterFile.poster_url : null;

        html += '<div class="dup-card-with-poster">';
        if (poster) {
            html += '<div class="dup-poster"><img src="' + esc(poster) + '" alt="' + esc(group.identifier) + '" loading="lazy"></div>';
        }
        html += '<div class="dup-card-content">';
        html += '<h3>' + esc(group.identifier) + '</h3>';
        html += '<div class="dup-files">';
        for (const f of group.files) {
            const isBest = f.id === group.best_file_id;
            let rowClass = 'dup-file';
            if (f.status === 'approved') rowClass += ' dup-kept';
            else if (f.status === 'rejected') rowClass += ' dup-rejected';
            else if (isBest) rowClass += ' best';
            html += '<div class="' + rowClass + '">';
            if (f.status === 'approved') {
                html += '<span class="dup-status-icon dup-status-kept">&#10003;</span>';
            } else if (f.status === 'rejected') {
                html += '<span class="dup-status-icon dup-status-rejected">&#10005;</span>';
            }
            html += '<span class="dup-quality">' + (f.resolution || '?') + '</span>';
            html += '<span class="dup-score">Score: ' + f.quality_score + '</span>';
            html += '<span class="dup-filename" title="' + esc(f.source_path) + '">' + esc(f.source_filename) + '</span>';
            html += '<span>' + formatSize(f.file_size) + '</span>';
            if (isBest && f.status === 'pending') {
                html += '<span class="dup-best-tag">BEST</span>';
            }
            html += '<span class="dup-actions">';
            if (f.status === 'pending') {
                html += '<button class="btn btn-success btn-sm" onclick="approveFile(\'' + f.id + '\')">Keep</button> ';
                html += '<button class="btn btn-danger btn-sm" onclick="rejectFile(\'' + f.id + '\')">Reject</button>';
            } else if (f.status === 'approved') {
                html += '<span class="dup-decision-label dup-decision-kept">KEEPING</span>';
                html += '<button class="btn btn-outline btn-sm" onclick="resetFile(\'' + f.id + '\')">Undo</button>';
            } else if (f.status === 'rejected') {
                html += '<span class="dup-decision-label dup-decision-rejected">REMOVING</span>';
                html += '<button class="btn btn-outline btn-sm" onclick="resetFile(\'' + f.id + '\')">Undo</button>';
            }
            html += '</span>';
            html += '</div>';
        }
        html += '</div>';
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
        const clickable = h.has_archive ? ' history-clickable' : '';
        const onclick = h.has_archive ? ' onclick="viewArchive(\'' + h.scan_id + '\')"' : '';
        html += '<div class="history-item' + clickable + '"' + onclick + '>';
        html += '<span class="history-date">' + new Date(h.started_at).toLocaleString() + '</span>';
        html += '<span class="badge badge-' + (h.status === 'completed' ? 'completed' : 'failed') + '">' + h.status + '</span>';
        html += '<span class="history-count">' + h.total_files + ' files</span>';
        html += '<span class="history-count">' + h.duplicates + ' duplicates</span>';
        if (h.has_archive) {
            html += '<span class="history-view-link">View &rarr;</span>';
        }
        html += '</div>';
    }

    document.getElementById('tab-history').innerHTML = html;
}

// Archive viewing
async function viewArchive(scanId) {
    document.getElementById('tab-history').innerHTML = '<div class="scanning"><div class="spinner"></div><p>Loading archived scan...</p></div>';
    try {
        const scan = await api('GET', '/api/history/' + scanId);
        if (scan.detail) {
            showEmpty('history', 'Archive not found.');
            return;
        }
        renderArchive(scan, scanId);
    } catch (e) {
        showEmpty('history', 'Failed to load archive.');
    }
}

function renderArchive(scan, scanId) {
    window._archiveFiles = scan.files;

    let html = '<div class="toolbar">';
    html += '<button class="btn btn-outline btn-sm" onclick="loadHistory()">&larr; Back to History</button>';
    html += '<div class="filter-group">';
    html += '<span class="filter-label">' + new Date(scan.started_at).toLocaleString() + '</span>';
    html += '<span class="badge badge-' + scan.status + '">' + scan.status + '</span>';
    html += '<span class="filter-label">' + scan.files.length + ' files, ' + scan.duplicates.length + ' duplicates</span>';
    html += '</div>';
    html += '<div class="toolbar-spacer"></div>';
    html += '<button class="btn btn-primary btn-sm" onclick="downloadArchive(\'' + scanId + '\')">Download JSON</button>';
    html += '</div>';

    html += '<div class="toolbar">';
    html += '<input type="text" class="search-box" placeholder="Search files..." oninput="filterArchive(this.value)">';
    html += '</div>';

    html += '<table class="file-table"><thead><tr>';
    html += '<th>Status</th><th>Type</th><th>Title</th><th>Current Name</th><th></th><th>New Name</th><th>Quality</th><th>Size</th>';
    html += '</tr></thead><tbody id="archive-tbody">';

    for (const f of scan.files) {
        html += renderArchiveRow(f);
    }

    html += '</tbody></table>';
    document.getElementById('tab-history').innerHTML = html;
}

function renderArchiveRow(f) {
    let subtitle = '';
    if (f.media_type === 'movie' && f.year) {
        subtitle = ' (' + f.year + ')';
    } else if (f.media_type === 'episode' && f.season != null) {
        subtitle = ' S' + String(f.season).padStart(2, '0') + 'E' + String(f.episode).padStart(2, '0');
    }

    let html = '<tr>';
    html += '<td><span class="badge badge-' + f.status + '">' + f.status + '</span></td>';
    html += '<td>' + (f.media_type === 'movie' ? 'Movie' : 'TV') + '</td>';
    html += '<td>' + esc(f.title || 'Unknown') + subtitle + '</td>';
    html += '<td class="filename" title="' + esc(f.source_path) + '">' + esc(f.source_filename) + '</td>';
    html += '<td class="arrow">&rarr;</td>';
    html += '<td class="filename" title="' + esc(f.destination_path) + '">' + esc(f.destination_filename) + '</td>';
    html += '<td>' + (f.resolution || '-') + '</td>';
    html += '<td>' + formatSize(f.file_size) + '</td>';
    html += '</tr>';
    return html;
}

function filterArchive(query) {
    const q = query.toLowerCase();
    const filtered = q ? window._archiveFiles.filter(f =>
        (f.source_filename && f.source_filename.toLowerCase().includes(q)) ||
        (f.destination_filename && f.destination_filename.toLowerCase().includes(q)) ||
        (f.title && f.title.toLowerCase().includes(q))
    ) : window._archiveFiles;
    document.getElementById('archive-tbody').innerHTML = filtered.map(renderArchiveRow).join('');
}

function downloadArchive(scanId) {
    const key = apiKey ? '?api_key=' + encodeURIComponent(apiKey) : '';
    window.location.href = API + '/api/history/' + scanId + '/download' + key;
}

// Actions
async function triggerScan() {
    await api('POST', '/api/scan');
    document.getElementById('btn-scan').disabled = true;
    document.getElementById('tab-files').innerHTML = '<div class="scanning"><div class="spinner"></div><p>Scanning your media library...</p></div>';
    startPolling();
}

async function cancelScan() {
    await api('POST', '/api/scan/cancel');
    stopPolling();
    await loadScan();
}

function updateFileStatus(id, status) {
    // Optimistic UI update
    for (const f of cachedFiles) {
        if (f.id === id) { f.status = status; break; }
    }
    // Also update inside duplicate groups
    if (window._cachedDuplicates) {
        for (const g of window._cachedDuplicates) {
            for (const f of g.files) {
                if (f.id === id) { f.status = status; break; }
            }
        }
        renderDuplicates(window._cachedDuplicates);
    }
    renderFiles(cachedFiles);
}

async function approveFile(id) {
    updateFileStatus(id, 'approved');
    api('POST', '/api/files/' + id + '/approve');
}

async function rejectFile(id) {
    updateFileStatus(id, 'rejected');
    api('POST', '/api/files/' + id + '/reject');
}

async function resetFile(id) {
    updateFileStatus(id, 'pending');
    api('POST', '/api/files/' + id + '/pending');
}

async function approveAll() {
    for (const f of cachedFiles) {
        if (f.status === 'pending' && !f.already_correct) f.status = 'approved';
    }
    renderFiles(cachedFiles);
    api('POST', '/api/files/approve-all');
}

async function rejectAll() {
    for (const f of cachedFiles) {
        if (f.status === 'pending' && !f.already_correct) f.status = 'rejected';
    }
    renderFiles(cachedFiles);
    api('POST', '/api/files/reject-all');
}

async function keepBest(bestId, rejectIds) {
    await api('POST', '/api/files/' + bestId + '/approve');
    for (const id of rejectIds) {
        await api('POST', '/api/files/' + id + '/reject');
    }
    await loadScan();
}

async function executeApproved() {
    if (!confirm('This will rename approved files and move rejected files to trash. Continue?')) return;
    const btn = document.getElementById('btn-execute');
    btn.disabled = true;
    btn.innerHTML = '<span class="btn-spinner"></span> Running...';
    try {
        const result = await api('POST', '/api/execute');
        let msg = 'Done!';
        if (result.completed > 0) msg += ' ' + result.completed + ' renamed.';
        if (result.moved_to_trash > 0) msg += ' ' + result.moved_to_trash + ' moved to trash.';
        if (result.failed > 0) msg += ' ' + result.failed + ' failed.';
        alert(msg);
    } catch (e) {
        alert('Execute failed: ' + e.message);
    }
    btn.innerHTML = 'Run';
    await loadScan();
}

function setSearch(q) {
    searchQuery = q;
    renderFiles(cachedFiles);
}

function setSort(s) {
    currentSort = s;
    localStorage.setItem('renamarr_sort', s);
    renderFiles(cachedFiles);
}

function getLetterKey(title) {
    if (!title) return '#';
    const first = title.trim().charAt(0).toUpperCase();
    if (first >= 'A' && first <= 'Z') return first;
    return '#';
}

function sortFiles(files) {
    if (currentSort !== 'alpha') return files;
    return [...files].sort((a, b) => {
        const ta = (a.title || a.source_filename || '').toLowerCase();
        const tb = (b.title || b.source_filename || '').toLowerCase();
        return ta.localeCompare(tb);
    });
}

function setFilter(f) {
    currentFilter = f;
    renderFiles(cachedFiles);
}

function setTypeFilter(f) {
    currentTypeFilter = f;
    renderFiles(cachedFiles);
}

function esc(s) {
    if (!s) return '';
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// Trash management
let trashAuthRequired = false;

async function loadTrash() {
    try {
        const data = await api('GET', '/api/trash');
        trashAuthRequired = data.delete_auth_required;
        document.getElementById('badge-trash').textContent = data.count;
        renderTrash(data);
    } catch (e) {
        showEmpty('trash', 'Failed to load trash.');
    }
}

function renderTrash(data) {
    if (!data.files || data.files.length === 0) {
        showEmpty('trash', 'Trash is empty.');
        return;
    }

    let html = '<div class="toolbar">';
    html += '<div class="filter-group">';
    html += '<span class="filter-label">' + data.count + ' files (' + data.total_size_human + ')</span>';
    html += '</div>';
    html += '<div class="toolbar-spacer"></div>';
    html += '<button class="btn btn-danger btn-sm" onclick="emptyTrash()">Empty Trash</button>';
    html += '</div>';

    html += '<table class="file-table"><thead><tr>';
    html += '<th>Filename</th><th>Size</th><th>Date Moved</th><th>Actions</th>';
    html += '</tr></thead><tbody>';

    for (const f of data.files) {
        html += '<tr>';
        html += '<td class="filename" title="' + esc(f.name) + '">' + esc(f.name) + '</td>';
        html += '<td>' + f.size_human + '</td>';
        html += '<td>' + new Date(f.modified).toLocaleString() + '</td>';
        html += '<td><button class="btn btn-danger btn-sm" onclick="deleteTrashFile(\'' + esc(f.name) + '\')">Delete</button></td>';
        html += '</tr>';
    }

    html += '</tbody></table>';

    if (trashAuthRequired) {
        html += '<div class="auth-notice">Delete operations require an 8-digit code. Run: <code>docker exec renamarr python -m src.main --delete-code YOUR_PASSPHRASE</code></div>';
    }

    document.getElementById('tab-trash').innerHTML = html;
}

async function getDeleteCode() {
    if (!trashAuthRequired) return '';
    const code = prompt('Enter your 8-digit delete code.\n\nGenerate one by running:\ndocker exec renamarr python -m src.main --delete-code YOUR_PASSPHRASE');
    if (!code) return null;
    return code.trim();
}

async function deleteTrashFile(filename) {
    if (!confirm('Permanently delete "' + filename + '"?')) return;
    const code = await getDeleteCode();
    if (code === null) return;
    try {
        const headers = getHeaders();
        if (code) headers['X-Delete-Code'] = code;
        const r = await fetch(API + '/api/trash/' + encodeURIComponent(filename), { method: 'DELETE', headers });
        const data = await r.json();
        if (!r.ok) {
            alert(data.detail || 'Delete failed');
            return;
        }
        await loadTrash();
    } catch (e) {
        alert('Delete failed: ' + e.message);
    }
}

async function emptyTrash() {
    if (!confirm('Permanently delete ALL files in trash? This cannot be undone.')) return;
    const code = await getDeleteCode();
    if (code === null) return;
    try {
        const headers = getHeaders();
        if (code) headers['X-Delete-Code'] = code;
        const r = await fetch(API + '/api/trash', { method: 'DELETE', headers });
        const data = await r.json();
        if (!r.ok) {
            alert(data.detail || 'Delete failed');
            return;
        }
        alert(data.deleted + ' files deleted.');
        await loadTrash();
    } catch (e) {
        alert('Delete failed: ' + e.message);
    }
}

// Library dedup
let libraryPolling = null;

async function loadLibraryScan() {
    try {
        const scan = await api('GET', '/api/library/scan/current');
        if (scan.detail) {
            showLibraryEmpty();
            return;
        }

        const status = await refreshStatus();
        if (scan.status === 'running' && status.scanning) {
            document.getElementById('tab-library').innerHTML = '<div class="scanning"><div class="spinner"></div><p>Scanning library for duplicate folders and misnamed directories...</p></div>';
            startLibraryPolling();
            return;
        }

        stopLibraryPolling();
        const totalIssues = (scan.groups || []).length + (scan.folder_renames || []).length;
        document.getElementById('badge-library').textContent = totalIssues;
        renderLibrary(scan);
    } catch (e) {
        showLibraryEmpty();
    }
}

function showLibraryEmpty() {
    document.getElementById('tab-library').innerHTML =
        '<div class="toolbar"><div class="toolbar-spacer"></div>' +
        '<button class="btn btn-primary btn-sm" onclick="triggerLibraryScan()">Scan Library for Duplicates</button>' +
        '</div>' +
        '<div class="empty-state"><p>No library scan results. Click "Scan Library for Duplicates" to find case-insensitive duplicate folders.</p></div>';
}

function renderLibrary(scan) {
    const groups = scan.groups || [];
    const renames = scan.folder_renames || [];
    const allApproved = groups.filter(g => g.status === 'approved').length
        + renames.filter(r => r.status === 'approved').length;
    const totalIssues = groups.length + renames.length;

    let html = '<div class="toolbar">';
    html += '<div class="filter-group">';
    html += '<span class="filter-label">' + groups.length + ' duplicate groups, ' + renames.length + ' misnamed folders</span>';
    html += '</div>';
    html += '<div class="toolbar-spacer"></div>';
    html += '<button class="btn btn-primary btn-sm" onclick="triggerLibraryScan()">Re-scan</button> ';
    html += '<button class="btn btn-success btn-sm" onclick="approveAllLibrary()">Approve All</button> ';
    html += '<button class="btn btn-primary btn-sm" onclick="executeLibrary()"' + (allApproved === 0 ? ' disabled' : '') + '>Execute (' + allApproved + ')</button>';
    html += '</div>';

    if (totalIssues === 0) {
        html += '<div class="empty-state"><p>No issues found. Your library is clean!</p></div>';
        document.getElementById('tab-library').innerHTML = html;
        return;
    }

    // Render misnamed folders first
    if (renames.length > 0) {
        html += '<h2 class="library-section-title">Misnamed Folders</h2>';
        for (const r of renames) {
            html += '<div class="merge-group">';
            html += '<div class="merge-header">';
            html += '<span class="badge badge-' + r.status + '">' + r.status + '</span>';
            html += '<span class="merge-type badge card-type-' + r.media_type + '">' + (r.media_type === 'movie' ? 'Movie' : 'TV') + '</span>';
            if (r.title) {
                html += '<h3>' + esc(r.title) + (r.year ? ' (' + r.year + ')' : '') + '</h3>';
            }
            html += '</div>';

            html += '<div class="merge-details">';
            html += '<div class="merge-folder merge-duplicate">';
            html += '<span class="badge badge-rejected">CURRENT</span> ';
            html += '<span class="merge-path" title="' + esc(r.current_path) + '">' + esc(r.current_name) + '</span>';
            html += '<span class="merge-stats">' + r.file_count + ' files, ' + r.total_size_human + '</span>';
            html += '</div>';
            html += '<div class="merge-folder merge-canonical">';
            html += '<span class="dup-best-tag">RENAME TO</span> ';
            html += '<span class="merge-path">' + esc(r.proposed_name) + '</span>';
            html += '</div>';
            html += '</div>';

            html += '<div class="merge-actions">';
            if (r.status === 'pending') {
                html += '<button class="btn btn-success btn-sm" onclick="approveFolderRename(\'' + r.id + '\')">Approve</button> ';
                html += '<button class="btn btn-outline btn-sm" onclick="skipFolderRename(\'' + r.id + '\')">Skip</button> ';
                html += '<button class="btn btn-outline btn-sm" onclick="editFolderRename(\'' + r.id + '\', \'' + esc(r.proposed_name) + '\')">Edit Name</button>';
            } else if (r.status === 'approved' || r.status === 'skipped') {
                html += '<button class="btn btn-outline btn-sm" onclick="resetFolderRename(\'' + r.id + '\')">Undo</button>';
            }
            html += '</div>';

            html += '</div>';
        }
    }

    // Render duplicate folder groups
    if (groups.length > 0) {
        html += '<h2 class="library-section-title">Duplicate Folders</h2>';
        for (const g of groups) {
            html += '<div class="merge-group">';
            html += '<div class="merge-header">';
            html += '<span class="badge badge-' + g.status + '">' + g.status + '</span>';
            html += '<span class="merge-type badge card-type-' + g.media_type + '">' + (g.media_type === 'movie' ? 'Movie' : 'TV') + '</span>';
            html += '<h3>' + esc(g.canonical_name) + '</h3>';
            html += '</div>';

            html += '<div class="merge-details">';
            html += '<div class="merge-folder merge-canonical">';
            html += '<span class="dup-best-tag">KEEP</span> ';
            html += '<span class="merge-path" title="' + esc(g.canonical_path) + '">' + esc(g.canonical_path) + '</span>';
            html += '<span class="merge-stats">' + g.canonical_file_count + ' files, ' + g.canonical_size_human + '</span>';
            html += '</div>';

            for (let i = 0; i < g.duplicate_paths.length; i++) {
                html += '<div class="merge-folder merge-duplicate">';
                html += '<span class="badge badge-rejected">MERGE</span> ';
                html += '<span class="merge-path" title="' + esc(g.duplicate_paths[i]) + '">' + esc(g.duplicate_paths[i]) + '</span>';
                html += '</div>';
            }

            html += '<div class="merge-summary">';
            if (g.duplicate_file_count === 0) {
                html += 'Empty duplicate folder &mdash; will be removed';
            } else {
                html += g.duplicate_file_count + ' files (' + g.duplicate_size_human + ') to merge';
                if (g.conflicts > 0) {
                    html += ' &middot; <span style="color:var(--yellow)">' + g.conflicts + ' filename conflicts (will be auto-renamed)</span>';
                }
            }
            html += '</div>';
            html += '</div>';

            html += '<div class="merge-actions">';
            if (g.status === 'pending') {
                html += '<button class="btn btn-success btn-sm" onclick="approveLibraryGroup(\'' + g.id + '\')">Approve</button> ';
                html += '<button class="btn btn-outline btn-sm" onclick="skipLibraryGroup(\'' + g.id + '\')">Skip</button>';
            } else if (g.status === 'approved' || g.status === 'skipped') {
                html += '<button class="btn btn-outline btn-sm" onclick="resetLibraryGroup(\'' + g.id + '\')">Undo</button>';
            }
            html += '</div>';

            html += '</div>';
        }
    }

    document.getElementById('tab-library').innerHTML = html;
}

async function triggerLibraryScan() {
    await api('POST', '/api/library/scan');
    document.getElementById('tab-library').innerHTML = '<div class="scanning"><div class="spinner"></div><p>Scanning library for duplicate folders...</p></div>';
    startLibraryPolling();
}

function startLibraryPolling() {
    if (libraryPolling) return;
    libraryPolling = setInterval(async () => {
        const s = await refreshStatus();
        if (!s.scanning) {
            stopLibraryPolling();
            await loadLibraryScan();
        }
    }, 3000);
}

function stopLibraryPolling() {
    if (libraryPolling) { clearInterval(libraryPolling); libraryPolling = null; }
}

async function approveLibraryGroup(id) {
    await api('POST', '/api/library/groups/' + id + '/approve');
    await loadLibraryScan();
}

async function skipLibraryGroup(id) {
    await api('POST', '/api/library/groups/' + id + '/skip');
    await loadLibraryScan();
}

async function resetLibraryGroup(id) {
    await api('POST', '/api/library/groups/' + id + '/pending');
    await loadLibraryScan();
}

async function approveAllLibrary() {
    await api('POST', '/api/library/groups/approve-all');
    await api('POST', '/api/library/renames/approve-all');
    await loadLibraryScan();
}

async function approveFolderRename(id) {
    await api('POST', '/api/library/renames/' + id + '/approve');
    await loadLibraryScan();
}

async function skipFolderRename(id) {
    await api('POST', '/api/library/renames/' + id + '/skip');
    await loadLibraryScan();
}

async function resetFolderRename(id) {
    await api('POST', '/api/library/renames/' + id + '/pending');
    await loadLibraryScan();
}

async function editFolderRename(id, currentName) {
    const newName = prompt('Enter the correct folder name:', currentName);
    if (!newName || newName.trim() === currentName) return;
    await api('POST', '/api/library/renames/' + id + '/edit', { proposed_name: newName.trim() });
    await loadLibraryScan();
}

async function executeLibrary() {
    if (!confirm('This will execute all approved folder merges and renames. Continue?')) return;
    const result = await api('POST', '/api/library/execute');
    let msg = 'Done!';
    if (result.merged > 0) msg += ' ' + result.merged + ' merged.';
    if (result.renamed > 0) msg += ' ' + result.renamed + ' renamed.';
    if (result.moved_files > 0) msg += ' ' + result.moved_files + ' files moved.';
    if (result.failed > 0) msg += ' ' + result.failed + ' failed.';
    alert(msg);
    await loadLibraryScan();
}

// Metadata retry/edit
async function retryLookup(id) {
    const btn = event.target;
    btn.disabled = true;
    btn.textContent = '...';
    try {
        const result = await api('POST', '/api/files/' + id + '/retry');
        if (result.detail) {
            alert('Retry failed: ' + result.detail);
        }
        await loadScan();
    } catch (e) {
        alert('Retry failed: ' + e.message);
    }
    btn.disabled = false;
    btn.innerHTML = '&#x21bb;';
}

async function editMetadata(id, currentTitle, currentYear) {
    const title = prompt('Enter title:', currentTitle);
    if (title === null) return;
    const yearStr = prompt('Enter year (or leave blank):', currentYear || '');
    if (yearStr === null) return;
    const year = yearStr ? parseInt(yearStr, 10) : null;
    if (yearStr && isNaN(year)) {
        alert('Invalid year.');
        return;
    }
    const body = {};
    if (title) body.title = title.trim();
    if (year) body.year = year;
    try {
        const result = await api('POST', '/api/files/' + id + '/retry', body);
        if (result.detail) {
            alert('Lookup failed: ' + result.detail);
        }
        await loadScan();
    } catch (e) {
        alert('Lookup failed: ' + e.message);
    }
}

// Activity log
let activityPolling = null;
let lastLogId = 0;
let autoScroll = true;

async function loadActivityLog() {
    try {
        const data = await api('GET', '/api/logs?after=' + lastLogId);
        if (data.logs && data.logs.length > 0) {
            const container = document.getElementById('activity-log');
            if (!container) return;
            for (const entry of data.logs) {
                const line = document.createElement('div');
                line.className = 'log-line log-' + entry.level.toLowerCase();
                line.innerHTML = '<span class="log-time">' + entry.time + '</span>'
                    + '<span class="log-level">' + entry.level + '</span>'
                    + '<span class="log-msg">' + esc(entry.message) + '</span>';
                container.appendChild(line);
                lastLogId = entry.id;
            }
            // Keep buffer trimmed in DOM
            while (container.children.length > 500) {
                container.removeChild(container.firstChild);
            }
            if (autoScroll) {
                container.scrollTop = container.scrollHeight;
            }
        }
    } catch (e) { /* ignore */ }
}

function startActivityPolling() {
    if (activityPolling) return;
    loadActivityLog();
    activityPolling = setInterval(loadActivityLog, 2000);
}

function stopActivityPolling() {
    if (activityPolling) { clearInterval(activityPolling); activityPolling = null; }
}

// Init
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.tab').forEach(t => {
        t.addEventListener('click', () => {
            switchTab(t.dataset.tab);
            if (t.dataset.tab === 'history') loadHistory();
            if (t.dataset.tab === 'trash') loadTrash();
            if (t.dataset.tab === 'library') loadLibraryScan();
            if (t.dataset.tab === 'activity') startActivityPolling();
            else stopActivityPolling();
        });
    });

    loadScan();
});
