# Server deployment

The app runs on the droplet as user `trader` from `/home/trader/TeleTrader`,
supervised by systemd. These are the unit files it uses.

| Unit | What it does |
| --- | --- |
| `teletrader.service` | the long-running listener (`main.py`); `Restart=on-failure` |
| `teletrader-eod.service` | one-shot end-of-day P&L summary (`eod_report.py`) |
| `teletrader-eod.timer` | fires the above at 15:35 IST, Mon-Fri |

## Installing the end-of-day timer

```bash
sudo cp deploy/teletrader-eod.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now teletrader-eod.timer
systemctl list-timers teletrader-eod.timer   # confirm the next run
```

Run it by hand at any time (it is idempotent — scoring a day again just
recomputes it):

```bash
sudo systemctl start teletrader-eod.service   # send the summary now
# or, as trader, without sending:
uv run python eod_report.py --print
uv run python eod_report.py --date 2026-09-28 --print
```

## A note on the timer's timezone

The server runs UTC, so the schedule carries its timezone **inline on
`OnCalendar`** (`Mon..Fri 15:35 Asia/Kolkata`). There is no `Timezone=` key for
timers, and systemd ignores unknown keys with only a log line — an earlier
version of this file used one and was silently scheduled for 15:35 **UTC**,
i.e. 21:05 IST. Always confirm after changing it:

```bash
systemd-analyze calendar "Mon..Fri 15:35 Asia/Kolkata"   # -> 10:05 UTC
systemctl list-timers teletrader-eod.timer
```

## A note on restarts

`teletrader.service` sets `StartLimitBurst=5` / `StartLimitIntervalSec=300`. A
few rapid failures (e.g. a bad `.env`) trip the limiter, and systemd then refuses
even a corrected start with *"Start request repeated too quickly"*. Clear it
first:

```bash
sudo systemctl reset-failed teletrader
sudo systemctl start teletrader
```
