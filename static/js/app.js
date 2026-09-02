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
    
    // Check authentication
    await checkAuthentication();
    
    // Load initial data
    console.log('NAZMan initialized');
}

async function checkAuthentication() {
    // For now, just check if we can access the API
    try {
        await api.getSystemStatus();
    } catch (error) {
        if (error.message === 'Unauthorized') {
            console.log('Authentication required');
        }
    }
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
