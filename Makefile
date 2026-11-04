# Developer commands. SRS section 11.4 defines the deployment targets.

.DEFAULT_GOAL := help

PROTO_DIR := shared/protobuf
CONTRACTS_BASELINE ?= origin/main

# Targets a later module delivers, as <target>:<module>. They fail until then, so a
# command that does nothing yet can never look like it passed.
PLANNED_TARGETS := \
	images:M1.6 \
	secrets:M1.7 \
	infra:M1.7 \
	clean:M1.7 \
	logs:M1.9 \
	services:M1.10 \
	migrate:M2.5 \
	identity-seed:M3.1 \
	eval:M12.8 \
	chaos:M13.2 \
	seed-demo:M14.5 \
	smoke-test:M14.5

.PHONY: help install lint format typecheck test audit contracts check $(foreach item,$(PLANNED_TARGETS),$(firstword $(subst :, ,$(item))))

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

check: lint typecheck contracts test ## Run every check required before a pull request

audit: ## Check locked dependencies for known vulnerabilities
	uv export --quiet --locked --all-groups --no-emit-workspace --format requirements-txt --output-file .venv/audit-requirements.txt
	uv run pip-audit --strict --disable-pip --requirement .venv/audit-requirements.txt

define planned_target
$(firstword $(subst :, ,$(1))):
	@echo "make $(firstword $(subst :, ,$(1))) is not built yet. Module $(lastword $(subst :, ,$(1))) delivers it (docs/phases/README.md)." >&2
	@exit 1
endef

$(foreach item,$(PLANNED_TARGETS),$(eval $(call planned_target,$(item))))
