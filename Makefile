# Developer commands. SRS section 11.4 defines the deployment targets.

.DEFAULT_GOAL := help

PROTO_DIR := shared/protobuf
CONTRACTS_BASELINE ?= origin/main
# Commands name this profile and context directly, so they cannot reach another cluster. It is
# WINDOPS_KUBE_CONTEXT in .env, and windops when that is unset.
CLUSTER := $(or $(shell python3 scripts/lib/settings.py WINDOPS_KUBE_CONTEXT 2>/dev/null),windops)
OVERLAY ?= local
KUBERNETES_DIR := infrastructure/kubernetes

# Targets a later module delivers, as <target>:<module>. They fail until then, so a
# command that does nothing yet can never look like it passed.
PLANNED_TARGETS := \
	eval:M12.8 \
	chaos:M13.2 \
	seed-demo:M14.5 \
	smoke-test:M14.5

.PHONY: help dev demo-logins config install lint format typecheck test audit contracts contracts-gen check logs services sample-check phase-1-gate service images scaffold-check cluster cluster-stop cluster-delete cluster-check infra secrets clean gateway gateway-routes gateway-forward gateway-check observability-forward observability-check postgres-provision postgres-check redis-check qdrant-check nats-check migrate migration migrate-check outbox-check audit-check events-check dead-letters phase-2-gate phase-3-gate route-sweep identity-seed identity-check mfa-check mtls-check data-tls-check authorization-seed authorization-check identity-service-check tenant-seed tenant-check cross-tenant-check edge-check openapi bff-check gateway-policy-check token-exchange-check frontend-test frontend-check admin-api-check platform-api-check security-check asset-seed asset-check telemetry-devices telemetry-check $(foreach item,$(PLANNED_TARGETS),$(firstword $(subst :, ,$(item))))

help: ## Show the available commands
	@grep -E '^[a-z-]+:.*## ' $(firstword $(MAKEFILE_LIST)) | sed -E 's/:.*## /|/' | awk -F'|' '{printf "  %-12s %s\n", $$1, $$2}'
	@echo "Planned:"
	@for item in $(PLANNED_TARGETS); do printf "  %-14s %s\n" "$${item%%:*}" "$${item##*:}"; done

dev: ## Run the backend and frontend together, installing and deploying what is missing
	./scripts/dev/dev.sh

demo-logins: ## Show the local demo users' passwords, by the key make dev prints beside each email
	@kubectl --context $(CLUSTER) get secret windops-identity-demo-users --namespace=windops-system \
		--output=go-template='{{range $$key, $$value := .data}}{{printf "  %-20s %s\n" $$key ($$value | base64decode)}}{{end}}'

install: ## Install the exact locked development tools
	uv sync --locked

lint: ## Check lint rules and formatting without changing files
	uv run ruff check .
	uv run ruff format --check .

format: ## Apply safe lint fixes and formatting
	uv run ruff check --fix .
	uv run ruff format .

typecheck: ## Run strict type checks
	uv run mypy

test: ## Run the tests
	uv run pytest

contracts: ## Lint Protobuf contracts and check for breaking changes
	buf lint $(PROTO_DIR)
	buf format --diff --exit-code $(PROTO_DIR)
	@git rev-parse --verify --quiet "$(CONTRACTS_BASELINE)^{commit}" >/dev/null || \
		{ echo "Baseline $(CONTRACTS_BASELINE) not found. Run: git fetch origin main" >&2; exit 1; }
	@if git cat-file -e "$(CONTRACTS_BASELINE):$(PROTO_DIR)/buf.yaml" 2>/dev/null; then \
		buf breaking $(PROTO_DIR) --against ".git#ref=$(CONTRACTS_BASELINE),subdir=$(PROTO_DIR)"; \
	else \
		echo "No contracts in $(CONTRACTS_BASELINE) yet, so there is nothing to compare."; \
	fi

contracts-gen: ## Generate Python code from the Protobuf contracts
	uv run python -m scripts.generate.contracts

