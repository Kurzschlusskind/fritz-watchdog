#!/usr/bin/env python3
"""
FritzWatchdog — Automated upstream connectivity monitor and recovery daemon
for AVM FRITZ!Box gateways via TR-064 SOAP protocol.
"""

import os
import re
import signal
import socket
import struct
import subprocess
import sys
import time
import logging
import logging.handlers
from datetime import datetime, timedelta

try:
    from fritzconnection.lib.fritzconnection import FritzConnection
except ImportError:
    print("fritzconnection library not found. Install with: pip install fritzconnection", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration — environment variables
# ---------------------------------------------------------------------------

FRITZ_ADDRESS    = os.environ.get("FRITZ_ADDRESS", "192.168.178.1")
FRITZ_USER       = os.environ.get("FRITZ_USER", "watchdog")
FRITZ_PASSWORD   = os.environ.get("FRITZ_PASSWORD", "")
FRITZ_PORT       = int(os.environ.get("FRITZ_PORT", "49000"))

PROBE_INTERVAL   = int(os.environ.get("PROBE_INTERVAL", "60"))
FAIL_THRESHOLD   = int(os.environ.get("FAIL_THRESHOLD", "3"))
RECONNECT_HOLD   = int(os.environ.get("RECONNECT_HOLD", "120"))
REBOOT_HOLD      = int(os.environ.get("REBOOT_HOLD", "180"))
ACTION_COOLDOWN  = int(os.environ.get("ACTION_COOLDOWN", "600"))
MAX_ESCALATIONS  = int(os.environ.get("MAX_ESCALATIONS", "5"))

ICMP_TARGETS     = [t.strip() for t in os.environ.get("ICMP_TARGETS", "8.8.8.8,1.1.1.1").split(",") if t.strip()]
DNS_TARGET       = os.environ.get("DNS_TARGET", "9.9.9.9")
DNS_QUERY        = os.environ.get("DNS_QUERY", "google.com")
DNS_PORT         = int(os.environ.get("DNS_PORT", "53"))

SCHEDULED_REBOOT = os.environ.get("SCHEDULED_REBOOT", "TUE/04:00")

LOG_DIR          = os.environ.get("LOG_DIR", "/var/log/fritzwatchdog")
LOG_FILE         = os.path.join(LOG_DIR, "watchdog.log")

NOMINAL_INTERVAL = int(os.environ.get("NOMINAL_INTERVAL", "600"))  # seconds between NOMINAL log entries

# Day name → weekday index (Monday = 0)
_DAY_MAP = {
    "MON": 0, "TUE": 1, "WED": 2, "THU": 3,
    "FRI": 4, "SAT": 5, "SUN": 6,
}

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

class PrecisionFormatter(logging.Formatter):
    """Formatter that produces the exact log style required."""

    def format(self, record):
        ts = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
        tag = record.getMessage().split("]", 1)
        # Messages are pre-tagged with [TAG] prefix
        return f"[{ts}] {record.getMessage()}"


def _setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = PrecisionFormatter()

    # stdout handler
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # rotating file handler (10 MB × 5)
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)


log = logging.getLogger(__name__)


def _log(tag: str, msg: str, level: int = logging.INFO):
    tag_padded = f"[{tag}]".ljust(14)
    log.log(level, f"{tag_padded} {msg}")


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

_shutdown_requested = False


def _handle_signal(signum, frame):
    global _shutdown_requested
    _log("SHUTDOWN", f"Signal {signum} received — initiating graceful shutdown")
    _shutdown_requested = True


# ---------------------------------------------------------------------------
# Scheduling helpers
# ---------------------------------------------------------------------------

def _parse_scheduled_reboot(spec: str):
    """Parse 'TUE/04:00' into (weekday_index, hour, minute)."""
    try:
        day_part, time_part = spec.upper().split("/")
        weekday = _DAY_MAP[day_part.strip()]
        hour, minute = (int(x) for x in time_part.strip().split(":"))
        return weekday, hour, minute
    except (KeyError, ValueError):
        _log("WARN", f"Invalid SCHEDULED_REBOOT format '{spec}' — scheduled reboot disabled", logging.WARNING)
        return None


def _next_scheduled_reboot(weekday: int, hour: int, minute: int) -> datetime:
    """Return the next datetime matching the given weekday/hour/minute."""
    now = datetime.now()
    days_ahead = (weekday - now.weekday()) % 7
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days_ahead)
    if candidate <= now:
        candidate += timedelta(weeks=1)
    return candidate


