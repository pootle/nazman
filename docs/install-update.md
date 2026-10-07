# Install & Update NAZMan

NAZMan runs on Ubuntu Server or Raspberry Pi OS (64-bit). Installing or updating
is scripted — you never hand-edit system files.

## What an install creates

| Path | Purpose |
|---|---|
| `/opt/nazman` | Checkout of the production branch — the running code |
| `/etc/nazman` | Configuration (`nazman.conf`) and the SQLite database |
| `/var/lib/nazman` | Metrics and runtime state |
| `/var/log/nazman` | Service logs |
| `nazman.service` | Systemd unit that serves the web UI on port 8080 |

## Fresh install

From a shell on the machine:

```bash
curl -fsSL https://raw.githubusercontent.com/pootle/nazman/main/install.sh | sudo bash
```

`install.sh` downloads the production branch into `/opt/nazman`, runs
`prepare.sh` to install the OS packages (ZFS, NFS, parted, smartmontools,
Python), then `build.sh` to create the directories, the Python virtualenv, the
default config, the shared `nfsanon` user, and the systemd service. It is safe
to re-run.

Then open `http://<server-ip>:8080` and sign in. A fresh install accepts any
password; set `AUTH_PASSWORD_HASH` in `/etc/nazman/nazman.conf` to enforce one.

Verify the service is running:

```bash
sudo systemctl status nazman
sudo journalctl -u nazman -f
```

## Updating

Updates replace only the code — the configuration in `/etc/nazman` and your
database are left untouched. Two equivalent options:

**Option A — re-run the one-liner** (full update). This refetches the latest
production branch and re-runs `prepare.sh` + `build.sh`:

```bash
curl -fsSL https://raw.githubusercontent.com/pootle/nazman/main/install.sh | sudo bash
```

**Option B — pull + deploy** (incremental). Faster when only the code changed:

```bash
cd /opt/nazman && sudo git pull
sudo ./deploy.sh
```

`deploy.sh` copies only the new/changed/deleted application files (per the
`deploy.txt` manifest) and restarts the service. It reports what it applied and
skips files that are already identical.

> **Tip:** if `git pull` refuses with *local changes*, or `deploy.sh` reports
> files that will not apply, use option A — `install.sh` re-syncs
> `/opt/nazman` to the branch and discards any drift.

> **Note:** `deploy.sh` does not install Python packages. If
> `requirements.txt` changed, reinstall dependencies first:
>
> ```bash
> sudo /opt/nazman/venv/bin/pip install -r /opt/nazman/requirements.txt
> ```

## Raspberry Pi OS

`prepare.sh` enables the Debian `contrib` repository (ZFS lives there) and
builds the ZFS kernel module with DKMS — a few minutes on first install. If the
DKMS build fails against a newly-shipped kernel, see
[Troubleshooting](troubleshooting) for the backports workaround.

## Second-machine backup service

A lower-power machine (e.g. a Raspberry Pi) can run NAZMan purely as a **backup
service**: install it, declare the external backup disks on the **Backup** page,
and it receives streams from your main NAS. No pools are required on the
service itself.

Next: [Troubleshooting](troubleshooting)