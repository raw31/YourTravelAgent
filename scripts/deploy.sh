#!/usr/bin/env bash
# Deploy yta/ + tests/ to the AWS box: sync, build, prune, restart, verify.
#
# The root volume is only 8.6GB -- a full build needs ~2GB of transient
# space (base image + apt + Playwright's Chrome/Chromium/FFmpeg
# downloads) before it consolidates down to the ~564MB final image, so
# there is NOT enough room to also keep a second (rollback) image
# resident during a build -- tried it, hit ENOSPC both times. The old
# image is removed BEFORE building the new one instead, so at most one
# image's footprint is ever committed at once. No local rollback image
# as a result; "rollback" is redeploying the previous commit, which
# takes the same ~70s as any other deploy. Never resize the instance or
# volume to work around this -- prune/remove only.
#
# Usage: ./scripts/deploy.sh
set -euo pipefail

HOST="ubuntu@52.64.46.121"
KEY="$HOME/.ssh/bookmystay-aws.pem"
SSH="ssh -i $KEY $HOST"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Syncing yta/ and tests/ to AWS..."
rsync -avz -e "ssh -i $KEY" "$REPO_ROOT/yta/" "$HOST:~/app/yta/"
rsync -avz -e "ssh -i $KEY" "$REPO_ROOT/tests/" "$HOST:~/app/tests/"

echo "==> Pruning any stale build cache from a previous failed attempt..."
# Runs BEFORE the build too, not just after -- a build that failed last
# time (almost always ENOSPC on this box) leaves its own cache behind,
# and pruning only on success could never recover from that on its own:
# the next attempt just inherited the same full disk.
$SSH 'docker builder prune -af' || true

echo "==> Archiving the running container's logs (a deploy removes the container, and its logs with it)..."
# Conversations (YTA_LOG_MESSAGES) and LLM/key events live only in the docker logs;
# keep a dated copy on the data volume, and drop copies older than 30 days.
$SSH '
  mkdir -p /home/ubuntu/data/log-archive
  docker logs --timestamps bookmystay > /home/ubuntu/data/log-archive/bookmystay-$(date -u +%Y%m%d-%H%M%S).log 2>&1 || true
  find /home/ubuntu/data/log-archive -name "bookmystay-*.log" -mtime +30 -delete 2>/dev/null || true
'

echo "==> Removing the old image (no room to keep it alongside a new build)..."
$SSH '
  docker stop bookmystay 2>/dev/null || true
  docker rm bookmystay 2>/dev/null || true
  docker rmi bookmystay:latest 2>/dev/null || true
'

echo "==> Building on AWS..."
# No pipe on this ssh call -- a piped `| tail` masks docker build's real
# exit code (bit us once: a failed build's stale "latest" tag got
# redeployed anyway because `set -e` never saw the failure). Let it
# print in full and fail the script for real if it fails.
$SSH 'cd ~/app && docker build -t bookmystay:latest .'

echo "==> Pruning build cache (keeps the small root disk from filling up)..."
$SSH 'docker builder prune -af'

echo "==> Starting the container..."
$SSH '
  docker run -d --name bookmystay --env-file ~/app/.env \
    -v /home/ubuntu/data:/app/data -p 8765:8765 \
    --restart unless-stopped bookmystay:latest
'

echo "==> Verifying..."
sleep 2
$SSH '
  echo "YTA_WA_FLOW: $(docker exec bookmystay printenv | grep YTA_WA_FLOW)"
  echo "Mounts: $(docker inspect bookmystay --format "{{json .Mounts}}")"
  docker exec bookmystay python -c "
import yta.leads.db as d
con = d.connect()
print(\"Leads:\", con.execute(\"SELECT COUNT(*) as c FROM leads\").fetchone()[\"c\"])
"
  echo "Health: $(curl -s -o /dev/null -w "%{http_code}" http://localhost:8765/)"
  echo "Disk: $(df -h / | tail -1)"
'
echo "==> Deploy complete."
