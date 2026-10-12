#!/bin/bash
if tailscale status 2>&1 | grep -q "hasn't received a network map"; then
  sudo systemctl restart tailscaled
fi