check: lint typecheck contracts test ## Run every check required before a pull request

audit: ## Check locked dependencies for known vulnerabilities
	uv export --quiet --locked --all-groups --no-emit-workspace --format requirements-txt --output-file .venv/audit-requirements.txt
	uv run pip-audit --strict --disable-pip --requirement .venv/audit-requirements.txt

service: ## Create a service from the template: make service NAME=telemetry-service
	@test -n "$(NAME)" || { echo "Usage: make service NAME=telemetry-service" >&2; exit 1; }
	uv run python -m scripts.dev.new_service "$(NAME)" $(if $(DATABASE),--database,)

images: ## Build an image for every service, into the cluster's image store
	@eval "$$(minikube -p $(CLUSTER) docker-env --shell bash)"; \
	for dockerfile in services/*/Dockerfile; do \
		[ -f "$$dockerfile" ] || { echo "No services to build yet."; exit 0; }; \
		service="$$(basename "$$(dirname "$$dockerfile")")"; \
		name="windops-$${service%-service}"; \
		echo "Building $$name:local"; \
		docker build -q -f "$$dockerfile" -t "$$name:local" . || exit 1; \
	done; \
	echo "Building windops-frontend:local"; \
	docker build -q -t windops-frontend:local frontend || exit 1; \
	for dockerfile in infrastructure/docker/*/Dockerfile; do \
		name="windops-$$(basename "$$(dirname "$$dockerfile")")"; \
		echo "Building $$name:local"; \
		docker build -q -t "$$name:local" "$$(dirname "$$dockerfile")" || exit 1; \
	done

scaffold-check: ## Generate, build and run a throwaway service to prove the template works
	uv run python -m scripts.checks.foundation.scaffold $(SCAFFOLD_ARGS)

cluster: ## Start the local cluster, with SRS 11.4 settings for a 16 GB laptop
	@# Restarting a stopped cluster switches kubectl's context, despite --keep-context, so restore it.
	@previous="$$(kubectl config current-context 2>/dev/null || true)"; \
	minikube start -p $(CLUSTER) --driver=docker --container-runtime=docker \
		--kubernetes-version=v1.35.1 --cpus=6 --memory=10240 --cni=calico --keep-context || exit 1; \
	minikube -p $(CLUSTER) addons enable metrics-server || exit 1; \
	if [ -n "$$previous" ]; then kubectl config use-context "$$previous" >/dev/null; fi

cluster-stop: ## Stop the local cluster and free its memory, keeping its data
	minikube stop -p $(CLUSTER) --keep-context-active

cluster-delete: ## Delete the local cluster and all its data
	minikube delete -p $(CLUSTER)

cluster-check: ## Deploy a throwaway service to prove the manifests work on the cluster
	uv run python -m scripts.checks.foundation.cluster

infra: ## Create the namespaces, cert-manager, etcd, data stores, Keycloak and observability
	kubectl --context $(CLUSTER) apply --kustomize $(KUBERNETES_DIR)/base/namespaces
	uv run python -m scripts.setup.apply_settings
	uv run python -m scripts.setup.provision_pki
	kubectl --context $(CLUSTER) apply --kustomize infrastructure/gateway/etcd
	kubectl --context $(CLUSTER) apply --kustomize $(KUBERNETES_DIR)/base/data
	kubectl --context $(CLUSTER) apply --kustomize $(KUBERNETES_DIR)/base/observability
	uv run python -m scripts.setup.provision_postgres
	uv run python -m scripts.setup.provision_nats
	@# After PostgreSQL has Keycloak's database and login.
	kubectl --context $(CLUSTER) apply --kustomize infrastructure/security/keycloak

migrate: ## Run each service's migration Job, once per release, before make services
	uv run python -m scripts.setup.run_migrations