# ---------------------------------------------------------------------------
# Probe functions
# ---------------------------------------------------------------------------

def _icmp_probe(target: str, timeout: int = 5) -> tuple[bool, float]:
    """
    Send a single ICMP echo request via the system ping binary.
    Returns (success, rtt_ms).
    """
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), target],
            capture_output=True,
            text=True,
            timeout=timeout + 2,
        )
        if result.returncode == 0:
            # Extract RTT from ping output: "time=4.12 ms"
            m = re.search(r"time[=<]([\d.]+)\s*ms", result.stdout)
            rtt = float(m.group(1)) if m else 0.0
            return True, rtt
        return False, 0.0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False, 0.0


def _build_dns_query(hostname: str, qid: int = 0x1234) -> bytes:
    """Build a minimal DNS A-record query packet."""
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    labels = b""
    for part in hostname.split("."):
        encoded = part.encode("ascii")
        labels += struct.pack("B", len(encoded)) + encoded
    labels += b"\x00"
    question = labels + struct.pack(">HH", 1, 1)  # QTYPE=A, QCLASS=IN
    return header + question


def _dns_probe(server: str, query: str, port: int = 53, timeout: int = 5) -> tuple[bool, float]:
    """Send a raw DNS UDP query and check for a valid response."""
    try:
        packet = _build_dns_query(query)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        t0 = time.monotonic()
        sock.sendto(packet, (server, port))
        data, _ = sock.recvfrom(512)
        rtt_ms = (time.monotonic() - t0) * 1000
        sock.close()
        # Minimal validation: response bit set and RCODE == 0
        if len(data) >= 4:
            flags = struct.unpack(">H", data[2:4])[0]
            qr = (flags >> 15) & 1
            rcode = flags & 0x000F
            return (qr == 1 and rcode == 0), rtt_ms
        return False, 0.0
    except (socket.timeout, OSError):
        return False, 0.0


def _run_probes() -> dict:
    """
    Run all configured probes. Returns a dict with results keyed by probe label.
    """
    results = {}
    for target in ICMP_TARGETS:
        ok, rtt = _icmp_probe(target)
        results[f"ICMP/{target}"] = (ok, rtt)
    ok, rtt = _dns_probe(DNS_TARGET, DNS_QUERY, DNS_PORT)
    results[f"DNS/{DNS_TARGET}"] = (ok, rtt)
    return results


def _all_pass(results: dict) -> bool:
    return all(ok for ok, _ in results.values())


def _format_probe_results(results: dict) -> str:
    parts = []
    for label, (ok, rtt) in results.items():
        if ok:
            parts.append(f"{label}: ✓ {rtt:.1f}ms")
        else:
            parts.append(f"{label}: ✗ timeout")
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# TR-064 / FritzConnection helpers
# ---------------------------------------------------------------------------

def _get_fritz_connection() -> FritzConnection:
    return FritzConnection(
        address=FRITZ_ADDRESS,
        port=FRITZ_PORT,
        user=FRITZ_USER,
        password=FRITZ_PASSWORD,
        use_cache=False,
    )


def _fetch_telemetry(fc: FritzConnection) -> str:
    """Fetch WAN status and return a telemetry string."""
    try:
        wan = fc.call_action("WANIPConnection:1", "GetStatusInfo")
        ext_ip_info = fc.call_action("WANIPConnection:1", "GetExternalIPAddress")
        link = fc.call_action("WANCommonInterfaceConfig:1", "GetCommonLinkProperties")

        uptime = wan.get("NewUptime", "?")
        ext_ip = ext_ip_info.get("NewExternalIPAddress", "?.?.?.?")
        down_rate = link.get("NewLayer1DownstreamMaxBitRate", 0)
        up_rate = link.get("NewLayer1UpstreamMaxBitRate", 0)

        # Mask last two octets of external IP (e.g. 84.132.xx.xx)
        masked = re.sub(r"(\d+\.\d+\.)\d+\.\d+$", r"\1xx.xx", ext_ip)
        down_kbps = int(down_rate) // 1000 if down_rate else 0
        up_kbps = int(up_rate) // 1000 if up_rate else 0

        return (
            f"WAN uptime: {uptime}s | External IPv4: {masked} | "
            f"Link rate: {down_kbps} kbps down / {up_kbps} kbps up"
        )
    except Exception as exc:
        return f"Telemetry unavailable: {exc}"


def _force_termination(fc: FritzConnection):
    """Dispatch TR-064 ForceTermination SOAP call."""
    fc.call_action("WANIPConnection:1", "ForceTermination")
    _log("RECOVERY", "POST /upnp/control/wanipconnection1 HTTP/1.1 — SOAPAction: \"ForceTermination\"")


