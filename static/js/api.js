// API Client for NAZMan
class NasManAPI {
    constructor(baseUrl = '') {
        this.baseUrl = baseUrl;
        this.token = localStorage.getItem('nazman_token');
        this._loginResolvers = [];
        this._loginActive = false;
    }

    async request(method, path, data = null) {
        const headers = {
            'Content-Type': 'application/json'
        };

        if (this.token) {
            headers['Authorization'] = `Bearer ${this.token}`;
        }

        const options = {
            method,
            headers
        };

        if (data && (method === 'POST' || method === 'PUT' || method === 'PATCH')) {
            options.body = JSON.stringify(data);
        }

        const response = await fetch(`${this.baseUrl}${path}`, options);

        if (response.status === 401) {
            // A stored token is present but no longer valid; drop it so we prompt.
            if (this.token) {
                this.logout();
            }
            await this._requireAuth();
            // Retry once with the (now-present) token.
            return this.request(method, path, data);
        }

        if (!response.ok) {
            const error = await response.json().catch(() => ({ detail: 'Request failed' }));
            throw new Error(error.detail || 'Request failed');
        }

        return response.json();
    }

    // System
    async getSystemStatus() {
        return this.request('GET', '/api/system/status');
    }

    async getSystemMetrics() {
        return this.request('GET', '/api/system/metrics');
    }

    async getCommandLog({ type, status } = {}) {
        const params = new URLSearchParams();
        if (type) params.set('type', type);
        if (status) params.set('status', status);
        const qs = params.toString();
        return this.request('GET', `/api/system/command-log${qs ? '?' + qs : ''}`);
    }

    // Monitoring
    async getMonitoringSummary() {
        return this.request('GET', '/api/monitoring/summary');
    }

    async getMonitoringHistory(pool, metric, days, device = null) {
        let q = `/api/monitoring/history?pool=${encodeURIComponent(pool)}&metric=${encodeURIComponent(metric)}&days=${days}`;
        if (device) q += `&device=${encodeURIComponent(device)}`;
        return this.request('GET', q);
    }

    async getLoggingState() {
        return this.request('GET', '/api/monitoring/logging');
    }

    async setLoggingEnabled(pool, enabled) {
        return this.request('POST', `/api/monitoring/logging?pool=${encodeURIComponent(pool)}&enabled=${enabled}`);
    }

    // Disks
    async getDisks() {
        return this.request('GET', '/api/disks/');
    }

    async getDisk(id) {
        return this.request('GET', `/api/disks/${id}`);
    }

    async getDiskHealth(id) {
        return this.request('GET', `/api/disks/${id}/health`);
    }

    async getDiskPartitions(diskId) {
        return this.request('GET', `/api/disks/${diskId}/partitions`);
    }

    async wipeDisk(diskId) {
        return this.request('POST', `/api/disks/${diskId}/wipe`);
    }

    async partitionDisk(diskId, data) {
        return this.request('POST', `/api/disks/${diskId}/partition`, data);
    }

    async batchPartitionDisks(data) {
        return this.request('POST', '/api/disks/batch-partition', data);
    }

    async batchWipeDisks(data) {
        return this.request('POST', '/api/disks/batch-wipe', data);
    }

    async updateDisk(diskId, data) {
        return this.request('PATCH', `/api/disks/${diskId}`, data);
    }

    async deleteDisk(diskId) {
        return this.request('DELETE', `/api/disks/${diskId}`);
    }

    async secureWipeDisk(diskId) {
        return this.request('POST', `/api/disks/${diskId}/secure-wipe`);
    }

    async resurrectDisk(diskId) {
        return this.request('POST', `/api/disks/${diskId}/resurrect`);
    }

    // Pools
    async getPools() {
        return this.request('GET', '/api/pools/');
    }

    async getPoolStatus(name) {
        return this.request('GET', `/api/pools/${name}`);
    }

