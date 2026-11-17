SHELL := /bin/bash
.DEFAULT_GOAL := help

VENV        ?= .venv
PY          := $(VENV)/bin/python
TF_DIRS     := $(shell find providers -name '*.tf' -not -path '*/.terraform/*' -exec dirname {} \; | sort -u)
PY_PATHS    := core providers/aws/functions
SH_SCRIPTS  := $(shell find core/scripts -name '*.sh' 2>/dev/null)

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
	@if [ -n "$(SH_SCRIPTS)" ]; then shellcheck $(SH_SCRIPTS); fi

.PHONY: format
format: ## Auto-format Python and Terraform
	$(VENV)/bin/ruff check --fix .
	$(VENV)/bin/ruff format .
	terraform fmt -recursive providers

.PHONY: typecheck
typecheck: ## Run mypy
	@paths="$$(for p in $(PY_PATHS); do find $$p -name '*.py' -print -quit | grep -q . && echo $$p; done)"; \
	  if [ -n "$$paths" ]; then $(VENV)/bin/mypy $$paths; else echo "no Python files to type-check"; fi

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
security: ## IaC security scan
	$(VENV)/bin/checkov -d providers --config-file .checkov.yaml

.PHONY: boundaries
boundaries: ## Ensure core/ never imports cloud SDKs
	@! grep -rnE '^\s*(import|from)\s+(boto3|botocore|azure)' core --include='*.py' || (echo "core/ must not import cloud SDKs" && exit 1)
	@echo "core/ boundary OK"

.PHONY: check
check: lint typecheck boundaries test validate security ## Run every check (same as CI)
