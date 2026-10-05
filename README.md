# Mac mini Status Bot

Small Python standard-library Telegram monitor. One authorized private-chat
**Status Check** inline button; typed messages and all other commands are ignored.
No AI, remote shell, fan writes, shutdown commands, or inbound server.

Every roughly 30 seconds it reads temperatures, fan RPM, RAM/swap and CPU usage.
Hourly reports include sampled averages/peaks from up to 120 recent samples.
Temperature warnings, missing readings, inactive thermal control, low available
RAM and protection-mode changes trigger alerts, with a five-minute alert cooldown.
Recovery is reported after previously reported conditions clear. Sampling and
alerts can be delayed by network failures; this is not a thermal safety controller.
The existing thermal guard remains responsible for cooling and shutdown.

CPU/GPU/PSU warning levels are 65/70/62 C, chosen relative to the companion
thermal guard's current shutdown configuration, not manufacturer damage limits.
TN1F/TN1S remain raw channel labels; their physical locations are uncertain.
No supported wall-power meter is available: reports explicitly say unavailable.
CPU activity and PSU temperature are not converted into invented watts or kWh.

## Setup

Requires Python 3, systemd and the Linux thermal drivers; no pip dependencies.
Create a dedicated bot through Telegram's verified @BotFather, and open its private
chat and press Start. Obtain your numeric Telegram user/chat IDs. Do not reuse a
bot consumed by another polling client or webhook.

```sh
python3 bot.py setup
install -d ~/.local/lib/macmini-status-bot ~/.local/state/macmini-status-bot ~/.config/systemd/user
install -m 644 bot.py ~/.local/lib/macmini-status-bot/bot.py
install -m 644 macmini-status-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now macmini-status-bot.service
```

Setup prompts for the token with hidden input. Credentials live in
`~/.config/macmini-status-bot/config.json` (mode 600), outside the repo.
The service initially sends a report containing the button. User and chat IDs
must both match, the chat must be private, and only `status` callback data works.
Button reports have a ten-second cooldown. Telegram still displays a text box.
The bot requests only callback updates and advances its saved update offset.
Delivery is best effort; a crash at the send/save boundary can duplicate a message.
No raw incoming messages, credentials, destination IDs, hostnames or network
addresses are logged or included in reports. Report contents go to Telegram.
Never commit private configuration or live monitoring output; Git authorship is
retained. Long polling needs outbound HTTPS only. The unit has a 5% one-core CPU
budget, 96 MiB memory cap and read-only filesystem except its private state.
For boot operation without login, enable user lingering through the system's
administrator. Service activation is skipped until its configuration exists.

```sh
python3 bot.py status     # local read-only check; no Telegram request
pnpm run build           # syntax checks and unit tests
systemctl --user status macmini-status-bot
```

A dead or disconnected machine cannot send notifications. External heartbeat
monitoring is required for outage detection. CPU usage is host-wide across the
sample interval; history resets on restart and does not capture every short peak.

References: [Telegram Bot API](https://core.telegram.org/bots/api), especially
getUpdates, CallbackQuery, answerCallbackQuery and sendMessage.
