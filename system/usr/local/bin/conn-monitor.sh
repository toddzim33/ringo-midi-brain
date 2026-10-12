#!/bin/bash
# Correlates Tailscale health, DERP/control-plane TCP reachability, and RustDesk
# port reachability over time so intermittent drops can be pattern-matched
# instead of only caught by luck. Appends one line per check to LOGFILE.
LOGFILE=/home/toddz/conn-monitor.log
# RUSTDESK_HOST is machine-local; set it in /etc/default/conn-monitor.
RUSTDESK_HOST=
[ -r /etc/default/conn-monitor ] && . /etc/default/conn-monitor

while true; do
  ts=$(date '+%Y-%m-%d %H:%M:%S')

  ts_status=$(tailscale status 2>&1)
  if echo "$ts_status" | grep -q "hasn't received a network map"; then
    ts_state="STALE_NETMAP"
  elif echo "$ts_status" | grep -q "^100\.64\.191\.79.*offline"; then
    ts_state="SELF_OFFLINE"
  else
    ts_state="OK"
  fi

  ctrl_rc=0
  curl -s -o /dev/null --max-time 4 https://controlplane.tailscale.com/ || ctrl_rc=$?

  derp_rc=0
  curl -s -o /dev/null --max-time 4 https://derp10.tailscale.com/ || derp_rc=$?

  rd_ports=""
  for p in 21115 21116 21117; do
    if timeout 2 bash -c "echo > /dev/tcp/${RUSTDESK_HOST}/${p}" 2>/dev/null; then
      rd_ports="${rd_ports}${p}:open "
    else
      rd_ports="${rd_ports}${p}:FAIL "
    fi
  done

  echo "${ts} ts_state=${ts_state} ctrl_https_rc=${ctrl_rc} derp_https_rc=${derp_rc} rustdesk[${rd_ports}]" >> "$LOGFILE"

  sleep 60
done
