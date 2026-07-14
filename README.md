# supervisoragent
tinkering with my Mac mini 

## Network Guardian

The existing Telegram bot includes a read-only home-network guardian. It observes
the macOS ARP cache, default gateway, DNS, internet reachability, and optional TCP
service targets. It never scans the subnet, changes router or mesh configuration,
blocks clients, changes DNS, or executes an intrusive action.

Telegram commands:

- `/net_status` — current health and last observation
- `/net_devices` — devices seen in the local ARP cache
- `/net_scan` — run a read-only observation now
- `/net_alerts` — recent detected changes
- `/net_summary` — generate the daily report now
- `/net_speed` — run and record an ad-hoc internet quality test
- `/net_actions` — actions awaiting explicit approval
- `/net_approve <id>` and `/net_reject <id>` — decide a proposal

The first observation establishes a baseline without sending new-device alerts.
Later new devices alert immediately. Health failures must occur twice consecutively
before alerting. A summary is sent once per Singapore calendar day on the first
scan at or after 09:00; the default observation interval is five minutes.

At 08:30 Singapore time the guardian runs one internet performance test. It uses
Ookla's CLI when that tool is already installed and otherwise uses macOS's built-in
`/usr/bin/networkQuality`. A failed test is retried once at 08:40. Download,
upload, base latency, available quality metrics, and a seven-day comparison are
included in the 09:00 summary. A test failure never prevents the summary.

Optional environment settings in `ai_news_push.env`:

```text
NETWORK_GUARDIAN_SCAN_SECONDS=300
NETWORK_GUARDIAN_TARGETS=NAS=192.168.1.10:443,HomeAssistant=192.168.1.20:8123
```

State is stored in the ignored `network_guardian_state.json` file. Router-specific
TP-Link/Aginet modification support is intentionally absent from this release.
