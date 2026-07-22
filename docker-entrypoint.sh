#!/bin/sh
set -eu

if [ "$(id -u)" != "0" ]; then
  exec "$@"
fi

PUID="${PUID:-99}"
PGID="${PGID:-100}"
APP_USER="librairy"
APP_GROUP="librairy"

if getent group "${PGID}" >/dev/null 2>&1; then
  APP_GROUP="$(getent group "${PGID}" | cut -d: -f1)"
else
  groupmod -o -g "${PGID}" "${APP_GROUP}"
fi

if getent passwd "${PUID}" >/dev/null 2>&1; then
  APP_USER="$(getent passwd "${PUID}" | cut -d: -f1)"
else
  usermod -o -u "${PUID}" -g "${APP_GROUP}" "${APP_USER}"
fi

mkdir -p /data/inbox /data/library /data/quarantine /data/appdata
chown -R "${PUID}:${PGID}" /data/inbox /data/library /data/quarantine /data/appdata /app

exec gosu "${PUID}:${PGID}" "$@"
