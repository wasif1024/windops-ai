#!/usr/bin/env bash
# Run WindOps locally, the backend and the frontend together, to see changes as they are made.
#
#   scripts/dev.sh              # or: make dev
#   scripts/dev.sh --rebuild    # after pulling backend changes: rebuild images and redeploy
#   scripts/dev.sh --trust-ca   # macOS: trust the cluster's local CA, so the browser shows no warning
#
# It installs missing Python and Node packages, starts the Minikube cluster, deploys and seeds
# WindOps the first time, then runs the gateway and the Vite dev server until Ctrl+C.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
LOG_DIR="${TMPDIR:-/tmp}/windops-dev"
REBUILD=0
TRUST_CA=0

usage() {
  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
}

for argument in "$@"; do
  case "$argument" in
    --rebuild) REBUILD=1 ;;
    --trust-ca) TRUST_CA=1 ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() {
  printf '\033[31mdev.sh: %s\033[0m\n' "$*" >&2
  exit 1
}
need() { command -v "$1" >/dev/null 2>&1 || fail "$1 is not installed. $2"; }
port_in_use() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
kube() { kubectl --context "$CLUSTER" "$@"; }

step "Checking the tools"
need docker "Install Docker Desktop: https://docs.docker.com/desktop/"
need minikube "Install it with: brew install minikube"
need kubectl "Install it with: brew install kubectl"
need make "Install the Xcode command line tools: xcode-select --install"
need uv "Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh"
need node "Install Node.js 24 or later: https://nodejs.org"
need npm "It comes with Node.js: https://nodejs.org"
need lsof "It comes with macOS and most Linux distributions"
node_major="$(node -p 'process.versions.node.split(".")[0]')"
[ "$node_major" -ge 24 ] || fail "Node.js 24 or later is needed, and this is $(node --version)."
docker info >/dev/null 2>&1 || fail "Docker is not running. Start Docker Desktop, then run this again."
echo "All present."

step "Settings"
command -v python3 >/dev/null 2>&1 || fail "python3 is not installed. It comes with the Xcode command line tools."
# Your .env, made from .env.example the first time. New settings are added, and your values kept.
python3 scripts/settings.py --ensure-env
setting() { python3 "$ROOT/scripts/settings.py" "$1"; }
CLUSTER="$(setting WINDOPS_KUBE_CONTEXT)"
PUBLIC_URL="$(setting WINDOPS_PUBLIC_URL)"
FRONTEND_PORT="$(setting WINDOPS_DEV_PORT)"
GATEWAY_PORT="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.urlparse(sys.argv[1]).port or 443)' "$PUBLIC_URL")"

step "Python packages"
# Exact versions from uv.lock. Quick when nothing changed.
uv sync --locked --quiet
echo "Up to date."

step "Frontend packages"
if [ ! -d frontend/node_modules ] || [ frontend/package-lock.json -nt frontend/node_modules/.package-lock.json ]; then
  (cd frontend && npm ci --no-audit --no-fund)
else
  echo "Up to date."
fi

step "The Minikube cluster"
if [ "$(minikube status -p "$CLUSTER" --format '{{.Host}}' 2>/dev/null || true)" != "Running" ]; then
  make cluster
else
  echo "Running."
fi

restart_workloads() {
  # Images keep the tag :local, so a rebuilt one is picked up only by a restart.
  for namespace in windops-system windops-application; do
    kube rollout restart deployment --namespace="$namespace" >/dev/null
  done
}

seed() {
  make identity-seed
  make authorization-seed
  make tenant-seed
}

if ! kube get deployment windops-web-bff --namespace=windops-system >/dev/null 2>&1; then
  step "First run: deploying WindOps (this takes a while)"
  make secrets
  make infra
  make images
  make migrate
  make services
  make gateway-routes
  seed
elif [ "$REBUILD" -eq 1 ]; then
  step "Rebuilding the images and redeploying"
  make images
  make migrate
  make services
  restart_workloads
fi

step "Waiting for WindOps to be ready"
for namespace in windops-system windops-application; do
  for deployment in $(kube get deployment --namespace="$namespace" --output=name); do
    kube rollout status "$deployment" --namespace="$namespace" --timeout=300s >/dev/null ||
      fail "$deployment in $namespace is not ready. See: kubectl --context $CLUSTER -n $namespace describe $deployment"
  done
done
# The settings from .env, which restarts what reads them if any changed.
make --no-print-directory config
# Also re-sends the gateway's certificate, which changes when cert-manager renews it.
make --no-print-directory gateway-routes >/dev/null
if [ "$REBUILD" -eq 1 ]; then seed; fi
echo "Ready."