def _reboot(fc: FritzConnection):
    """Dispatch TR-064 Reboot SOAP call."""
    _log("ESCALATION", "Dispatching TR-064 SOAP call — Action: Reboot (urn:dslforum-org:service:DeviceConfig:1)")
    fc.call_action("DeviceConfig:1", "Reboot")


# ---------------------------------------------------------------------------
# Recovery logic
# ---------------------------------------------------------------------------

def _phase1_recovery(fc: FritzConnection) -> bool:
    """
    Phase 1: Force WAN reconnect via ForceTermination.
    Returns True if post-recovery probes pass.
    """
    _log("RECOVERY", "Phase 1: Dispatching TR-064 SOAP call — Action: ForceTermination")
    try:
        _force_termination(fc)
    except Exception as exc:
        _log("ERROR", f"TR-064 ForceTermination failed: {exc}", logging.ERROR)
        return False

    _log("RECOVERY", f"Holding for {RECONNECT_HOLD}s — awaiting PPPoE re-negotiation")
    _interruptible_sleep(RECONNECT_HOLD)

    results = _run_probes()
    _log("PROBE", f"Post-recovery validation — {_format_probe_results(results)}")
    return _all_pass(results)


def _phase2_recovery(fc: FritzConnection) -> bool:
    """
    Phase 2: Full device reboot.
    Returns True if post-recovery probes pass.
    """
    _log("ESCALATION", "Phase 1 insufficient — Elevating to Phase 2: Full device reboot")
    try:
        _reboot(fc)
    except Exception as exc:
        _log("ERROR", f"TR-064 Reboot failed: {exc}", logging.ERROR)
        return False

    _log("ESCALATION", f"Gateway reboot dispatched — holding for {REBOOT_HOLD}s")
    _interruptible_sleep(REBOOT_HOLD)

    results = _run_probes()
    _log("PROBE", f"Post-reboot validation — {_format_probe_results(results)}")
    return _all_pass(results)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _interruptible_sleep(seconds: int):
    """Sleep in small increments so shutdown signals are handled promptly."""
    deadline = time.monotonic() + seconds
    while not _shutdown_requested and time.monotonic() < deadline:
        time.sleep(min(1, deadline - time.monotonic()))


# ---------------------------------------------------------------------------
# Main daemon loop
# ---------------------------------------------------------------------------

