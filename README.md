# MacMini Status Bot

Check a Linux Mac mini from Telegram with one **Status Check** button. Receive
hourly health summaries and alerts when monitored conditions change.

Built for an older machine: Python's standard library, Linux sensor files, and
Telegram long polling. No AI runtime, database, web server, or pip dependencies.
The bot observes the machine; the existing thermal controller manages cooling
and shutdown.

## What you receive

| Measurement | Reported value |
| --- | --- |
| CPU and GPU | Current temperatures; sampled averages and peaks in hourly reports |
| PSU and memory | Available temperature readings |
| Additional SMC channels | `TN1F` and `TN1S`, kept under their raw sensor names |
| Fan | Current RPM; sampled average and peak |
| CPU activity | Host-wide usage across the measurement interval |
| Memory | Available RAM and occupied swap |
| Thermal controller | Service state and interpreted cooling mode |
| Power consumption | Explicitly unavailable until supported metering is implemented |

**Watts and kWh are not estimated from CPU usage or PSU temperature.** The current
implementation has no wall-power meter integration. The physical locations of
`TN1F` and `TN1S` are uncertain, so reports do not invent component mappings.

Each message includes an inline **Status Check** button. Only the configured
Telegram user in the configured private chat can request a report. Typed messages,
including `/status`, are ignored. Telegram still displays its text input box.
There are no remote shell, Codex, fan-control, restart, or shutdown commands.

## Requirements

- Linux with Python 3 and a systemd user manager.
- Thermal drivers exposing compatible readings through `/sys/class/hwmon`.
  This implementation targets `applesmc`, `coretemp`, and `nouveau`.
- The companion `macmini-thermal-guard.service` for controller status. A missing
  service is reported as inactive or unreadable.
- Outbound HTTPS access to Telegram; no inbound port or public URL is required.
- A dedicated Telegram bot token and your numeric user/private-chat IDs.

Run these commands from the repository directory. Node and pnpm are only needed
for the `pnpm run build` verification wrapper, not to operate the bot.

## Set up Telegram

1. Open Telegram's verified [@BotFather](https://t.me/BotFather), create a bot with
   `/newbot`, and keep the issued token private.
2. Open your new bot's private chat and press **Start**. This allows the bot to
   send you messages; it does not trigger a command in this application.
3. Obtain your numeric Telegram user and private-chat IDs. Usernames and phone
   numbers are not accepted. In a direct bot conversation these IDs are normally
   the same.

If you need to discover your IDs, the following optional helper reads the Start
message **before the monitor is running**. It prompts for the token without
putting it in shell history. Only run this against your dedicated bot, and select
the IDs belonging to the private chat you just opened.

```sh
python3 - <<'PY'
import getpass
from bot import api
try:
    token = getpass.getpass('Bot token (hidden): ')
    updates = api(token, 'getUpdates', timeout=0, allowed_updates=['message'])
    for update in updates:
        message = update.get('message', {})
        chat = message.get('chat', {})
        sender = message.get('from', {})
        if chat.get('type') == 'private':
            print('User ID:', sender.get('id'), 'Chat ID:', chat.get('id'))
except Exception:
    print('Could not retrieve IDs. Check the token, connection, and existing webhook.')
PY
```

If no IDs appear, send another message to the bot and rerun the helper. Never run
two polling clients against the same bot. An existing webhook must be removed
before this monitor can start; the monitor deliberately does not remove it for you.

Save your credentials using the interactive setup:

```sh
python3 bot.py setup
```

The token is entered with hidden input. Configuration is stored outside the repo
at `~/.config/macmini-status-bot/config.json` with permissions `600`. Startup
rejects configuration readable by other users. No Telegram message is sent by setup.

## Install and start

First check the local report without contacting Telegram:

```sh
python3 bot.py status
```

Then install the application and user service:

```sh
install -d ~/.local/lib/macmini-status-bot ~/.config/systemd/user
install -d -m 700 ~/.local/state/macmini-status-bot
install -m 644 bot.py ~/.local/lib/macmini-status-bot/bot.py
install -m 644 macmini-status-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now macmini-status-bot.service
```

On first activation, the bot sends a report with the button. On later restarts,
its saved report schedule is retained, so a new message may not appear immediately;
the button on a previous message still works.

To run after boot without an interactive login, enable lingering once for the
account that owns the service:

```sh
sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger
```

An enabled service is **not necessarily running**. Without its private configuration
file, systemd skips startup. After configuring an already enabled installation, run
`systemctl --user start macmini-status-bot.service`.

## Reports and alerts

| Behavior | Current implementation |
| --- | --- |
| Sampling | Approximately every 30 seconds or longer, depending on polling and network delays |
| Scheduled reports | Hourly, based on the last successful scheduled send |
| History | Latest 120 periodic samples in memory; reset when the process restarts |
| Button response | Fresh snapshot, limited to one response per 10 seconds |
| Change alerts | Compared with the last reported conditions; five-minute cooldown after a report or alert |
| Recovery | Reported when all previously reported alert conditions have cleared |
| Delivery failure | Generic local log message, then a 30-second retry delay |

