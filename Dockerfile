# =============================================================================
# Dockerfile — Enterprise API Testing Framework
#
# ─── FIX 3: Non-Deterministic Build Hardening ────────────────────────────────
#
# REMOVED: apt-get upgrade -y
#   WHY: `apt-get upgrade` pulls whatever package versions are current at
#   build time. A broken upstream Debian package will fail your CI pipeline
#   even if you changed zero lines of code. This is a reproducibility failure.
#
#   The correct mental model: the BASE IMAGE is the unit of security patching,
#   not individual packages inside a running build. When CVEs are found in
#   base packages, you update the pinned digest (see below) and rebuild.
#   This is done on a schedule (weekly/monthly), not on every test run.
#
# ADDED: SHA256 digest pin
#   The base image is pinned to an exact content-addressable digest.
#   `python:3.11-slim` is a mutable tag — it changes when Python pushes a
#   new patch release. The same tag resolves to a different image tomorrow.
#   A digest pin means the image is immutable. `docker pull` on the same
#   digest always produces the same bytes, on any machine, at any time.
#
# HOW TO UPDATE THE PIN (run this before bumping):
#   make pin-base-image
#   # or manually:
#   docker pull python:3.11-slim
#   docker inspect python:3.11-slim --format='{{index .RepoDigests 0}}'
#   # Replace the sha256:... below with the new digest.
#
# SECURITY PATCHING WORKFLOW:
#   1. A CVE scanner (Trivy, Snyk, Grype) runs weekly via a separate CI job.
#   2. If a critical CVE is found in the base image, a PR is opened that
#      bumps the SHA256 digest to the patched image version.
#   3. The PR triggers a full test run. If tests pass, the pin is merged.
#   This gives you both reproducibility AND a documented security patch trail.
#
# CURRENT PIN:
#   python:3.11-slim @ sha256 (pinned 2026-04-28)
#   Re-pin monthly or when a CVE is found. See Makefile target `pin-base-image`.
# =============================================================================

# Pinned to exact digest for reproducible, deterministic builds.
# To update: run `make pin-base-image` and replace the digest below.
# DO NOT use `python:3.11-slim` without a digest — mutable tags are not
# acceptable in a production CI pipeline.
FROM python:3.11-slim@sha256:31e4d5c21d4ee4b72fd61a53f2e67b9f2430dced28a6ae91be5b5bedf3f7e0e6 AS base

# ─── Metadata labels ───────────────────────────────────────────────────────
LABEL org.opencontainers.image.description="Enterprise API Testing Framework" \
      org.opencontainers.image.base.name="python:3.11-slim" \
      org.opencontainers.image.source="https://github.com/your-org/api-testing-framework"

# ─── System packages: install ONLY what is needed, nothing more ────────────
# We do NOT run apt-get upgrade. Security patching is handled by updating
# the base image digest (see header comment above). Running upgrade here:
#   1. Breaks build reproducibility (package versions are non-deterministic)
#   2. Can pull in broken packages from upstream mirrors
#   3. Is NOT a substitute for a proper base image update workflow
#
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /tmp/* /var/tmp/*


# =============================================================================
# Dependency installation stage
# Separated from the runtime stage so that:
#   - Source code changes don't bust the pip cache layer
#   - Only installed packages (not build tools) reach the final image
# =============================================================================
FROM base AS deps

WORKDIR /install

# Copy only the requirements file. Docker caches this layer until the file
# changes — a test code change does NOT re-run pip install.
COPY requirements.txt .

RUN pip install --upgrade pip --no-cache-dir \
    && pip install --no-cache-dir -r requirements.txt


# =============================================================================
# Runtime stage — the image that actually runs tests
# =============================================================================
FROM base AS runtime

# ─── Non-root user ───────────────────────────────────────────────────────────
# Running as root in a container is rejected by:
#   - Kubernetes PodSecurityPolicy (securityContext.runAsNonRoot: true)
#   - OPA Gatekeeper / Kyverno cluster policies
#   - CIS Docker Benchmark (Rule 4.1)
# uid/gid 1001 avoids conflicts with common system accounts.
RUN groupadd --gid 1001 testrunner \
    && useradd --uid 1001 --gid testrunner \
               --shell /bin/bash \
               --create-home \
               testrunner

WORKDIR /app

# Copy installed Python packages from the deps stage (no re-install)
COPY --from=deps /usr/local/lib/python3.11/site-packages \
                 /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

# Copy application source, owned by the non-root user
COPY --chown=testrunner:testrunner . .

# Create output directories with correct ownership BEFORE switching users
RUN mkdir -p logs reports allure-results \
    && chown -R testrunner:testrunner logs reports allure-results

# Switch to non-root user for all subsequent operations
USER testrunner

# Declare output directories as volumes (docker-compose bind-mounts these)
VOLUME ["/app/logs", "/app/reports"]

# ─── Health check ────────────────────────────────────────────────────────────
# Validates the runtime environment before the container is marked healthy.
# Checks that all critical imports resolve — catches broken installs early.
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c \
        "import pytest, requests, pydantic, structlog, tenacity, faker; print('OK')" \
    || exit 1

# ─── Default command ─────────────────────────────────────────────────────────
# Override in docker-compose or CI: pytest -m smoke, pytest -m crud, etc.
CMD ["python", "-m", "pytest", "--tb=short", "-v"]
