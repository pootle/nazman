// Main Application Logic for NAZMan

// Initialize application
document.addEventListener('DOMContentLoaded', function() {
    initializeApp();
});

async function initializeApp() {
    // Setup refresh button
    const refreshBtn = document.getElementById('refresh-btn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', refreshCurrentPage);
    }

    // Setup logout button
    const logoutBtn = document.getElementById('logout-btn');
    if (logoutBtn) {
        logoutBtn.addEventListener('click', () => {
            api.logout();
            window.location.reload();
        });
        if (api._hasToken()) {
            logoutBtn.style.display = '';
        }
    }

    // Check authentication
    await checkAuthentication();
    
    // Start the global activity + notification pollers.
    initGlobalPollers();

    // Load initial data
    console.log('NAZMan initialized');
}

async function checkAuthentication() {
    // Prompt only when there is no token at all. Token validity is enforced
    // lazily by the API client (any 401 triggers re-auth + retry); probing
    // /api/system/status here used to run a full disk/SMART sync on every
    // page load AND — worse — a transient probe failure destroyed a valid
    // session, re-prompting on the next page.
    if (!api._hasToken()) {
        await api._requireAuth().catch(() => {});
    }
}

// ── Global activity + notification pollers ──────────────────────────────
// Present on every screen: the bell badge reflects unread journal entries and
// the sidebar indicator shows long-running tasks so a backup or restore can
// be watched without staying on the page that started it.
let activeTasksPollTimer = null;
let lastActiveTaskKeys = new Set();

function initGlobalPollers() {
    refreshNotificationBadge();
    pollActiveTasks();
    setInterval(refreshNotificationBadge, 30000);
}

async function pollActiveTasks() {
    if (activeTasksPollTimer) return;
    activeTasksPollTimer = setTimeout(pollActiveTasks, 4000);
    let data;
    try {
        data = await api.getActiveTasks();
    } catch (e) {
        return;
    }
    const tasks = data.tasks || [];
    const keys = new Set(tasks.map(taskKey));
    // A task that was running and is now gone finished; its completion was
    // journaled server-side, so refresh the bell to surface it.
    for (const old of lastActiveTaskKeys) {
        if (!keys.has(old)) {
            refreshNotificationBadge();
            break;
        }
    }
    lastActiveTaskKeys = keys;
    renderActiveTasksBadge(tasks.length);
    const modal = document.getElementById('active-tasks-modal');
    if (modal && modal.style.display !== 'none') {
        renderActiveTasksModal(tasks);
    }
}

function taskKey(t) {
    return `${t.kind}:${t.id}`;
}

function renderActiveTasksBadge(count) {
    const badge = document.getElementById('active-tasks-badge');
    if (!badge) return;
    badge.textContent = count > 99 ? '99+' : String(count);
    badge.style.display = count > 0 ? '' : 'none';
    const item = document.getElementById('active-tasks-nav');
    if (item) item.classList.toggle('has-active', count > 0);
}

function openActiveTasks() {
    showModal('active-tasks-modal');
    renderActiveTasksModalFromServer();
}

async function renderActiveTasksModalFromServer() {
    const body = document.getElementById('active-tasks-body');
    if (body) body.innerHTML = createLoading();
    try {
        const data = await api.getActiveTasks();
        renderActiveTasksModal(data.tasks || []);
    } catch (e) {
        if (body) body.innerHTML = createErrorState('Failed to load activity: ' + e.message);
    }
}

function renderActiveTasksModal(tasks) {
    const body = document.getElementById('active-tasks-body');
    if (!body) return;
    if (tasks.length === 0) {
        body.innerHTML = '<p class="text-muted">Nothing is running right now.</p>';
        return;
    }
    body.innerHTML = tasks.map(t => {
        const pct = t.progress_pct != null ? Math.min(100, Math.max(0, t.progress_pct)) : null;
        const bar = pct != null
            ? `<div class="task-progress"><div class="task-progress-fill" style="width:${pct}%"></div></div>`
            : `<div class="task-progress task-progress-indeterminate"><div class="task-progress-fill"></div></div>`;
        const detail = t.detail ? `<span class="text-muted">${escapeHtml(t.detail)}</span>` : '';
        const started = t.started_at ? `<span class="text-muted">started ${formatDate(t.started_at)}</span>` : '';
        const link = t.link ? `<a class="task-link" href="${escapeHtml(t.link)}">View</a>` : '';
        return `<div class="task-entry">
            <div class="task-head">
                <span class="task-kind">${escapeHtml(t.kind)}</span>
                <span class="task-label">${escapeHtml(t.label || '')}</span>
                <span class="task-meta">${detail} ${started}</span>
                ${link}
            </div>
            ${bar}
        </div>`;
    }).join('');
}

