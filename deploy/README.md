# Server deployment

The app runs on the droplet as user `trader` from `/home/trader/TeleTrader`,
supervised by systemd. These are the unit files it uses.

| Unit | What it does |
| --- | --- |
| `teletrader.service` | the long-running listener (`main.py`); `Restart=on-failure` |
| `teletrader-eod.service` | one-shot end-of-day P&L summary (`eod_report.py`) |
| `teletrader-eod.timer` | fires the above at 15:35 IST, Mon-Fri |
| `teletrader-token-check.service` | one-shot pre-market token check (`check_token.py`) |
| `teletrader-token-check.timer` | fires the above at 08:45 IST, Mon-Fri |

## Installing the timers

```bash
sudo cp deploy/teletrader-eod.{service,timer} /etc/systemd/system/
sudo cp deploy/teletrader-token-check.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now teletrader-eod.timer teletrader-token-check.timer
systemctl list-timers 'teletrader-*'         # confirm the next runs
```

The token check alerts **only on failure** — a warning that arrives every
morning is one nobody reads. It reports a dead token by exiting 1, so the unit
sets `SuccessExitStatus=0 1`: that is information, not a service fault.

Run it by hand at any time (it is idempotent — scoring a day again just
recomputes it):

```bash
sudo systemctl start teletrader-token-check.service   # check the token now
sudo systemctl start teletrader-eod.service           # send the summary now
# or, as trader, without sending:
uv run python eod_report.py --print
uv run python eod_report.py --date 2026-09-28 --print
uv run python eod_report.py --since 2026-09-28 --date 2026-09-30 --print  # catch up
uv run python check_token.py --print
```

## The daily FYERS token

`FYERS_ACCESS_TOKEN` expires **daily**, at a fixed cutoff. Run the login **in the
morning before 09:15 IST** — a token minted overnight (e.g. 03:30) expires at
that cutoff and is already dead when the market opens:

```bash
ssh -t root@64.227.151.139 "su - trader -c 'cd ~/TeleTrader && uv run python fyers_login.py --manual'"
sudo systemctl restart teletrader
```

Without a live token the shadow alerts still arrive in full (symbol, expiry,
quantity, protective exits) — only the funds check and the end-of-day P&L need
it. Both now name the reason when it fails, e.g.
`Funds check unavailable (Could not authenticate the user).`

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
