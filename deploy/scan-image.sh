#!/usr/bin/env bash
# Scan a container image for fixable HIGH/CRITICAL vulnerabilities. Exit 1 if any are found.
#
#   deploy/scan-image.sh nuroflow-backend:latest
#
# Trivy runs from its official image, pinned by digest: CI, releases and laptops all run
# the identical scanner, with no install script to break (the setup-trivy action's
# binary download failed in CI, so the scan never ran).
# Extra `docker run` flags (e.g. a proxy) can be passed via TRIVY_DOCKER_ARGS.
set -euo pipefail

IMAGE="${1:?usage: scan-image.sh <image>}"
TRIVY_IMAGE="aquasec/trivy@sha256:a22415a38938a56c379387a8163fcb0ce38b10ace73e593475d3658d578b2436" # 0.65.0
CACHE_DIR="${TRIVY_CACHE_DIR:-$HOME/.cache/trivy}"
mkdir -p "$CACHE_DIR"

# shellcheck disable=SC2086  # TRIVY_DOCKER_ARGS is intentionally word-split
docker run --rm ${TRIVY_DOCKER_ARGS:-} \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -v "$CACHE_DIR:/root/.cache/trivy" \
  "$TRIVY_IMAGE" image \
  --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed \
  --no-progress --exit-code 1 \
  "$IMAGE"
