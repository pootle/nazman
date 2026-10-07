# Set Up Telegram Alerts

NAZMan can message your phone when something goes wrong: an unhealthy pool, a
scrub or resilver with errors, a pool filling up, a scheduled task that failed,
or a backup that needs a disk. Alerts are delivered through a free Telegram
bot.

> Note: Alerts need outbound internet access to `api.telegram.org`. There is no
> cost involved.

## Requirements

- A free [Telegram](https://telegram.org) account on your phone.
- Outbound HTTPS access from the NAS to `api.telegram.org`.

## 1. Create a bot

1. Open Telegram on your phone and search for **@BotFather**.
2. Start a chat and send `/newbot`.
3. Give the bot a public name (e.g. `NAZMan Alerts`) and a username ending in
   `bot` (e.g. `nazman_alerts_bot`).
4. BotFather replies with an **HTTP API token** that looks like
   `123456789:AA...`. Keep it private - it grants anyone who holds it the
   ability to post as your bot.

## 2. Find your chat id

1. Open a chat with your new bot (search for its username, tap *Start*).
2. In a browser, visit:

   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```

   replacing `<TOKEN>` with your bot token. Press **Start** again in the bot
   chat if the list is empty.
3. The JSON contains a `chat` object; your id is the numeric `"id"` value
   (e.g. `123456789`). It may be a negative number for groups.

> Tip: You can also skip this step - the *Send test* button below will tell you
> if the chat id you entered is wrong.

## 3. Configure NAZMan

1. Open the web UI and go to **Settings** -> **Telegram Alerts**.
2. Paste the bot token and your chat id.
3. Leave **Enable alerts** unticked for now and click **Send test**.
4. You should get a "NAZMan test alert" on your phone. If not, the error shown
   (e.g. *chat not found*) will point you at the fix.
5. Tick **Enable alerts** and click **Save**.

## What triggers an alert

**Pools**

- A pool enters a state other than `ONLINE` (e.g. `DEGRADED`, `FAULTED`,
  `OFFLINE`).
- A scrub or resilver finishes with read/write/checksum errors.
- A pool crosses the configured usage threshold (default 90%). Each further
  5% step re-alerts.

**Scheduled tasks**

- Any scheduled task (scrub, snapshot, health check) fails.

**Backups** — a backup group reports its own outcomes, independently of the
scheduler:

- **Session finished** — sent after every session with how many datasets
  completed and where the data was written. `success` reports as an info
  message, `partial` (some datasets failed) as a warning.
- **Session failed** — every dataset in the group errored on the current set.
- **Needs a disk** — the active set is full, or its disk is offline. Backups
  pause until you plug in a disk or move the group to another set.
- **Incremental promoted to full** — a run had no base snapshot to build on,
  so NAZMan sent a full instead. This is normal after a set is reset, but it
  means the run transferred much more data than expected.

These are shown on the **Backup Sessions** card as well; the alert is the
heads-up that something needs your attention.

The cooldown setting (default 60 minutes) stops the same event from
re-notifying you on every poll - a persistently broken pool is reported once
per window, not every minute. Backup session-finished and promotion notices
bypass that cooldown, since each one is a distinct event worth seeing; the
failure and needs-a-disk notices are subject to it, so a group that stays
broken does not message you every few minutes.

## Where your token is stored

The token is written to `/etc/nazman/nazman.conf` (readable only by root) and
is masked in the Settings UI. When updating alerts, the token field is only
written back if you actually type a new one.

## Troubleshooting

- **"chat not found" on test** - the bot has never been started, or the chat id
  is wrong. Open the bot in Telegram, press Start, and re-verify the id.
- **401 from api.telegram.org** - the token is invalid or truncated. Ask
  BotFather for a fresh one and re-enter it.
- **Alert settings save but no messages arrive** - confirm *Enable alerts* is
  checked after saving and that the NAS has outbound network access.