function refreshCurrentPage() {
    const path = window.location.pathname;
    
    // Reload current page data based on path
    switch (path) {
        case '/dashboard':
            if (typeof loadDashboard === 'function') {
                loadDashboard();
            }
            break;
        case '/disks':
            if (typeof loadDisksPage === 'function') {
                loadDisksPage();
            }
            break;
        case '/pools':
            if (typeof loadPoolsPage === 'function') {
                loadPoolsPage();
            }
            break;
        case '/datasets':
            if (typeof loadDatasetsPage === 'function') {
                loadDatasetsPage();
            }
            break;
        case '/nfs':
            if (typeof loadNfsPage === 'function') {
                loadNfsPage();
            }
            break;
        case '/smb':
            if (typeof loadSmbPage === 'function') {
                loadSmbPage();
            }
            break;
        case '/snapshots':
            if (typeof loadSnapshotsPage === 'function') {
                loadSnapshotsPage();
            }
            break;
        case '/backup':
            if (typeof loadBackupPage === 'function') {
                loadBackupPage();
            }
            break;
        case '/restore':
            if (typeof loadRestorePage === 'function') {
                loadRestorePage();
            }
            break;
        case '/monitoring':
            if (typeof loadMonitoringPage === 'function') {
                loadMonitoringPage();
            }
            break;
        default:
            console.log('No refresh handler for path:', path);
    }
}

// Utility functions
function showLoading(elementId) {
    const element = document.getElementById(elementId);
    if (element) {
        element.innerHTML = createLoading();
    }
}

function showError(elementId, message) {
    const element = document.getElementById(elementId);
    if (element) {
        element.innerHTML = createErrorState(message);
    }
}

function showEmpty(elementId, message, link = null) {
    const element = document.getElementById(elementId);
    if (element) {
        element.innerHTML = createEmptyState(message, link);
    }
}

// Form helpers
function getFormData(formId) {
    const form = document.getElementById(formId);
    if (!form) return null;
    
    const formData = new FormData(form);
    const data = {};
    
    for (let [key, value] of formData.entries()) {
        data[key] = value;
    }
    
    return data;
}

function resetForm(formId) {
    const form = document.getElementById(formId);
    if (form) {
        form.reset();
    }
}

// Table helpers
function renderTable(containerId, headers, rows, options = {}) {
    const container = document.getElementById(containerId);
    if (!container) return;
    
    if (rows.length === 0) {
        container.innerHTML = createEmptyState(options.emptyMessage || 'No data available');
        return;
    }
    
    let html = '<table class="data-table">';
    
    // Headers
    html += '<thead><tr>';
    headers.forEach(header => {
        html += `<th>${header}</th>`;
    });
    html += '</tr></thead>';
    
    // Rows
    html += '<tbody>';
    rows.forEach(row => {
        html += '<tr>';
        headers.forEach(header => {
            const key = header.toLowerCase().replace(/\s+/g, '_');
            const value = row[key] || row[header] || '';
            html += `<td>${value}</td>`;
        });
        html += '</tr>';
    });
    html += '</tbody>';
    
    html += '</table>';
    container.innerHTML = html;
}

// Modal helpers
function showModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.style.display = 'flex';
    }
}

function hideModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.style.display = 'none';
    }
}

// Event listeners for modals
document.addEventListener('click', function(e) {
    if (e.target.classList.contains('modal')) {
        e.target.style.display = 'none';
    }
});

// Keyboard shortcuts
document.addEventListener('keydown', function(e) {
    // Escape key closes modals
    if (e.key === 'Escape') {
        document.querySelectorAll('.modal').forEach(modal => {
            modal.style.display = 'none';
        });
    }
    
    // Ctrl+R refreshes page
    if (e.ctrlKey && e.key === 'r') {
        e.preventDefault();
        refreshCurrentPage();
    }
});
