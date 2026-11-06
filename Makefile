# Developer commands. SRS section 11.4 defines the deployment targets.

.DEFAULT_GOAL := help

PROTO_DIR := shared/protobuf
CONTRACTS_BASELINE ?= origin/main
# Commands name this profile and context directly, so they cannot reach another cluster.
CLUSTER := windops
OVERLAY ?= local
KUBERNETES_DIR := infrastructure/kubernetes

# Targets a later module delivers, as <target>:<module>. They fail until then, so a
# command that does nothing yet can never look like it passed.
PLANNED_TARGETS := \
	eval:M12.8 \
	chaos:M13.2 \
	seed-demo:M14.5 \
	smoke-test:M14.5

.PHONY: help install lint format typecheck test audit contracts contracts-gen check logs services sample-check phase-1-gate service images scaffold-check cluster cluster-stop cluster-delete cluster-check infra secrets clean gateway gateway-routes gateway-forward gateway-check observability-forward observability-check postgres-provision postgres-check redis-check qdrant-check nats-check migrate migration migrate-check outbox-check audit-check events-check dead-letters phase-2-gate identity-seed identity-check mfa-check mtls-check data-tls-check authorization-seed authorization-check identity-service-check tenant-seed tenant-check cross-tenant-check edge-check openapi bff-check gateway-policy-check token-exchange-check frontend-test frontend-check $(foreach item,$(PLANNED_TARGETS),$(firstword $(subst :, ,$(item))))

help: ## Show the available commands
	@grep -E '^[a-z-]+:.*## ' $(firstword $(MAKEFILE_LIST)) | sed -E 's/:.*## /|/' | awk -F'|' '{printf "  %-12s %s\n", $$1, $$2}'
	@echo "Planned:"
	@for item in $(PLANNED_TARGETS); do printf "  %-14s %s\n" "$${item%%:*}" "$${item##*:}"; done

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
	uv run python scripts/generate_contracts.py

check: lint typecheck contracts test ## Run every check required before a pull request

audit: ## Check locked dependencies for known vulnerabilities
	uv export --quiet --locked --all-groups --no-emit-workspace --format requirements-txt --output-file .venv/audit-requirements.txt
	uv run pip-audit --strict --disable-pip --requirement .venv/audit-requirements.txt

service: ## Create a service from the template: make service NAME=telemetry-service
	@test -n "$(NAME)" || { echo "Usage: make service NAME=telemetry-service" >&2; exit 1; }
	uv run python scripts/new_service.py "$(NAME)" $(if $(DATABASE),--database,)

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
	uv run python scripts/verify_scaffold.py $(SCAFFOLD_ARGS)

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
	uv run python scripts/verify_cluster.py

infra: ## Create the namespaces, cert-manager, etcd, data stores, Keycloak and observability
	kubectl --context $(CLUSTER) apply --kustomize $(KUBERNETES_DIR)/base/namespaces
	uv run python scripts/provision_pki.py
	kubectl --context $(CLUSTER) apply --kustomize infrastructure/gateway/etcd
	kubectl --context $(CLUSTER) apply --kustomize $(KUBERNETES_DIR)/base/data
	kubectl --context $(CLUSTER) apply --kustomize $(KUBERNETES_DIR)/base/observability
	uv run python scripts/provision_postgres.py
	PYTHONPATH=scripts uv run python scripts/provision_nats.py
	@# After PostgreSQL has Keycloak's database and login.
	kubectl --context $(CLUSTER) apply --kustomize infrastructure/security/keycloak

migrate: ## Run each service's migration Job, once per release, before make services
	uv run python scripts/run_migrations.py

migration: ## Add a migration: make migration PACKAGE=windops_audit MESSAGE="add records"
	@test -n "$(PACKAGE)" -a -n "$(MESSAGE)" || { echo 'Usage: make migration PACKAGE=windops_audit MESSAGE="add records"' >&2; exit 1; }
	uv run python -m windops_migrations new "$(PACKAGE)" --message "$(MESSAGE)"

migrate-check: ## Prove migration Jobs work on the cluster, using a throwaway service
	PYTHONPATH=scripts uv run python scripts/verify_migrations.py

outbox-check: ## Prove state and events commit together, against real PostgreSQL and JetStream
	PYTHONPATH=scripts uv run python scripts/verify_outbox.py

audit-check: ## Prove the Audit Service keeps a tamper-evident record on the cluster
	PYTHONPATH=scripts uv run python scripts/verify_audit.py

events-check: ## Prove the event metrics, dashboards and alerts work, using real failures
	PYTHONPATH=scripts uv run python scripts/verify_events.py

dead-letters: ## List the messages in the dead-letter stream, without their contents
	PYTHONPATH=scripts uv run python scripts/dead_letters.py

identity-seed: ## Create the Keycloak realm, tenants, client and demo users
	PYTHONPATH=scripts uv run python scripts/identity_seed.py

identity-check: ## Prove Keycloak's login, tenant claim and rules on the cluster
	PYTHONPATH=scripts uv run python scripts/verify_identity.py

mfa-check: ## Prove MFA and step-up on the cluster, which takes about 7 minutes
	PYTHONPATH=scripts uv run python scripts/verify_mfa.py

mtls-check: ## Prove mutual TLS on the cluster, including a real certificate renewal
	PYTHONPATH=scripts uv run python scripts/verify_mtls.py

authorization-seed: ## Give the demo users their first roles, after make identity-seed
	PYTHONPATH=scripts uv run python scripts/authorization_seed.py