migration: ## Add a migration: make migration PACKAGE=windops_audit MESSAGE="add records"
	@test -n "$(PACKAGE)" -a -n "$(MESSAGE)" || { echo 'Usage: make migration PACKAGE=windops_audit MESSAGE="add records"' >&2; exit 1; }
	uv run python -m windops_migrations new "$(PACKAGE)" --message "$(MESSAGE)"

migrate-check: ## Prove migration Jobs work on the cluster, using a throwaway service
	uv run python -m scripts.checks.foundation.migrations

outbox-check: ## Prove state and events commit together, against real PostgreSQL and JetStream
	uv run python -m scripts.checks.data.outbox

audit-check: ## Prove the Audit Service keeps a tamper-evident record on the cluster
	uv run python -m scripts.checks.data.audit

events-check: ## Prove the event metrics, dashboards and alerts work, using real failures
	uv run python -m scripts.checks.data.events

dead-letters: ## List the messages in the dead-letter stream, without their contents
	uv run python -m scripts.dev.dead_letters

identity-seed: ## Create the Keycloak realm, tenants, client and demo users
	uv run python -m scripts.setup.identity_seed

identity-check: ## Prove Keycloak's login, tenant claim and rules on the cluster
	uv run python -m scripts.checks.identity.identity

mfa-check: ## Prove MFA and step-up on the cluster, which takes about 7 minutes
	uv run python -m scripts.checks.identity.mfa

mtls-check: ## Prove mutual TLS on the cluster, including a real certificate renewal
	uv run python -m scripts.checks.identity.mtls

authorization-seed: ## Give the demo users their first roles, after make identity-seed
	uv run python -m scripts.setup.authorization_seed

authorization-check: ## Prove roles, scopes, break-glass and audited denials on the cluster
	uv run python -m scripts.checks.identity.authorization

identity-service-check: ## Prove user management, sessions and MFA status on the cluster
	uv run python -m scripts.checks.identity.identity_service

tenant-seed: ## Record the demo tenants in the Tenant Service, after make identity-seed
	uv run python -m scripts.setup.tenant_seed

asset-seed: ## Record the synthetic demo wind farms, turbines and components, after make migrate
	uv run python -m scripts.setup.asset_seed

asset-check: ## Prove assets: isolation, scoped reads, changes and their events, on the cluster
	uv run python -m scripts.checks.fleet.asset

telemetry-devices: ## Give each demo turbine a device credential, kept in the simulator's Secret
	uv run python -m scripts.setup.telemetry_devices

telemetry-check: ## Prove telemetry ingestion: device credentials, the gateway route, storage
	uv run python -m scripts.checks.fleet.telemetry

tenant-check: ## Prove tenants, branding and row-level security on the cluster
	uv run python -m scripts.checks.identity.tenant

cross-tenant-check: ## Attack every data path across tenants, in PostgreSQL and each API
	uv run python -m scripts.checks.identity.cross_tenant

edge-check: ## Prove the gateway serves only HTTPS, and exposes only Keycloak's sign-in pages
	uv run python -m scripts.checks.edge.edge

openapi: ## Write Web BFF's OpenAPI document to docs/api
	uv run python -m scripts.generate.openapi

bff-check: ## Prove Web BFF on the cluster, with a real login through the gateway
	uv run python -m scripts.checks.edge.web_bff

gateway-policy-check: ## Prove the gateway's token checks, CORS and per-address limits
	uv run python -m scripts.checks.edge.gateway_policy

token-exchange-check: ## Prove token exchange for agents with a real login (SEC-008)
	uv run python -m scripts.checks.identity.token_exchange

frontend-test: ## Lint, type-check, test and build the frontend, as CI does
	cd frontend && npm ci --no-audit --no-fund && npm run lint && npm run typecheck && npm test && npm run build && npm audit --audit-level=moderate

frontend-check: ## Sign in through a real browser, and see the branded, role-aware shell
	uv run python -m scripts.checks.consoles.frontend

admin-api-check: ## Prove the administrator console's API through the gateway, with real logins
	uv run python -m scripts.checks.consoles.admin_api

