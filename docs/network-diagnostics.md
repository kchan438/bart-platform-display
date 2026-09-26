# Persistent network diagnostics

## Scope and implementation plan

The office Pi intermittently loses connectivity and recovers after a manual
reconnect. No office failure trace is available yet; this change collects
future evidence and does not claim to fix the disconnects.

1. Preserve evidence: automatically open a rotating JSONL log outside the Git
   checkout before starting Wi-Fi and BART workers. Include UTC, monotonic time,
   boot ID, and a fresh process session ID. Keep at most four 4 MiB log files.
2. Instrument existing boundaries: record Wi-Fi operation starts/results,
   supervisor state/reason/retry changes, and observed device state/IP presence.
   Record each BART request outcome, duration, error class and HTTP failure code.
3. Sample radio health on a separate background worker about once a minute:
   link signal/frequency, power-saving state, gateway/DNS presence, and Pi
   throttling bits. All commands are read-only and have three-second timeouts.
4. Export and validate: provide an offline ZIP export, test state transitions,
   persistence, rotation, omitted secrets, missing tools, and write failures;
   run the existing suite before publishing.

Acceptance: logging starts with the deployed app without sudo or a service-file
change; old records survive a process restart; failures do not break the display;
export includes only diagnostic logs; existing Wi-Fi behavior remains intact.

## Install at the office

After this change is merged into `main`:

1. Reconnect Wi-Fi as usual.
2. Open **Settings → System → Update App**.
3. When the update finishes, press **Restart Display to Apply** and confirm.
4. Leave the display running at the office. If it drops again, note the approximate
   time and reconnect as usual. Manual attempts and their results are recorded.

New logging cannot capture earlier incidents. An **Already up to date** result
before the merge does not mean this feature is installed.

The app automatically writes to the service user's home directory:

```text
/home/kevinchan/.local/state/bart-platform-display/diagnostics/network.jsonl
```

Older files are `network.jsonl.1`, `.2`, and `.3`. The combined limit is about
16 MiB, not a guaranteed number of days; heavy activity replaces old logs sooner.
Export soon after bringing the Pi home. Ordinary restarts preserve logs, though
abrupt power loss can lose the latest filesystem-buffered writes. No logging
runs while the display service is stopped, or in desktop development mode.

## Retrieve at home

In a terminal on the Pi, as `kevinchan` (do not run the export with sudo):

```bash
cd ~/bart-platform-display
sudo systemctl stop bart-platform-display
venv/bin/python -m bartdisplay.network_diagnostics --export "$HOME/bart-network-diagnostics.zip"
sudo systemctl start bart-platform-display
```

Stopping the service briefly keeps log rotation from racing the export. Restart
it even if export fails. An existing ZIP is never overwritten: choose a new
filename for later exports. The ZIP contains only the structured diagnostic logs
and a reading guide, not `config.json`, saved Wi-Fi profiles, or raw journals.
Bring that ZIP back for investigation. A missing-log error means the updated app
has not recorded under this user, or the log directory was unavailable; inspect
`journalctl -u bart-platform-display` for `[network-diagnostics]` warnings.

For an optional check while the app is running:

```bash
tail -n 5 ~/.local/state/bart-platform-display/diagnostics/network.jsonl
```

Expect `session_start`, Wi-Fi observations, `radio_health`, and `bart_request`
records. Tool errors are explicit (`unavailable`, `failed`, `timeout`); they are
not healthy measurements. Missing `iw` or `vcgencmd` does not stop other logging.

## What the evidence can distinguish

- `wifi_observation`: NetworkManager numeric device state and reason, query
  success and IPv4 address presence. Changes are recorded on the existing
  supervisor polls, with a heartbeat at least every 60 seconds when polled.
- `wifi_supervisor`: grace period, reconnect attempts, authentication attention,
  success, and exhausted recovery, using the app's existing reason codes.
- `wifi_operation_start/end`: manual scan/connect/disconnect/forget attempts and
  stable result codes, including failures.
- `radio_health`: signal in dBm, frequency in MHz, power saving, configured IPv4
  gateway/DNS presence, and raw numeric Raspberry Pi throttling flags. Gateway
  and DNS configuration presence does not establish reachability. Multiple Wi-Fi
  adapters are not supported by the sampler; it reads the first reported adapter.
- `bart_request`: distinguishes a working BART request from timeouts, connection,
  HTTP, and parsing errors. ConnectionError alone does not isolate DNS vs routing.

Network names, BSSIDs, profile UUIDs, IP addresses, credentials, request URLs,
response bodies, and exception messages are not written to these files. Boot and
session IDs are local correlation IDs. Compare monotonic times within one boot:
Pi wall clocks may jump when internet time synchronization becomes available.

The recorder uses the existing three-second supervisor polling, so a brief event
between polls can be missed, particularly during a blocking recovery attempt.
Radio sampling is about once per minute plus command runtime. This change does
not capture kernel/driver messages or every NetworkManager event, change power
saving, trigger scans, or add new reconnect attempts. If we need deeper evidence,
install the optional persistent journald configuration in the README before the
next office run. Raw system journals can contain network identifiers and should
be reviewed before sharing.

## Validation boundary

Desktop tests simulate disconnect/reconnect observations, request failures and
hardware-command output. They verify recording, export, retention, and unchanged
application tests. They do not prove Linux permissions, installed command
availability, Raspberry Pi SD-card behavior, office Wi-Fi reliability, or the
cause of the real disconnect. Validate the records on the Pi after updating.