    async getPoolDestroyInfo(name) {
        return this.request('GET', `/api/pools/${name}/destroy-info`);
    }

    async createPool(data) {
        return this.request('POST', '/api/pools/', data);
    }

    async scrubPool(name) {
        return this.request('POST', `/api/pools/${name}/scrub`);
    }

    async exportPool(name) {
        return this.request('POST', `/api/pools/${name}/export`);
    }

    async importPool(name) {
        return this.request('POST', `/api/pools/${name}/import`);
    }

    async destroyPool(name) {
        return this.request('DELETE', `/api/pools/${name}`);
    }

    async removeDevice(poolName, devicePath) {
        return this.request('DELETE', `/api/pools/${poolName}/devices/${devicePath}`);
    }

    // Datasets
    async getDatasets(poolName = null) {
        const params = poolName ? `?pool_name=${poolName}` : '';
        return this.request('GET', `/api/datasets/${params}`);
    }

    async getDataset(name) {
        return this.request('GET', `/api/datasets/${name}`);
    }

    async createDataset(data) {
        return this.request('POST', '/api/datasets/', data);
    }

    async updateDataset(name, data) {
        return this.request('PUT', `/api/datasets/${name}`, data);
    }

    async destroyDataset(name, recursive = false) {
        return this.request('DELETE', `/api/datasets/${name}?recursive=${recursive}`);
    }

    // NFS
    async getNfsExports() {
        return this.request('GET', '/api/nfs/');
    }

    async getActiveExports() {
        return this.request('GET', '/api/nfs/active');
    }

    async createNfsExport(data) {
        return this.request('POST', '/api/nfs/', data);
    }

    async updateNfsExport(name, data) {
        return this.request('PUT', `/api/nfs/${name}`, data);
    }

    async deleteNfsExport(name) {
        return this.request('DELETE', `/api/nfs/${name}`);
    }

    async getNfsPresence() {
        return this.request('GET', '/api/nfs/presence');
    }

    async installNfs() {
        return this.request('POST', '/api/nfs/install');
    }

    // SMB
    async getSmbPresence() {
        return this.request('GET', '/api/smb/presence');
    }

    async installSmb() {
        return this.request('POST', '/api/smb/install');
    }

    async getSmbShares() {
        return this.request('GET', '/api/smb/');
    }

    async createSmbShare(data) {
        return this.request('POST', '/api/smb/', data);
    }

    async updateSmbShare(name, data) {
        return this.request('PUT', `/api/smb/${name}`, data);
    }

    async deleteSmbShare(name) {
        return this.request('DELETE', `/api/smb/${name}`);
    }

    // Snapshots
    async getSnapshots(datasetName = null) {
        const params = datasetName ? `?dataset_name=${datasetName}` : '';
        return this.request('GET', `/api/snapshots/${params}`);
    }

    async createSnapshot(data) {
        return this.request('POST', '/api/snapshots/', data);
    }

    async destroySnapshot(name) {
        return this.request('DELETE', `/api/snapshots/${name}`);
    }

    // Backup
    async getBackupStatus() {
        return this.request('GET', '/api/backup/status');
    }

    async getBackupHistory(limit = 50) {
        return this.request('GET', `/api/backup/history?limit=${limit}`);
    }

    async createBackup(message = null) {
        const params = message ? `?message=${encodeURIComponent(message)}` : '';
        return this.request('POST', `/api/backup/backup${params}`);
    }

    async restoreBackup(commitHash) {
        return this.request('POST', '/api/backup/restore', { commit_hash: commitHash });
    }

    // ZFS data backup
    async listBackupDisks() {
        return this.request('GET', '/api/backup-zfs/disks');
    }

    async backupDiskCandidates() {
        return this.request('GET', '/api/backup-zfs/disks/candidates');
    }

    async declareBackupDisk(diskId, confirm) {
        return this.request('POST', `/api/backup-zfs/disks/${diskId}/declare`, { confirm });
    }

