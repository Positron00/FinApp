#!/bin/sh
# Generate the Redis ACL file from env (PAY-607; platform LLD §2.2; R12, C3, C27) and start Redis.
# Passwords are stored as SHA-256 (#<hex>), so no plaintext reaches disk; the file lives on a tmpfs.
#   --print  prints the rule strings (hashes masked, default user omitted) for the managed-Redis runbook.
set -eu
umask 077
for v in REDIS_ADMIN_PASSWORD REDIS_WEB_PASSWORD REDIS_IDENTITY_PASSWORD REDIS_PAYROLL_PASSWORD \
         REDIS_PAYOUT_WORKER_PASSWORD REDIS_EXPORTER_PASSWORD; do
  eval "val=\${$v:-}"
  [ -n "$val" ] || { echo "redis-acl: missing $v" >&2; exit 1; }
done
h() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }
# -scan -randomkey: key patterns don't filter SCAN/RANDOMKEY results, so they'd list other users' keys (C63).
BASE='+@connection +ping +@read +@write +@keyspace +@transaction -@stream -@scripting -@pubsub -@admin -@dangerous -scan -randomkey'
WORKER_MAIN='~stream:payout.requested +xreadgroup +xack +xautoclaim +xpending +xinfo +xgroup|create +xgroup|delconsumer'
WORKER_DLQ='~stream:payout.requested.dlq +xadd +xinfo'
EXPORTER='+@connection +ping -@scripting -@dangerous +info +config|get +client|list +slowlog|get +slowlog|len +latency|latest +xinfo +xlen +type +dbsize +memory|stats'
acl() {
  cat <<ACL
user default on #$(h "$REDIS_ADMIN_PASSWORD") ~* &* +@all
user web on #$(h "$REDIS_WEB_PASSWORD") resetkeys resetchannels -@all ~web:* $BASE
user identity on #$(h "$REDIS_IDENTITY_PASSWORD") resetkeys resetchannels -@all ~identity:* $BASE
user payroll on #$(h "$REDIS_PAYROLL_PASSWORD") resetkeys resetchannels -@all ~payroll:* $BASE (~stream:payout.requested +xadd)
user payout-worker on #$(h "$REDIS_PAYOUT_WORKER_PASSWORD") resetkeys resetchannels -@all +@connection +ping -@scripting -@dangerous ($WORKER_MAIN) ($WORKER_DLQ)
user exporter on #$(h "$REDIS_EXPORTER_PASSWORD") resetkeys resetchannels -@all %R~stream:* $EXPORTER
ACL
}
if [ "${1:-}" = "--print" ]; then
  acl | sed -e '/^user default /d' -e 's/ #[0-9a-f]\{64\}/ #<sha256>/'
  exit 0
fi
mkdir -p /run/redis
acl > /run/redis/users.acl
chown redis:redis /run/redis/users.acl
exec docker-entrypoint.sh redis-server /usr/local/etc/redis/redis.conf