for port in "$GATEWAY_PORT" "$FRONTEND_PORT"; do
  if port_in_use "$port"; then
    fail "Port $port is in use. Stop what uses it (lsof -nP -iTCP:$port -sTCP:LISTEN), then run this again."
  fi
done

CA_FILE="$(PYTHONPATH=scripts uv run --quiet python scripts/dev_info.py ca)"

if [ "$TRUST_CA" -eq 1 ]; then
  step "Trusting the cluster's local CA"
  if [ "$(uname)" != "Darwin" ]; then
    echo "Only on macOS. Elsewhere, add $CA_FILE to your browser's trusted authorities yourself."
  else
    echo "This adds WindOps' local CA to your login keychain, so Chrome and Safari trust $PUBLIC_URL."
    echo "It is made by your own cluster. Remove it in Keychain Access, under 'windops', when you no longer need it."
    printf 'Trust it? [y/N] '
    read -r answer
    if [ "$answer" = "y" ] || [ "$answer" = "Y" ]; then
      security add-trusted-cert -r trustRoot -k "$HOME/Library/Keychains/login.keychain-db" "$CA_FILE" &&
        echo "Trusted. Restart your browser to pick it up."
    else
      echo "Skipped."
    fi
  fi
fi

mkdir -p "$LOG_DIR"
GATEWAY_LOG="$LOG_DIR/gateway.log"
FORWARD_PID=""
VITE_PID=""

stop() {
  trap - EXIT INT TERM
  printf '\nStopping...\n'
  for pid in $VITE_PID $FORWARD_PID; do
    kill "$pid" 2>/dev/null || true
    pkill -P "$pid" 2>/dev/null || true
  done
  # Whatever else forwards the gateway's port for this script, such as one the loop just restarted.
  pkill -f "port-forward --namespace=windops-system service/windops-gateway $GATEWAY_PORT:443" 2>/dev/null || true
  wait 2>/dev/null || true
  echo "Stopped. The cluster keeps running: make cluster-stop frees its memory."
}
trap stop EXIT INT TERM

step "Starting the gateway at $PUBLIC_URL"
# A forward ends when its pod restarts, so it starts again by itself.
(
  trap 'exit 0' INT TERM
  while true; do
    kube port-forward --namespace=windops-system service/windops-gateway "$GATEWAY_PORT:443" >>"$GATEWAY_LOG" 2>&1 || true
    sleep 1
  done
) &
FORWARD_PID=$!
for _ in $(seq 1 30); do
  if curl --silent --fail --cacert "$CA_FILE" "$PUBLIC_URL/api/v1/public/branding/north-sea" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl --silent --fail --cacert "$CA_FILE" "$PUBLIC_URL/api/v1/public/branding/north-sea" >/dev/null ||
  fail "The gateway does not answer. See $GATEWAY_LOG"

step "Starting the frontend at http://localhost:$FRONTEND_PORT"
(
  cd frontend
  # Node trusts the cluster's CA for the API proxy, and the app finds Keycloak at the gateway.
  NODE_EXTRA_CA_CERTS="$CA_FILE" VITE_AUTH_ORIGIN="$PUBLIC_URL" VITE_API_TARGET="$PUBLIC_URL" \
    VITE_KEYCLOAK_REALM="$(setting WINDOPS_KEYCLOAK_REALM)" VITE_WEB_CLIENT_ID="$(setting WINDOPS_WEB_CLIENT_ID)" \
    exec npm run dev -- --clearScreen false --port "$FRONTEND_PORT"
) &
VITE_PID=$!

sleep 2
cat <<EOF

$(printf '\033[1m')WindOps is running.$(printf '\033[0m')

  Frontend, live reload   http://localhost:$FRONTEND_PORT/?tenant=north-sea
  As deployed             $PUBLIC_URL/?tenant=north-sea
  Grafana                 make observability-forward, then http://127.0.0.1:3000

Demo users (local only). See their passwords with: make demo-logins
Administrators set up an authenticator app at their first sign-in.
$(PYTHONPATH=scripts uv run --quiet python scripts/dev_info.py logins)

Sign-in goes to Keycloak at $PUBLIC_URL. Unless you ran with --trust-ca, the
browser warns about its certificate once: choose Advanced, then Proceed to localhost.

Frontend changes show at once. After backend changes: scripts/dev.sh --rebuild
Press Ctrl+C to stop.
EOF

wait "$VITE_PID"