    async mountBackupDisk(id) {
        return this.request('POST', `/api/backup-zfs/disks/${id}/mount`);
    }

    async unmountBackupDisk(id) {
        return this.request('POST', `/api/backup-zfs/disks/${id}/unmount`);
    }

    async scanBackupDisk(id) {
        return this.request('POST', `/api/backup-zfs/disks/${id}/scan`);
    }

    async deleteBackupDisk(id) {
        return this.request('DELETE', `/api/backup-zfs/disks/${id}`);
    }

    async listBackupableDatasets() {
        return this.request('GET', '/api/backup-zfs/datasets');
    }

    async listBackupRuns() {
        return this.request('GET', '/api/backup-zfs/runs');
    }

    async runBackup(datasetName, backupDiskId, type) {
        return this.request('POST', '/api/backup-zfs/runs', {
            dataset_name: datasetName, backup_disk_id: backupDiskId, backup_type: type
        });
    }

    async restoreBackupRun(runId, datasetName) {
        return this.request('POST', `/api/backup-zfs/runs/${runId}/restore`, { dataset_name: datasetName });
    }

    async upsertBackupSchedule(payload) {
        return this.request('POST', '/api/backup-zfs/schedules', payload);
    }

    async deleteBackupSchedule(datasetName) {
        return this.request('DELETE', `/api/backup-zfs/schedules/${encodeURIComponent(datasetName)}`);
    }

    async listDiskStreams(backupDiskId) {
        return this.request('GET', `/api/backup-zfs/disks/${backupDiskId}/streams`);
    }

    async restoreFromFile(backupDiskId, streamFile, datasetName) {
        return this.request('POST', '/api/backup-zfs/restore-file', {
            stream_file: streamFile, dataset_name: datasetName
        });
    }

    // Authentication
    async login(password) {
        const response = await fetch(`${this.baseUrl}/api/auth/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password })
        });

        if (response.ok) {
            const data = await response.json();
            this.token = data.token;
            localStorage.setItem('nazman_token', data.token);
            return true;
        }
        return false;
    }

    logout() {
        this.token = null;
        localStorage.removeItem('nazman_token');
        const logoutBtn = document.getElementById('logout-btn');
        if (logoutBtn) logoutBtn.style.display = 'none';
    }

    _hasToken() {
        return Boolean(this.token);
    }

    _requireAuth() {
        // If we already have a (valid) token, nothing to wait on.
        if (this._hasToken()) {
            return Promise.resolve();
        }
        // Queue a promise that resolves once the user signs in successfully.
        return new Promise((resolve) => {
            this._loginResolvers.push(resolve);
            this._showLoginModal();
        });
    }

    _showLoginModal() {
        if (this._loginActive) {
            return;
        }
        this._loginActive = true;

        const modal = document.getElementById('login-modal');
        const password = document.getElementById('login-password');
        const errorEl = document.getElementById('login-error');
        const loginBtn = document.getElementById('login-btn');

        if (!modal || !password || !loginBtn) {
            return;
        }

        const hideError = () => { if (errorEl) errorEl.style.display = 'none'; };

        const doLogin = async () => {
            hideError();
            loginBtn.disabled = true;
            const ok = await this.login(password.value);
            loginBtn.disabled = false;
            if (ok) {
                modal.style.display = 'none';
                password.value = '';
                this._loginActive = false;
                const resolvers = this._loginResolvers.splice(0);
                resolvers.forEach((r) => r());
            } else {
                if (errorEl) {
                    errorEl.textContent = 'Invalid password.';
                    errorEl.style.display = 'block';
                }
                password.focus();
            }
        };

        loginBtn.onclick = doLogin;
        password.onkeydown = (e) => {
            if (e.key === 'Enter') doLogin();
            hideError();
        };

        modal.style.display = 'flex';
        password.focus();
    }

    showLoginModal() {
        this._showLoginModal();
    }
}

const api = new NasManAPI();