def main():
    _setup_logging()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    pid = os.getpid()
    _log("INIT", f"Watchdog daemon initialized — PID {pid}")
    _log("INIT", f"Target gateway: {FRITZ_ADDRESS} (AVM FRITZ!Box)")
    _log("INIT", f"TR-064 SOAP endpoint: http://{FRITZ_ADDRESS}:{FRITZ_PORT} — Auth: DIGEST-MD5")

    if not FRITZ_PASSWORD:
        _log("ERROR", "FRITZ_PASSWORD environment variable is required but not set", logging.ERROR)
        sys.exit(1)

    # Parse scheduled reboot spec
    sched = _parse_scheduled_reboot(SCHEDULED_REBOOT)
    next_sched_reboot = None
    if sched:
        weekday, hour, minute = sched
        next_sched_reboot = _next_scheduled_reboot(weekday, hour, minute)
        _log("INIT", f"Scheduled maintenance reboot: {next_sched_reboot.strftime('%Y-%m-%d %H:%M')} (weekly {SCHEDULED_REBOOT})")

    # Initial TR-064 handshake + telemetry
    try:
        fc = _get_fritz_connection()
        _log("HANDSHAKE", "TR-064 session established — ServiceType: urn:dslforum-org:service:WANIPConnection:1")
        telemetry = _fetch_telemetry(fc)
        _log("TELEMETRY", telemetry)
    except Exception as exc:
        _log("ERROR", f"Initial TR-064 handshake failed: {exc}", logging.ERROR)
        fc = None

    consecutive_failures = 0
    last_action_time = 0.0
    escalations_today = 0
    escalation_day = datetime.now().date()
    last_nominal_log = 0.0

    _log("INIT", f"Monitoring loop started — probe interval: {PROBE_INTERVAL}s, fail threshold: {FAIL_THRESHOLD}")

    while not _shutdown_requested:
        now = time.monotonic()
        now_dt = datetime.now()

        # Reset daily escalation counter
        if now_dt.date() != escalation_day:
            escalation_day = now_dt.date()
            escalations_today = 0

        # Check scheduled reboot
        if next_sched_reboot and now_dt >= next_sched_reboot:
            _log("MAINTENANCE", f"Scheduled maintenance window reached — initiating full reboot")
            try:
                if fc is None:
                    fc = _get_fritz_connection()
                _reboot(fc)
                _log("MAINTENANCE", f"Reboot dispatched — holding for {REBOOT_HOLD}s")
                _interruptible_sleep(REBOOT_HOLD)
                results = _run_probes()
                _log("PROBE", f"Post-maintenance validation — {_format_probe_results(results)}")
                if _all_pass(results):
                    _log("MAINTENANCE", "Gateway operational — resuming monitoring")
                else:
                    _log("WARN", "Post-maintenance probes failed — gateway may still be recovering", logging.WARNING)
            except Exception as exc:
                _log("ERROR", f"Scheduled reboot failed: {exc}", logging.ERROR)
            # Schedule next occurrence
            weekday, hour, minute = sched
            next_sched_reboot = _next_scheduled_reboot(weekday, hour, minute)
            _log("MAINTENANCE", f"Next scheduled maintenance: {next_sched_reboot.strftime('%Y-%m-%d %H:%M')}")

        # Run probes
        results = _run_probes()
        all_ok = _all_pass(results)

        if all_ok:
            consecutive_failures = 0
            # Log NOMINAL status periodically
            if now - last_nominal_log >= NOMINAL_INTERVAL:
                _log("NOMINAL", f"Upstream connectivity healthy — {_format_probe_results(results)}")
                last_nominal_log = now
        else:
            consecutive_failures += 1
            # Log each failure with counter
            for label, (ok, rtt) in results.items():
                if not ok:
                    proto, target = label.split("/", 1)
                    if proto == "ICMP":
                        _log("DEGRADED",
                             f"ICMP probe timeout — dst={target} — no reply within 5000ms "
                             f"({consecutive_failures}/{FAIL_THRESHOLD})",
                             logging.WARNING)
                    else:
                        _log("DEGRADED",
                             f"DNS probe timeout — server={target} query={DNS_QUERY} "
                             f"({consecutive_failures}/{FAIL_THRESHOLD})",
                             logging.WARNING)

        if consecutive_failures >= FAIL_THRESHOLD:
            _log("CRITICAL",
                 f"Connectivity fault confirmed — {FAIL_THRESHOLD}/{FAIL_THRESHOLD} consecutive probe failures across all targets",
                 logging.ERROR)

            cooldown_remaining = ACTION_COOLDOWN - (now - last_action_time)
            if cooldown_remaining > 0:
                _log("WARN",
                     f"Action cooldown active — {cooldown_remaining:.0f}s remaining before next recovery action",
                     logging.WARNING)
                _interruptible_sleep(PROBE_INTERVAL)
                continue

            if escalations_today >= MAX_ESCALATIONS:
                _log("WARN",
                     f"Escalation limit reached — {escalations_today}/{MAX_ESCALATIONS} escalations today — manual intervention required",
                     logging.WARNING)
                _interruptible_sleep(PROBE_INTERVAL)
                continue

            # Ensure TR-064 connection
            try:
                if fc is None:
                    fc = _get_fritz_connection()
            except Exception as exc:
                _log("ERROR", f"Cannot establish TR-064 connection: {exc}", logging.ERROR)
                _interruptible_sleep(PROBE_INTERVAL)
                continue

            last_action_time = now
            consecutive_failures = 0

            # Phase 1
            if _phase1_recovery(fc):
                _log("RESOLVED", "Upstream connectivity restored — Phase 1 recovery successful")
                try:
                    telemetry = _fetch_telemetry(fc)
                    _log("TELEMETRY", telemetry)
                except Exception:
                    pass
            else:
                _log("FAILED", "Post-recovery probes negative — All targets unreachable after Phase 1", logging.ERROR)
                escalations_today += 1

                # Phase 2
                if _phase2_recovery(fc):
                    _log("RESOLVED", "Full recovery confirmed — Gateway operational")
                    try:
                        telemetry = _fetch_telemetry(fc)
                        _log("TELEMETRY", telemetry)
                    except Exception:
                        pass
                else:
                    _log("CRITICAL",
                         "Phase 2 recovery failed — Gateway unresponsive — manual intervention required",
                         logging.CRITICAL)

        _interruptible_sleep(PROBE_INTERVAL)

    _log("SHUTDOWN", "Watchdog daemon stopped — goodbye")


if __name__ == "__main__":
    main()
