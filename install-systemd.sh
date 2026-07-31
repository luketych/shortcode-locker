#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run as root: sudo env \"PATH=$PATH\" ./install-systemd.sh" >&2
  exit 1
fi

SRC_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="/opt/shortcode_locker"
DATA_DIR="/var/lib/shortcode_locker"
SERVICE_USER="shortcode-locker"
SERVICE_NAME="shortcode-locker"
ENV_FILE="/etc/shortcode_locker.env"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

if ! getent group "${SERVICE_USER}" >/dev/null 2>&1; then
  groupadd --system "${SERVICE_USER}"
fi

if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  NOLOGIN="$(command -v nologin || true)"
  if [[ -z "${NOLOGIN}" || ! -x "${NOLOGIN}" ]]; then
    NOLOGIN="/bin/false"
  fi
  useradd --system --home-dir "${DATA_DIR}" --shell "${NOLOGIN}" --gid "${SERVICE_USER}" "${SERVICE_USER}"
fi

install -d -m 0755 "${APP_DIR}"
install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${DATA_DIR}"
install -m 0755 "${SRC_DIR}/app.py" "${APP_DIR}/app.py"
install -m 0644 "${SRC_DIR}/pyproject.toml" "${APP_DIR}/pyproject.toml"
install -m 0644 "${SRC_DIR}/uv.lock" "${APP_DIR}/uv.lock"
install -m 0644 "${SRC_DIR}/.python-version" "${APP_DIR}/.python-version"
install -m 0644 "${SRC_DIR}/README.md" "${APP_DIR}/README.md"

(
  cd "${APP_DIR}"
  uv sync --frozen --no-dev
)

if [[ ! -f "${DATA_DIR}/codes.json" ]]; then
  install -m 0640 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${SRC_DIR}/data/codes.json" "${DATA_DIR}/codes.json"
fi

if [[ ! -f "${DATA_DIR}/config.json" ]]; then
  install -m 0640 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${SRC_DIR}/data/config.json" "${DATA_DIR}/config.json"
fi

install -m 0644 "${SRC_DIR}/systemd/${SERVICE_NAME}.service" "/etc/systemd/system/${SERVICE_NAME}.service"

if [[ ! -f "${ENV_FILE}" ]]; then
  cat >"${ENV_FILE}" <<'ENV'
# Optional but recommended if reachable beyond a trusted LAN.
# SHORTCODE_LOCKER_TOKEN=change-this-token
ENV
  chmod 0600 "${ENV_FILE}"
fi

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"
systemctl --no-pager status "${SERVICE_NAME}"

echo
printf 'Installed. Open http://<zimaboard-ip>:%s\n' "${PORT:-8765}"
echo "Data: ${DATA_DIR}/codes.json"
echo "Optional token config: ${ENV_FILE}"
