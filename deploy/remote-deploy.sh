#!/usr/bin/env bash
# Runs ON the EC2 instance (as root, via SSM) from the GitHub Actions deploy job.
# Pulls IMAGE from ECR, replaces the running container, waits for it to become
# healthy, and rolls back to the previous image if it does not.
#
#   IMAGE=<account>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag> bash remote-deploy.sh
set -euo pipefail

: "${IMAGE:?IMAGE is required}"
NAME=chatbot
REGION="${AWS_REGION:-us-east-1}"
REGISTRY="${IMAGE%%/*}"

aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
docker pull "$IMAGE"

PREV="$(docker inspect -f '{{.Config.Image}}' "$NAME" 2>/dev/null || true)"

run_container() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker run -d --name "$NAME" --restart unless-stopped \
    -p 5000:5000 -e AWS_REGION="$REGION" -v chatbot-data:/data "$1" >/dev/null
}

wait_healthy() {
  local status
  for _ in $(seq 1 40); do
    status="$(docker inspect -f '{{.State.Health.Status}}' "$NAME" 2>/dev/null || echo none)"
    [ "$status" = healthy ] && return 0
    [ "$status" = unhealthy ] && return 1
    sleep 3
  done
  return 1
}

echo "Deploying $IMAGE (previous: ${PREV:-none})"
run_container "$IMAGE"

if wait_healthy; then
  curl -fsS http://127.0.0.1:5000/healthz && echo
  docker image prune -af >/dev/null   # keep the 8 GB disk from filling up
  echo "DEPLOYED $IMAGE"
  exit 0
fi

echo "New container is not healthy. Last logs:"
docker logs --tail 40 "$NAME" 2>&1 || true

if [ -n "$PREV" ] && [ "$PREV" != "$IMAGE" ]; then
  echo "Rolling back to $PREV"
  run_container "$PREV"
  wait_healthy && echo "ROLLED BACK to $PREV" || echo "Rollback container is not healthy either"
fi
exit 1
