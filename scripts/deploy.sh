#!/usr/bin/env bash
# Deploy yta/ + tests/ to the AWS box: sync, build, prune, restart, verify.
#
# The prune step exists because the disk filled up once already this
# session -- every "docker build" leaves cache behind, and the root
# volume is only 8.6GB. Pruning after every build keeps that from ever
# re-accumulating past what the NEXT build needs, rather than trusting
# anyone (human or Claude) to remember to run it by hand.
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

echo "==> Building on AWS..."
# No pipe on this ssh call -- a piped `| tail` masks docker build's real
# exit code (bit us once: a failed build's stale "latest" tag got
# redeployed anyway because `set -e` never saw the failure). Let it
# print in full and fail the script for real if it fails.
$SSH 'cd ~/app && docker build -t bookmystay .'

echo "==> Pruning build cache (keeps the small root disk from filling up)..."
$SSH 'docker builder prune -af'

echo "==> Restarting the container..."
$SSH '
  docker stop bookmystay 2>/dev/null || true
  docker rm bookmystay 2>/dev/null || true
  docker run -d --name bookmystay --env-file ~/app/.env \
    -v /home/ubuntu/data:/app/data -p 8765:8765 \
    --restart unless-stopped bookmystay
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
