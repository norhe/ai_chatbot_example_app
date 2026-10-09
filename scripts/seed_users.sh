#!/usr/bin/env bash
# seed_users.sh — Patch demo user placeholder Keycloak sub UUIDs in the database.
#
# Prerequisites:
#   - scripts/demo_user_subs.env written by setup_keycloak_kcadm.sh
#   - DATABASE_URL set in the environment
#
# Usage: ./scripts/seed_users.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SUBS_FILE="${SCRIPT_DIR}/demo_user_subs.env"

if [[ ! -f "${SUBS_FILE}" ]]; then
    echo "ERROR: ${SUBS_FILE} not found. Run ./scripts/setup_keycloak_kcadm.sh first." >&2
    exit 1
fi

# shellcheck source=/dev/null
set -a
source "${SUBS_FILE}"
set +a

echo "==> Patching demo user Keycloak sub UUIDs in the database..."
python3 "${SCRIPT_DIR}/../db.py" seed-users
echo "==> Done."
