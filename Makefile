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
	migrate:M2.5 \
	identity-seed:M3.1 \
	eval:M12.8 \
	chaos:M13.2 \
	seed-demo:M14.5 \
	smoke-test:M14.5

.PHONY: help install lint format typecheck test audit contracts contracts-gen check logs services sample-check phase-1-gate service images scaffold-check cluster cluster-stop cluster-delete cluster-check infra secrets clean gateway gateway-routes gateway-forward gateway-check observability-forward observability-check $(foreach item,$(PLANNED_TARGETS),$(firstword $(subst :, ,$(item))))

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
	uv run python scripts/new_service.py "$(NAME)"

images: ## Build an image for every service, into the cluster's image store
	@eval "$$(minikube -p $(CLUSTER) docker-env --shell bash)"; \
	for dockerfile in services/*/Dockerfile; do \
		[ -f "$$dockerfile" ] || { echo "No services to build yet."; exit 0; }; \
		service="$$(basename "$$(dirname "$$dockerfile")")"; \
		name="windops-$${service%-service}"; \
		echo "Building $$name:local"; \
		docker build -q -f "$$dockerfile" -t "$$name:local" . || exit 1; \
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

infra: ## Create the namespaces, etcd and the observability stack. Phase 2 adds data stores
	kubectl --context $(CLUSTER) apply --kustomize $(KUBERNETES_DIR)/base/namespaces
	kubectl --context $(CLUSTER) apply --kustomize infrastructure/gateway/etcd
	kubectl --context $(CLUSTER) apply --kustomize $(KUBERNETES_DIR)/base/observability

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

gateway: ## Deploy the API gateway
	kubectl --context $(CLUSTER) apply --kustomize services/api-gateway/deploy

gateway-routes: ## Apply the gateway's routes
	uv run python scripts/gateway_routes.py

gateway-forward: ## Reach the gateway at http://127.0.0.1:9080
	kubectl --context $(CLUSTER) port-forward --namespace=windops-system service/windops-gateway 9080:80

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
