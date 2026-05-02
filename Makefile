# =============================================================================
# Makefile — Enterprise API Testing Framework
#
# Provides convenient wrappers for common operations.
# All targets are documented with `make help`.
# =============================================================================

.PHONY: help install test test-crud test-contract test-parallel \
        docker-build docker-run docker-clean \
        pin-base-image audit lint \
        clean

# Detect OS for open command
UNAME := $(shell uname -s)
OPEN  := $(if $(filter Darwin,$(UNAME)),open,xdg-open)

# ---------------------------------------------------------------------------
help:  ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | sort \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-28s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Local development
# ---------------------------------------------------------------------------
install:  ## Install all Python dependencies
	pip install -r requirements.txt

test:  ## Run the full test suite (single process)
	python -m pytest --tb=short -v

test-crud:  ## Run only CRUD tests
	python -m pytest -m crud -v

test-contract:  ## Run only contract tests
	python -m pytest -m contract -v

test-parallel:  ## Run tests in parallel (requires REDIS_URL to be set)
	@if [ -z "$$REDIS_URL" ]; then \
	  echo "⚠️  WARNING: REDIS_URL is not set. Circuit breaker will NOT be shared across workers."; \
	  echo "   Set REDIS_URL=redis://localhost:6379/0 before running parallel tests."; \
	  echo "   Run 'make docker-run' to use docker-compose which sets this automatically."; \
	fi
	python -m pytest -n auto --tb=short -v

logs:  ## Tail the structured JSON log
	@tail -f logs/test_run.jsonl | python -c \
	  "import sys,json; [print(json.dumps(json.loads(l), indent=2)) for l in sys.stdin]"

report:  ## Open the HTML test report
	$(OPEN) reports/report.html

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------
docker-build:  ## Build the Docker image locally
	docker build --target runtime -t api-testing-framework:local .

docker-run:  ## Run the full suite via docker-compose (Redis included)
	docker-compose up --build --abort-on-container-exit

docker-run-parallel:  ## Run tests in parallel mode (4 workers) via docker-compose
	docker-compose run --rm api-tests python -m pytest -n 4 --tb=short -v

docker-clean:  ## Remove containers, networks, and named volumes
	docker-compose down --volumes --remove-orphans
	docker rmi api-testing-framework:local 2>/dev/null || true

# ---------------------------------------------------------------------------
# Security & Quality
# ---------------------------------------------------------------------------

## ─── FIX 3: Correct base image pinning workflow ──────────────────────────
## Run this target to get the current SHA256 digest of python:3.11-slim.
## Copy the output and replace the digest in the FROM line of Dockerfile.
## Run on a schedule (monthly) or when a CVE scanner reports a base image hit.
pin-base-image:  ## Fetch and display the current python:3.11-slim SHA256 digest
	@echo "Fetching current digest for python:3.11-slim ..."
	@docker pull python:3.11-slim > /dev/null 2>&1 \
	  && docker inspect python:3.11-slim \
	       --format='FROM python:3.11-slim@{{index .RepoDigests 0 | printf "%s"}}' \
	  | sed 's|FROM python:3.11-slim@python:3.11-slim@|FROM python:3.11-slim@|' \
	  || (echo "Docker not available — get digest from:" \
	      && echo "  https://hub.docker.com/_/python/tags?name=3.11-slim")
	@echo ""
	@echo "Replace the FROM line in Dockerfile with the output above."
	@echo "Then commit: git commit -m 'chore: pin python:3.11-slim to <date>'"

audit:  ## Run pip-audit CVE scan against requirements.txt
	pip-audit -r requirements.txt --strict

lint:  ## Run basic syntax check on all Python files
	python -m py_compile $$(find . -name "*.py" \
	  -not -path "./__pycache__/*" \
	  -not -path "./.git/*")
	@echo "✅ All Python files compile cleanly."

# ---------------------------------------------------------------------------
clean:  ## Remove all generated files (logs, reports, __pycache__)
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -rf logs/ reports/ allure-results/ .pytest_cache/ .coverage
	@echo "✅ Clean complete."
