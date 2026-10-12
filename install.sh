#!/bin/bash
# Rebuild ringo's system configuration on a fresh Raspberry Pi OS (Debian 13) install.
#
# Usage, as user toddz with this repo cloned into ~ :
#   ./install.sh
#
# Safe to re-run: it only copies files and enables services. It does not start
# or restart anything on its own; reboot when it finishes (the hardware watchdog
# handoff must only ever be activated by a reboot, never live).
#
# NOT covered here, redo by hand (secrets stay out of this public repo):
#   - Wi-Fi profiles (soupy priority 10, shaky priority 5)  -> ~/.secrets/credentials.md
#   - tailscale up
#   - rtpmidid and rustdesk: installed from upstream .deb releases, not apt
#   - RustDesk client config (root config must stay chattr +i)
#   - /etc/default/conn-monitor containing RUSTDESK_HOST=<host>
#   - SSH keys, gh auth, ~/ai-context clone and Claude Code hooks
#   - /boot/firmware/config.txt [all]: gpu_mem=16, dtoverlay=disable-bt, dtparam=audio=off
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO"

echo "== packages"
sudo apt-get update -q
sudo apt-get install -y -q qlcplus xvfb watchdog python3-rtmidi python3-pip alsa-utils
pip install --user --break-system-packages python-osc websocket-client

echo "== system files"
# system/ mirrors absolute paths
(cd system && find etc usr -type f -print0) | while IFS= read -r -d '' f; do
  mode=644
  [[ "$f" == usr/local/bin/* ]] && mode=755
  sudo install -D -m "$mode" "system/$f" "/$f"
done
sudo install -d -m 700 /var/lib/watchdog-io-test

echo "== brain, units, udev rules"
sudo install -m 755 ringo-midi-brain.py /usr/local/bin/ringo-midi-brain.py
sudo install -m 644 ringo-midi-brain.service qlcplus-headless.service qlcplus-usb-hotplug.service /etc/systemd/system/
sudo install -m 644 99-dmx-ftdi.rules 99-qlcplus-dmx-hotplug.rules /etc/udev/rules.d/

echo "== QLC+ fixture definitions"
install -d ~/.qlcplus/fixtures
install -m 644 QLC+/*.qxf ~/.qlcplus/fixtures/

echo "== crontab, groups"
crontab system/crontab.toddz
sudo usermod -aG audio,dialout,plugdev "$USER"

echo "== enable services"
sudo systemctl daemon-reload
sudo udevadm control --reload
sudo systemctl enable qlcplus-headless ringo-midi-brain conn-monitor watchdog
systemctl list-unit-files rtpmidid.service >/dev/null 2>&1 && sudo systemctl enable rtpmidid || echo "   rtpmidid not installed yet"

echo
echo "Done. Finish the manual steps listed at the top of this script, then reboot."
