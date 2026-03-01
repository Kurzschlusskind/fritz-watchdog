# FritzWatchdog

Automated upstream connectivity monitor and recovery daemon for **AVM FRITZ!Box** gateways. Runs continuously inside a Docker container, probing internet connectivity via ICMP and DNS, and dispatching TR-064 SOAP calls to recover the gateway when a fault is detected.

---

## Table of Contents

1. [Project Description](#project-description)
2. [System Requirements](#system-requirements)
3. [Quick Start](#quick-start)
4. [Configuration Reference](#configuration-reference)
5. [Log Format](#log-format)
6. [Recovery Flow](#recovery-flow)
7. [Scheduled Maintenance](#scheduled-maintenance)
8. [Architecture Overview](#architecture-overview)
9. [License](#license)

---

## Project Description

FritzWatchdog is a lightweight Python daemon that monitors upstream internet connectivity from behind an AVM FRITZ!Box gateway. When a connectivity fault is confirmed (configurable threshold of consecutive probe failures), it automatically attempts recovery using the FRITZ!Box TR-064 SOAP API:

- **Phase 1 — WAN Reconnect:** Issues a `ForceTermination` call to force PPPoE re-negotiation without a full reboot.
- **Phase 2 — Full Reboot:** If Phase 1 fails, issues a `Reboot` call for a complete gateway power cycle.

A configurable weekly maintenance reboot is also supported.

---

## System Requirements

| Component | Requirement |
|-----------|-------------|
| Hardware | Any Linux host on the same LAN as the FRITZ!Box (e.g., Raspberry Pi) |
| OS | Linux (aarch64, x86_64, or any Docker-capable platform) |
| Docker | Docker Engine >= 20.10 with Compose v2 |
| FRITZ!Box | Any model with TR-064 enabled (firmware >= 6.x recommended) |
| TR-064 User | A FRITZ!Box user account with `FRITZ!Box Settings` permission |

### Enable TR-064 on the FRITZ!Box

1. Open the FRITZ!Box web UI (`http://fritz.box` or `http://192.168.178.1`).
2. Navigate to **Home Network → Network → Network Settings**.
3. Enable **Allow access for applications** (TR-064).
4. Create a dedicated user account under **System → FRITZ!Box Users** with the `FRITZ!Box Settings` permission.

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/Kurzschlusskind/fritz-watchdog.git
cd fritz-watchdog
```

### 2. Configure the environment

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:

```env
FRITZ_ADDRESS=192.168.178.1
FRITZ_USER=watchdog
FRITZ_PASSWORD=your_actual_password
```

### 3. Create the log directory

```bash
mkdir -p logs
```

### 4. Build and start the container

```bash
docker compose up -d --build
```

### 5. Follow the logs

```bash
docker compose logs -f
# or directly from the log file:
tail -f logs/watchdog.log
```

### 6. Stop the daemon

```bash
docker compose down
```

---

## Configuration Reference

All configuration is provided via environment variables. Copy `.env.example` to `.env` and edit as needed.

### FRITZ!Box Connection

| Variable | Default | Description |
|----------|---------|-------------|
| `FRITZ_ADDRESS` | `192.168.178.1` | IP address of the FRITZ!Box gateway |
| `FRITZ_PORT` | `49000` | TR-064 SOAP port |
| `FRITZ_USER` | `watchdog` | TR-064 user account |
| `FRITZ_PASSWORD` | *(required)* | TR-064 password — **no default, must be set** |

### Probe Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `PROBE_INTERVAL` | `60` | Seconds between probe cycles |
| `FAIL_THRESHOLD` | `3` | Consecutive failures before triggering recovery |
| `ICMP_TARGETS` | `8.8.8.8,1.1.1.1` | Comma-separated list of ICMP ping targets |
| `DNS_TARGET` | `9.9.9.9` | DNS server for DNS probe |
| `DNS_QUERY` | `google.com` | Hostname to resolve in the DNS probe |
| `DNS_PORT` | `53` | UDP port for DNS probe |

### Recovery Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `RECONNECT_HOLD` | `120` | Seconds to wait after Phase 1 (ForceTermination) before re-probing |
| `REBOOT_HOLD` | `180` | Seconds to wait after Phase 2 (Reboot) before re-probing |
| `ACTION_COOLDOWN` | `600` | Minimum seconds between consecutive recovery actions |
| `MAX_ESCALATIONS` | `5` | Maximum Phase 2 escalations allowed per 24-hour window |

### Scheduled Maintenance

| Variable | Default | Description |
|----------|---------|-------------|
| `SCHEDULED_REBOOT` | `TUE/04:00` | Weekly maintenance reboot schedule — format: `DAY/HH:MM` |

Supported day names: `MON`, `TUE`, `WED`, `THU`, `FRI`, `SAT`, `SUN`.

### Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_DIR` | `/var/log/fritzwatchdog` | Directory for persistent log files |
| `NOMINAL_INTERVAL` | `600` | Seconds between periodic NOMINAL status entries (healthy state) |

---

## Log Format

All log entries follow this format:

```
[YYYY-MM-DD HH:MM:SS] [TAG]          Message
```

### Tags

| Tag | Meaning |
|-----|---------|
| `INIT` | Daemon startup and configuration |
| `HANDSHAKE` | TR-064 session establishment |
| `TELEMETRY` | WAN status snapshot (IP, uptime, link rate) |
| `NOMINAL` | Periodic healthy-state confirmation |
| `DEGRADED` | Individual probe failure |
| `CRITICAL` | Fault threshold reached |
| `RECOVERY` | Phase 1 recovery action |
| `ESCALATION` | Phase 2 escalation action |
| `PROBE` | Post-recovery probe results |
| `RESOLVED` | Connectivity restored |
| `FAILED` | Post-recovery probes failed |
| `MAINTENANCE` | Scheduled maintenance reboot |
| `SHUTDOWN` | Graceful shutdown |
| `WARN` | Non-critical warning |
| `ERROR` | Recoverable error |

### Example — Normal startup and healthy monitoring

```
[2026-03-01 14:32:01] [INIT]          Watchdog daemon initialized — PID 4821
[2026-03-01 14:32:01] [INIT]          Target gateway: 192.168.178.1 (AVM FRITZ!Box)
[2026-03-01 14:32:01] [INIT]          TR-064 SOAP endpoint: http://192.168.178.1:49000 — Auth: DIGEST-MD5
[2026-03-01 14:32:02] [HANDSHAKE]     TR-064 session established — ServiceType: urn:dslforum-org:service:WANIPConnection:1
[2026-03-01 14:32:02] [TELEMETRY]     WAN uptime: 847291s | External IPv4: 84.132.xx.xx | Link rate: 262144 kbps down / 46080 kbps up
[2026-03-01 14:32:02] [INIT]          Monitoring loop started — probe interval: 60s, fail threshold: 3
[2026-03-01 14:42:02] [NOMINAL]       Upstream connectivity healthy — ICMP/8.8.8.8: ✓ 4.1ms | ICMP/1.1.1.1: ✓ 3.8ms | DNS/9.9.9.9: ✓ 12.0ms
```

### Example — Phase 1 recovery

```
[2026-03-01 14:35:02] [DEGRADED]      ICMP probe timeout — dst=8.8.8.8 — no reply within 5000ms (1/3)
[2026-03-01 14:36:02] [DEGRADED]      ICMP probe timeout — dst=8.8.8.8 — no reply within 5000ms (2/3)
[2026-03-01 14:37:03] [DEGRADED]      ICMP probe timeout — dst=8.8.8.8 — no reply within 5000ms (3/3)
[2026-03-01 14:37:03] [CRITICAL]      Connectivity fault confirmed — 3/3 consecutive probe failures across all targets
[2026-03-01 14:37:03] [RECOVERY]      Phase 1: Dispatching TR-064 SOAP call — Action: ForceTermination
[2026-03-01 14:37:04] [RECOVERY]      POST /upnp/control/wanipconnection1 HTTP/1.1 — SOAPAction: "ForceTermination"
[2026-03-01 14:37:04] [RECOVERY]      Holding for 120s — awaiting PPPoE re-negotiation
[2026-03-01 14:39:04] [PROBE]         Post-recovery validation — ICMP/8.8.8.8: ✓ 4.1ms | ICMP/1.1.1.1: ✓ 3.8ms | DNS/9.9.9.9: ✓ 12.0ms
[2026-03-01 14:39:04] [RESOLVED]      Upstream connectivity restored — Phase 1 recovery successful
[2026-03-01 14:39:04] [TELEMETRY]     WAN uptime: 120s | External IPv4: 84.132.xx.xx | ...
```

### Example — Phase 2 escalation (full reboot)

```
[2026-03-01 14:39:04] [FAILED]        Post-recovery probes negative — All targets unreachable after Phase 1
[2026-03-01 14:39:04] [ESCALATION]    Phase 1 insufficient — Elevating to Phase 2: Full device reboot
[2026-03-01 14:39:04] [ESCALATION]    Dispatching TR-064 SOAP call — Action: Reboot (urn:dslforum-org:service:DeviceConfig:1)
[2026-03-01 14:39:04] [ESCALATION]    Gateway reboot dispatched — holding for 180s
[2026-03-01 14:42:05] [PROBE]         Post-reboot validation — ICMP/8.8.8.8: ✓ 5.2ms | ICMP/1.1.1.1: ✓ 4.9ms | DNS/9.9.9.9: ✓ 18.0ms
[2026-03-01 14:42:05] [RESOLVED]      Full recovery confirmed — Gateway operational
[2026-03-01 14:42:06] [TELEMETRY]     WAN uptime: 8s | External IPv4: 84.132.xx.xx | ...
```

---

## Recovery Flow

```
Probe cycle (every PROBE_INTERVAL seconds)
    │
    ├─ All probes pass ──────────────────► Reset failure counter, log NOMINAL periodically
    │
    └─ Any probe fails
           │
           ├─ failure count < FAIL_THRESHOLD ──► Log DEGRADED, continue
           │
           └─ failure count >= FAIL_THRESHOLD
                  │
                  ├─ ACTION_COOLDOWN not elapsed ──► Log WARN, skip recovery
                  ├─ MAX_ESCALATIONS reached ──────► Log WARN, skip recovery
                  │
                  └─ Trigger recovery
                         │
                         ├─ Phase 1: ForceTermination (TR-064)
                         │   Wait RECONNECT_HOLD seconds
                         │   Re-probe
                         │   │
                         │   ├─ Probes pass ──► Log RESOLVED
                         │   │
                         │   └─ Probes fail ──► Log FAILED
                         │          │
                         │          └─ Phase 2: Reboot (TR-064)
                         │              Wait REBOOT_HOLD seconds
                         │              Re-probe
                         │              │
                         │              ├─ Probes pass ──► Log RESOLVED
                         │              └─ Probes fail ──► Log CRITICAL
```

### Safety Mechanisms

- **Action cooldown:** At least `ACTION_COOLDOWN` seconds (default: 600) must elapse between consecutive recovery actions to avoid rapid cycling.
- **Escalation limit:** No more than `MAX_ESCALATIONS` (default: 5) Phase 2 escalations are permitted per 24-hour window.
- **Graceful shutdown:** SIGTERM and SIGINT are handled; the daemon logs a shutdown message and exits cleanly.

---

## Scheduled Maintenance

FritzWatchdog supports a weekly maintenance reboot window. Set `SCHEDULED_REBOOT` to `DAY/HH:MM`:

```env
SCHEDULED_REBOOT=TUE/04:00   # Every Tuesday at 04:00 local time
```

When the scheduled time arrives, the daemon:
1. Dispatches a `Reboot` TR-064 call.
2. Waits `REBOOT_HOLD` seconds.
3. Runs a probe validation.
4. Logs the result and schedules the next occurrence (7 days later).

---

## Architecture Overview

```
+─────────────────────────────────────────────────+
|  Docker Container (network_mode: host)           |
|                                                  |
|  watchdog.py                                     |
|  +────────────+   ICMP (ping subprocess)         |
|  |            |──────────────────► 8.8.8.8       |
|  |  Probe     |──────────────────► 1.1.1.1       |
|  |  Engine    |   DNS UDP socket                  |
|  |            |──────────────────► 9.9.9.9:53    |
|  +─────+──────+                                  |
|        | fault detected                           |
|  +─────▼──────+   TR-064 SOAP (HTTP Digest)      |
|  |  Recovery  |──────────────────► 192.168.178.1 |
|  |  Engine    |   ForceTermination / Reboot       |
|  +────────────+                                  |
|                                                  |
|  Logs ──► stdout + /var/log/fritzwatchdog/       |
+─────────────────────────────────────────────────+
           | volume mount
           ▼
       ./logs/  (host filesystem)
```

**Key dependencies:**

| Component | Purpose |
|-----------|---------|
| `fritzconnection 1.15.1` | TR-064 SOAP client with HTTP Digest authentication |
| `iputils-ping` | System `ping` binary for ICMP probes |
| Python 3.12 `socket` | Raw UDP socket for DNS probes |
| Python 3.12 `subprocess` | Invoke system `ping` |
| Python 3.12 `signal` | SIGTERM / SIGINT handling |
| Python 3.12 `logging` | Rotating file handler + stdout |

---

## License

This project is provided as-is without a formal license. You are free to use, modify, and distribute it for personal and internal use.
