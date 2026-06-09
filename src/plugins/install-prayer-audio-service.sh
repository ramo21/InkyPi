#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="inkypi-prayer-audio.service"
RUN_USER="${SUDO_USER:-${USER}}"
RUN_HOME="$(eval echo "~${RUN_USER}")"
CONFIG_DIR="${INKYPI_PRAYER_AUDIO_DIR:-${RUN_HOME}/.config/inkypi/prayer_audio}"

if [[ -f "${SCRIPT_DIR}/aladhan/prayer_audio_service.py" ]]; then
  SERVICE_SCRIPT="${SCRIPT_DIR}/aladhan/prayer_audio_service.py"
elif [[ -f "${SCRIPT_DIR}/weatheraladhan/prayer_audio_service.py" ]]; then
  SERVICE_SCRIPT="${SCRIPT_DIR}/weatheraladhan/prayer_audio_service.py"
elif [[ -f "${SCRIPT_DIR}/prayer_audio_service.py" ]]; then
  SERVICE_SCRIPT="${SCRIPT_DIR}/prayer_audio_service.py"
else
  echo "Could not find prayer_audio_service.py next to this installer." >&2
  exit 1
fi

sudo mkdir -p "${CONFIG_DIR}"
sudo chown -R "${RUN_USER}:${RUN_USER}" "${RUN_HOME}/.config" || true
sudo tee "/etc/systemd/system/${SERVICE_NAME}" >/dev/null <<EOF
[Unit]
Description=InkyPi Prayer Audio Scheduler
After=network.target sound.target
Wants=sound.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${SCRIPT_DIR}
Environment=INKYPI_PRAYER_AUDIO_DIR=${CONFIG_DIR}
ExecStart=/usr/bin/python3 ${SERVICE_SCRIPT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now "${SERVICE_NAME}"
echo "Installed and started ${SERVICE_NAME}"
echo "Config directory: ${CONFIG_DIR}"
echo "Check status with: sudo systemctl status ${SERVICE_NAME}"
