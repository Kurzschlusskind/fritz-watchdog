# syntax=docker/dockerfile:1

FROM python:3.12-slim

LABEL maintainer="FritzWatchdog" \
      description="Automated upstream connectivity monitor and recovery daemon for AVM FRITZ!Box gateways"

# Install system dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends iputils-ping \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip install --no-cache-dir fritzconnection==1.15.1

# Create log directory
RUN mkdir -p /var/log/fritzwatchdog

# Copy application
COPY watchdog.py /opt/fritzwatchdog/watchdog.py

# Healthcheck: log file must have been modified within the last 300 seconds
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD test $(( $(date +%s) - $(stat -c %Y /var/log/fritzwatchdog/watchdog.log 2>/dev/null || echo 0) )) -lt 300

ENTRYPOINT ["python3", "-u", "/opt/fritzwatchdog/watchdog.py"]
