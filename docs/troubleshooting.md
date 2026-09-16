# Troubleshooting

A quick reference for the most common "something isn't right" situations. Start
any investigation on the **Command Log** page — it records every command NAZMan
runs with its status (`success`, `failed`, `timeout`, `error`), return code and
stderr, and you can filter to **show reads** to make the write/system noise
disappear.

## Backup disk problems

| Symptom | Cause & fix |
|---|---|
| **Backup disk shows Not connected** | Device not visible. Try the **Wake / Replug** action (software re-enumerate). If the USB enclosure is powered but asleep, power-cycle it. The disk must reach **Mounted** before backups/restores. |
| **Filesystem changed** | A device is present but has a different filesystem UUID than declared. You have the wrong disk plugged in, or the disk was re-formatted/re-partitioned underneath NAZMan. **Scan** to re-probe; if it is genuinely the old disk with new identity, re-declare it (the old streams on it are still readable via **Restore from Backup Disk**). |
| **Full** | No free space. Thin out old stream files on the disk (or the pool hosting them) or move to a larger disk for the next full. |
| **Backup fails** | Check the failing run's row on the **Backup Runs** card (the error is in the status tooltip) and the Command Log for the `zfs send`/`zfs receive` stderr. Common: disk filled mid-stream (→ **Full**), or the dataset gained a snapshot conflict (destroy the stray `backup-*` snapshot). |

## Pool / disk issues

| Symptom | Cause & fix |
|---|---|
| **Pool **DEGRADED** on Dashboard / Pools** | A device in the vdev is missing or failed. Data is intact on mirror/RAIDZ. See [Handle a Disk Failure](disk-failure): find the device in **Details**, fit a replacement, `zpool replace`, let it resilver. |
| **Pool **FAULTED** / **UNAVAIL**** | A device is unusable (FAULTED) or the pool's vdevs are offline (UNAVAIL). No redundancy left — restore from backup to a fresh pool ([Recover a Complete System](recover-system)). |
| **Disk shows **not present** then **removed**** | Linux re-numbered devices or the disk is gone. NAZMan tracks by stable identity, so the pool vdevs are unaffected. **Resurrect** if it returns. |
| **SMART **Health **failing**** | Back the data up now, then plan a replacement. |
| **Scrub shows errors** | `zpool status` after a scrub reports checksum errors. Replace the offending device (likely the disk with errors in the scrub column) before it degrades further. |

## Sharing problems

| Symptom | Cause & fix |
|---|---|
| **NFS/SMB page shows "not installed"** | Install the server from the page's **Install NFS server** / **Install Samba** button; Create/Refresh stays disabled until it completes. |
| **NFS export shows **Paused**** | Resume it with the play icon on the row. |
| **Client can't mount NFS share** | Confirm the **Client Specification** covers the client (host or CIDR) and the client's firewall allows NFSv4. The `/` root export does not imply children — tick **No Hide (NFSv4)** to export a dataset that has child datasets. |
| **SMB file permissions look odd** | Expected: all access maps to the shared `nfsanon` user (UID/GID 65533) for NFS/SMB consistency. There is no per-user permission model. |

## App / login issues

| Symptom | Cause & fix |
|---|---|
| **Login says Invalid password** | Empty-password fresh installs accept anything until you set `AUTH_PASSWORD_HASH` in `/etc/nazman/nazman.conf`. To harden: `set_setting AUTH_PASSWORD_HASH '<bcrypt-hash>'` (or edit the conf) then restart the service (`sudo systemctl restart nazman`). |
| **Page stuck "Loading..."** | Refresh (top-right). If it persists, the API failed — check the **Command Log** and the service (`sudo systemctl status nazman`, `journalctl -u nazman -f`). |
| **Changes not reflected on pages** | ZFS is the source of truth; NAZMan re-reads it. Refresh the page or use the section's **Refresh** button. |
| **Service down after deploy** | Redeploy restarts the service; a Python error will put it in a failed state. Re-run `sudo ./deploy.sh` after fixing, or check `journalctl -u nazman` for the traceback back to the last working version. |

## If all else fails

1. Reproduce with the actual command from the **Command Log**.
2. Restore the dataset from the last good backup run — see
   [Recover a Dataset](recover-dataset).
3. Full disaster (pool lost / server gone): [Recover a Complete
   System](recover-system).