authorization-check: ## Prove roles, scopes, break-glass and audited denials on the cluster
	PYTHONPATH=scripts uv run python scripts/verify_authorization.py

identity-service-check: ## Prove user management, sessions and MFA status on the cluster
	PYTHONPATH=scripts uv run python scripts/verify_identity_service.py

tenant-seed: ## Record the demo tenants in the Tenant Service, after make identity-seed
	PYTHONPATH=scripts uv run python scripts/tenant_seed.py

tenant-check: ## Prove tenants, branding and row-level security on the cluster
	PYTHONPATH=scripts uv run python scripts/verify_tenant.py

cross-tenant-check: ## Attack every data path across tenants, in PostgreSQL and each API
	PYTHONPATH=scripts uv run python scripts/verify_cross_tenant.py

edge-check: ## Prove the gateway serves only HTTPS, and exposes only Keycloak's sign-in pages
	PYTHONPATH=scripts uv run python scripts/verify_edge.py

openapi: ## Write Web BFF's OpenAPI document to docs/api
	uv run python scripts/export_openapi.py

bff-check: ## Prove Web BFF on the cluster, with a real login through the gateway
	PYTHONPATH=scripts uv run python scripts/verify_web_bff.py

gateway-policy-check: ## Prove the gateway's token checks, CORS and per-address limits
	PYTHONPATH=scripts uv run python scripts/verify_gateway_policy.py

token-exchange-check: ## Prove token exchange for agents with a real login (SEC-008)
	PYTHONPATH=scripts uv run python scripts/verify_token_exchange.py

frontend-test: ## Lint, type-check, test and build the frontend, as CI does
	cd frontend && npm ci --no-audit --no-fund && npm run lint && npm run typecheck && npm test && npm run build && npm audit --audit-level=moderate

frontend-check: ## Sign in through a real browser, and see the branded, role-aware shell
	PYTHONPATH=scripts uv run python scripts/verify_frontend.py

data-tls-check: ## Prove every data store accepts only TLS, including a real renewal
	PYTHONPATH=scripts uv run python scripts/verify_data_tls.py

postgres-provision: ## Create each service's PostgreSQL database and login
	uv run python scripts/provision_postgres.py

postgres-check: ## Prove PostgreSQL isolates services and keeps data on the cluster
	PYTHONPATH=scripts uv run python scripts/verify_postgres.py

redis-check: ## Prove Redis isolates services and handles an outage on the cluster
	PYTHONPATH=scripts uv run python scripts/verify_redis.py

qdrant-check: ## Prove Qdrant needs a key and keeps its vectors on the cluster
	PYTHONPATH=scripts uv run python scripts/verify_qdrant.py

nats-check: ## Prove JetStream keeps events, limits publishers and dead-letters on the cluster
	PYTHONPATH=scripts uv run python scripts/verify_nats.py

secrets: ## Create local secrets from an ignored file, generating it the first time
	uv run python scripts/local_secrets.py

observability-forward: ## Reach Grafana at http://127.0.0.1:3000
	kubectl --context $(CLUSTER) port-forward --namespace=windops-observability service/windops-grafana 3000:3000

observability-check: ## Prove logs, traces, metrics and dashboards work on the cluster
	PYTHONPATH=scripts uv run python scripts/verify_observability.py

logs: ## Follow a workload's logs: make logs NAME=windops-telemetry
	@test -n "$(NAME)" || { echo "Usage: make logs NAME=windops-telemetry" >&2; exit 1; }
	kubectl --context $(CLUSTER) logs --all-namespaces --follow --selector=app.kubernetes.io/name=$(NAME)

services: ## Deploy the gateway and services, using the chosen overlay
	kubectl --context $(CLUSTER) apply --kustomize $(KUBERNETES_DIR)/overlays/$(OVERLAY)

sample-check: ## Prove two services work together on the cluster, through the gateway
	PYTHONPATH=scripts uv run python scripts/verify_sample.py

phase-1-gate: ## Check the Phase 1 exit gate on the cluster
	PYTHONPATH=scripts uv run python scripts/verify_phase_1_gate.py

phase-2-gate: ## Check the Phase 2 exit gate on the cluster
	PYTHONPATH=scripts uv run python scripts/verify_phase_2_gate.py

gateway: ## Deploy the API gateway
	kubectl --context $(CLUSTER) apply --kustomize services/api-gateway/deploy

gateway-routes: ## Apply the gateway's routes
	uv run python scripts/gateway_routes.py

gateway-forward: ## Reach the gateway at https://localhost:9443, the address Keycloak's pages use
	kubectl --context $(CLUSTER) port-forward --namespace=windops-system service/windops-gateway 9443:443

gateway-check: ## Prove the gateway works on the cluster, using a throwaway service
	PYTHONPATH=scripts uv run python scripts/verify_gateway.py

clean: ## Remove WindOps workloads and namespaces, keeping the cluster
	kubectl --context $(CLUSTER) delete --kustomize $(KUBERNETES_DIR)/overlays/$(OVERLAY) --ignore-not-found

define planned_target
$(firstword $(subst :, ,$(1))):
	@echo "make $(firstword $(subst :, ,$(1))) is not built yet. Module $(lastword $(subst :, ,$(1))) delivers it (docs/phases/README.md)." >&2
	@exit 1
endef

$(foreach item,$(PLANNED_TARGETS),$(eval $(call planned_target,$(item))))
