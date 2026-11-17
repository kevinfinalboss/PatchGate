SHELL := /bin/bash
.DEFAULT_GOAL := help

VENV        ?= .venv
PY          := $(VENV)/bin/python
TF_DIRS     := $(shell find providers examples -name '*.tf' -not -path '*/.terraform/*' -exec dirname {} \; | sort -u)
SH_SCRIPTS  := $(shell find core/scripts providers/aws/scripts -name '*.sh' 2>/dev/null)
LIVE        := providers/aws/terraform/live
ACCOUNT     ?= dev
CHECKOV_VERSION := 3.4.5

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: setup
setup: ## Create the virtualenv and install dev dependencies
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -e '.[dev]'

.PHONY: lint
lint: ## Lint and format-check Python and Terraform
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .
	terraform fmt -check -recursive providers
	@if [ -n "$(SH_SCRIPTS)" ]; then \
	  if command -v shellcheck >/dev/null; then shellcheck $(SH_SCRIPTS); \
	  else docker run --rm -v "$$PWD:/mnt" -w /mnt koalaman/shellcheck:stable $(SH_SCRIPTS); fi; \
	fi

.PHONY: format
format: ## Auto-format Python and Terraform
	$(VENV)/bin/ruff check --fix .
	$(VENV)/bin/ruff format .
	terraform fmt -recursive providers

.PHONY: typecheck
typecheck: ## Run mypy (each Lambda separately: they all use handler.py)
	$(VENV)/bin/mypy core/lib core/tests core/scripts/linux providers/aws/tests
	@for fn in providers/aws/functions/*/; do echo "mypy $$fn"; $(VENV)/bin/mypy $$fn || exit 1; done

.PHONY: test
test: ## Run unit and contract tests
	$(VENV)/bin/pytest

.PHONY: validate
validate: ## terraform validate every root/module (no backend)
	@for d in $(TF_DIRS); do \
	  echo "==> $$d"; \
	  TF_DATA_DIR=.terraform-validate terraform -chdir=$$d init -backend=false -input=false >/dev/null && TF_DATA_DIR=.terraform-validate terraform -chdir=$$d validate -no-color || exit 1; \
	done

.PHONY: security
security: ## IaC security scan (same checkov version as CI)
	@if command -v docker >/dev/null; then \
	  docker run --rm -v "$$PWD:/tf" -w /tf bridgecrew/checkov:$(CHECKOV_VERSION) -d . --config-file .checkov.yaml; \
	else \
	  $(VENV)/bin/checkov -d . --config-file .checkov.yaml; \
	fi

.PHONY: boundaries
boundaries: ## Ensure core/ never imports cloud SDKs
	@! grep -rnE '^\s*(import|from)\s+(boto3|botocore|azure)' core --include='*.py' || (echo "core/ must not import cloud SDKs" && exit 1)
	@echo "core/ boundary OK"


.PHONY: seed
seed: ## Export the built-in catalog seeded into the configuration table (commit the result)
	$(PY) -m patchgate export --out providers/aws/config/seed.json

.PHONY: layer
layer: ## Build the patchgate-core Lambda layer (hash-pinned dependencies)
	providers/aws/functions/layer/build.sh

.PHONY: spoke-artifacts
spoke-artifacts: ## Regenerate SSM documents and the StackSet template from core/ (commit the result)
	$(PY) providers/aws/stacksets/generate.py

.PHONY: validate-catalog
validate-catalog: ## Validate catalog documents against the JSON Schemas
	$(PY) -m patchgate validate

.PHONY: generated
generated: ## Fail if generated artifacts (plans.json, SSM docs, StackSet) are stale
	$(PY) providers/aws/stacksets/generate.py --check

.PHONY: check
check: lint typecheck boundaries validate-catalog generated test validate security ## Run every check (same as CI)

# ------------------------------------------------------------------ AWS deployment
# Applies always show a plan and ask for confirmation (no -auto-approve).

.PHONY: hub
hub: layer ## Deploy the hub (control plane)
	terraform -chdir=$(LIVE)/hub init -backend-config=backend.hcl -input=false >/dev/null
	terraform -chdir=$(LIVE)/hub apply

.PHONY: spoke
spoke: ## Deploy the spoke into ACCOUNT (dev|prod): make spoke ACCOUNT=prod
	terraform -chdir=$(LIVE)/spoke init -backend-config=backend.hcl -input=false >/dev/null
	terraform -chdir=$(LIVE)/spoke workspace select -or-create $(ACCOUNT)
	terraform -chdir=$(LIVE)/spoke apply -var-file=accounts/$(ACCOUNT).tfvars

.PHONY: up
up: ## Create the ephemeral lab (instances bootstrap automatically)
	terraform -chdir=$(LIVE)/lab init -backend-config=backend.hcl -input=false >/dev/null
	terraform -chdir=$(LIVE)/lab apply

.PHONY: down
down: ## Destroy the lab, then report leftovers
	terraform -chdir=$(LIVE)/lab destroy
	./providers/aws/scripts/janitor.sh

.PHONY: janitor
janitor: ## List lab leftovers (DELETE=1 removes them)
	./providers/aws/scripts/janitor.sh $(if $(DELETE),--delete,)