Hourly averages and peaks cover the retained samples, which can span more than an
hour. They are **sampled peaks**, not a guarantee that brief spikes were captured.
CPU usage includes all host processes and is measured since the preceding sample;
a button check also advances that measurement interval. Occupied swap alone does
not indicate current memory pressure.

Warnings currently trigger at:

| Condition | Warning |
| --- | --- |
| CPU | At least 65°C |
| GPU | At least 70°C |
| PSU | At least 62°C |
| `TN1F` or `TN1S` | At least 70°C |
| Available RAM | Less than 0.5 GiB |
| Required temperature or fan reading | Missing or unreadable |
| Thermal controller | Inactive, unreadable, or outside the recognized normal temperature-curve mode |

These temperature levels are early warnings relative to the companion controller's
configuration, **not manufacturer damage limits**. They are fixed in `bot.py` and
are not automatically synchronized with changes to the thermal guard.

The bot reports the controller's cooling status; it does not independently read or
clear SMC fault keys. An unchanged warning is included in hourly reports rather
than generating a new alert on every sample. Cooldown and network delays mean
notifications must not be relied upon for emergency protection.

## Resource use and privacy

The systemd service applies these limits:

| Setting | Limit |
| --- | --- |
| CPU | 5% of one CPU core, not 5% of total machine capacity |
| Memory | 96 MiB hard cap |
| Tasks | 8 |

Actual use depends on the system and network. The process normally waits on
Telegram long polling between short readings. If it exceeds the memory cap it
may be killed; systemd restarts failed runs after 30 seconds.

The service uses an unprivileged account, `NoNewPrivileges`, a private temporary
directory, and read-only system/home mounts with a writable state directory.
It requires no sudo access. The sandbox permits normal private temporary storage;
it does not make the entire process filesystem immutable.

| Local path | Contents |
| --- | --- |
| `~/.config/macmini-status-bot/config.json` | Token and authorized user/chat IDs; private |
| `~/.local/state/macmini-status-bot/state.json` | Telegram offset, report timestamps, last reported alerts; private |
| `~/.local/lib/macmini-status-bot/bot.py` | Installed application copy |
| `~/.config/systemd/user/macmini-status-bot.service` | Installed service unit |

Tokens, raw incoming messages, destination IDs, hostnames, and network addresses
are excluded from application logs and status reports. Selected health readings
and local timestamps are sent to Telegram. Keep credentials and live diagnostic
output outside Git; `.gitignore` is only a backup against accidental additions.
Git author identity and signatures are retained.

A powered-off or disconnected Mac cannot notify you. Detecting missed check-ins
requires an external monitor. Delivery is best effort: a crash between sending
and saving state can duplicate a message.

## Maintain and troubleshoot

```sh
systemctl --user status macmini-status-bot.service
journalctl --user -u macmini-status-bot.service -n 30 --no-pager
systemctl --user show macmini-status-bot.service \
  -p ActiveState -p CPUQuotaPerSecUSec -p MemoryCurrent -p MemoryMax
```

| Symptom | Check |
| --- | --- |
| Enabled but inactive | Run setup; verify the config exists, then start the service |
| No first message | Press Start in the bot's private chat; verify token and both IDs |
| Button does nothing | Use the authorized account and private chat; allow the ten-second cooldown |
| Typed commands do nothing | Expected: only the inline button is implemented |
| Startup failure | Config permissions/values, outbound HTTPS, or an existing webhook |
| Repeated generic retry messages | Connectivity, token validity, chat access, or another polling client |
| Missing temperatures | Check thermal drivers and compare with `python3 bot.py status` |
| Power consumption unavailable | Expected: a supported meter integration has not been implemented |

Error logs intentionally omit raw Telegram responses and exception URLs to avoid
leaking the bot token. Re-run `python3 bot.py setup` to replace credentials, then
restart the service. Do not post the private configuration in an issue.

After updating the repository, deploy the changed files explicitly:

```sh
install -m 644 bot.py ~/.local/lib/macmini-status-bot/bot.py
install -m 644 macmini-status-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user restart macmini-status-bot.service
```

Stop automatic reporting with:

```sh
systemctl --user disable --now macmini-status-bot.service
```

This stops the Telegram monitor only. It does not stop the thermal guard.

## Development

```sh
pnpm run build
```

This compiles the Python files and runs the unit tests, including private-chat
authorization, ignored text messages, saved update offsets, and failed delivery.
Tests use synthetic data and mocked Telegram calls; no token is required and no
real messages are sent. Python-only equivalent:

```sh
python3 -m py_compile bot.py test_bot.py
python3 -m unittest -v
```

Protocol reference: [Telegram Bot API](https://core.telegram.org/bots/api),
particularly `getUpdates`, `CallbackQuery`, `answerCallbackQuery`, and `sendMessage`.
