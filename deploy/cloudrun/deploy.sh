#!/usr/bin/env bash
# Deploys both pstb services onto the architecture the DEV project already
# tested: external ALB + Cloud Armor -> serverless NEG -> Cloud Run
# (ingress internal+LB) -> Direct VPC egress -> shared-VPC app subnet ->
# PSIGW/PIA and Oracle on the DEV VM.
#
# Fill the six variables, then:  ./deploy.sh gui   or   ./deploy.sh mcp
set -euo pipefail

PROJECT="${PROJECT:?set PROJECT}"
REGION="${REGION:?set REGION (must match the serverless NEG)}"
NETWORK="${NETWORK:?set NETWORK (shared VPC)}"
SUBNET="${SUBNET:?set SUBNET (e.g. the 10.60.10.0/24 app subnet)}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:?service account email}"
FILESTORE="${FILESTORE:-}"   # ip:/share for the /data mount; empty = stateless (dev only)

IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/pstb/pstb:$(git rev-parse --short HEAD)"

# Build from the REPO ROOT so .dockerignore applies and the Dockerfile
# path resolves; a --tag submit would look for Dockerfile at the root.
( cd "$(git rev-parse --show-toplevel)" &&
  gcloud builds submit --project "$PROJECT" \
    --config deploy/cloudrun/cloudbuild.yaml \
    --substitutions "_IMAGE=${IMAGE}" . )

COMMON=(
  --project "$PROJECT" --region "$REGION" --image "$IMAGE"
  --service-account "$SERVICE_ACCOUNT"
  --ingress internal-and-cloud-load-balancing
  --network "$NETWORK" --subnet "$SUBNET"
  --vpc-egress private-ranges-only
  # The app is intentionally single-instance: per-session turn locks,
  # the export job registry and activity polling are in-process. Scaling
  # out is not a tuning knob here; it is split-brain.
  --min-instances 1 --max-instances 1
  --no-cpu-throttling            # background export threads keep running
  --session-affinity
  --timeout 3600
  --memory 2Gi --cpu 2
)
VOLUME=()
if [ -n "$FILESTORE" ]; then
  VOLUME=(--add-volume "name=data,type=nfs,location=${FILESTORE}"
          --add-volume-mount "volume=data,mount-path=/data")
fi

case "${1:-}" in
  gui)
    gcloud run deploy pstb-gui "${COMMON[@]}" "${VOLUME[@]}" \
      --set-secrets "PSTB_AUTH_TOKEN=pstb-auth-token:latest,PSTB_OPERATOR_TOKEN=pstb-operator-token:latest,ORACLE_PASSWORD=pstb-oracle-password:latest" \
      --set-env-vars "PSTB_TRUSTED_PROXY=1,GOOGLE_CLOUD_PROJECT=${PROJECT}"
    ;;
  mcp)
    gcloud run deploy pstb-mcp "${COMMON[@]}" "${VOLUME[@]}" \
      --command pstb-mcp-http \
      --set-secrets "PSTB_MCP_TOKEN=pstb-mcp-token:latest,ORACLE_PASSWORD=pstb-oracle-password:latest" \
      --set-env-vars "GOOGLE_CLOUD_PROJECT=${PROJECT}"
    ;;
  *)
    echo "usage: $0 gui|mcp" >&2; exit 2 ;;
esac
