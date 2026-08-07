#!/bin/sh
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"
APP_USER="librairy"
APP_GROUP="librairy"

# The image defaults to USER librairy (uid 1000), so this is the normal path.
# Nothing to remap and nothing to chown — we only check that the mounts are
# actually writable, because a silent permission error here looks like a bug in
# LibrAIry rather than a bind-mount ownership problem on the host.
if [ "$(id -u)" != "0" ]; then
  for dir in /data/inbox /data/library /data/quarantine /data/appdata; do
    mkdir -p "${dir}" 2>/dev/null || true
    if [ ! -w "${dir}" ]; then
      echo "librairy: ${dir} is not writable by uid $(id -u)." >&2
      echo "librairy: either chown the host directory to $(id -u):$(id -g), or run the" >&2
      echo "librairy: container with user: \"0:0\" so PUID/PGID remapping can do it." >&2
      exit 1
    fi
  done
  exec "$@"
fi

# Root path: kept for hosts that map storage to a fixed uid/gid (UNRAID uses
# 99:100). Run the container with user: "0:0" to get here.
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

# Supervisor startup creates SQLite/log artifacts after the initial chown. Fix those
# once, then exit, so mounted appdata remains owned by PUID:PGID without keeping a
# root helper process alive.
(
  sleep 5
  chown -R "${PUID}:${PGID}" /data/appdata
) &

# setpriv keeps the environment, so HOME would still say /root after the drop
# — a directory mode 700 and owned by root. Anything that writes a dotfile or a
# cache under $HOME then fails as PUID, which is how czkawka spent every cycle
# panicking on a cache it could not create, with an empty stderr and an exit
# code of 101. /app is chowned to PUID:PGID above.
export HOME=/app

# setpriv replaces gosu: same exec-and-drop behaviour, but it comes from
# util-linux instead of a Go binary Debian builds against an ancient toolchain.
exec setpriv --reuid="${PUID}" --regid="${PGID}" --init-groups -- "$@"