platform-api-check: ## Prove the Platform console's API: tenants, branding views and break-glass
	uv run python -m scripts.checks.consoles.platform_api

security-check: ## Prove the Security dashboard and alerts have data, from real refusals
	uv run python -m scripts.checks.consoles.security

data-tls-check: ## Prove every data store accepts only TLS, including a real renewal
	uv run python -m scripts.checks.data.data_tls

postgres-provision: ## Create each service's PostgreSQL database and login
	uv run python -m scripts.setup.provision_postgres

postgres-check: ## Prove PostgreSQL isolates services and keeps data on the cluster
	uv run python -m scripts.checks.data.postgres

redis-check: ## Prove Redis isolates services and handles an outage on the cluster
	uv run python -m scripts.checks.data.redis_cache

qdrant-check: ## Prove Qdrant needs a key and keeps its vectors on the cluster
	uv run python -m scripts.checks.data.qdrant

nats-check: ## Prove JetStream keeps events, limits publishers and dead-letters on the cluster
	uv run python -m scripts.checks.data.nats_jetstream

secrets: ## Create local secrets from an ignored file, generating it the first time
	uv run python -m scripts.setup.local_secrets

observability-forward: ## Reach Grafana at http://127.0.0.1:3000
	kubectl --context $(CLUSTER) port-forward --namespace=windops-observability service/windops-grafana 3000:3000

observability-check: ## Prove logs, traces, metrics and dashboards work on the cluster
	uv run python -m scripts.checks.foundation.observability

logs: ## Follow a workload's logs: make logs NAME=windops-telemetry
	@test -n "$(NAME)" || { echo "Usage: make logs NAME=windops-telemetry" >&2; exit 1; }
	kubectl --context $(CLUSTER) logs --all-namespaces --follow --selector=app.kubernetes.io/name=$(NAME)

services: ## Deploy the gateway and services, using the chosen overlay
	uv run python -m scripts.setup.apply_settings
	kubectl --context $(CLUSTER) apply --kustomize $(KUBERNETES_DIR)/overlays/$(OVERLAY)

config: ## Put the WINDOPS_ settings from .env into the cluster, and restart what reads them
	uv run python -m scripts.setup.apply_settings

sample-check: ## Prove two services work together on the cluster, through the gateway
	uv run python -m scripts.checks.foundation.sample

phase-1-gate: ## Check the Phase 1 exit gate on the cluster
	uv run python -m scripts.checks.gates.phase_1

phase-2-gate: ## Check the Phase 2 exit gate on the cluster
	uv run python -m scripts.checks.gates.phase_2

phase-3-gate: ## Check the Phase 3 exit gate on the cluster: every check, about 40 minutes
	uv run python -m scripts.checks.gates.phase_3

route-sweep: ## Call every Web BFF route as callers who must be refused, and require each to be
	uv run python -m scripts.checks.edge.route_sweep

gateway: ## Deploy the API gateway
	kubectl --context $(CLUSTER) apply --kustomize services/api-gateway/deploy

gateway-routes: ## Apply the gateway's routes
	uv run python -m scripts.setup.gateway_routes

gateway-forward: ## Reach the gateway at https://localhost:9443, the address Keycloak's pages use
	kubectl --context $(CLUSTER) port-forward --namespace=windops-system service/windops-gateway 9443:443

gateway-check: ## Prove the gateway works on the cluster, using a throwaway service
	uv run python -m scripts.checks.foundation.gateway

clean: ## Remove WindOps workloads and namespaces, keeping the cluster
	kubectl --context $(CLUSTER) delete --kustomize $(KUBERNETES_DIR)/overlays/$(OVERLAY) --ignore-not-found

define planned_target
$(firstword $(subst :, ,$(1))):
	@echo "make $(firstword $(subst :, ,$(1))) is not built yet. Module $(lastword $(subst :, ,$(1))) delivers it (docs/phases/README.md)." >&2
	@exit 1
endef

$(foreach item,$(PLANNED_TARGETS),$(eval $(call planned_target,$(item))))
