# Enterprise API Testing Framework — Portfolio Narrative

**Author:** Diego Alonso
**Target Role:** Analyst (with Python) / SDET / SRE
**Framework targets:** Restful-Booker (mock); architecture designed for Tier-1 financial APIs

---

## Why This Project Exists

Testing a REST API is trivial. Testing one at the reliability standard required by a
quantitative trading or financial data platform is an entirely different engineering problem.

The difference is not in the test assertions — it is in every layer surrounding them:
how failures are detected, how data integrity is guaranteed, how secrets are handled,
how the system behaves when the upstream is degraded, and how every event is recorded
so that post-incident analysis is possible without guesswork.

This framework was built to demonstrate that understanding, end-to-end.

---

## Architecture Decisions and Their Financial Systems Justification

### 1. Pydantic Models as API Contracts (`src/models/booking.py`)

Every API response is deserialized through a Pydantic v2 model before any
assertion runs. If a field is renamed, a type changes, or a required field
disappears, the test fails at deserialization with a precise `ValidationError`
— not a `KeyError` buried in an assertion three layers deep, not a silent
`None` that propagates through downstream calculations.

**Financial relevance:** In a trading system, a `price` field silently returning
`None` instead of `0.0` due to an API schema change, caught only after it nulls
out a position's valuation, is a Severity-1 incident. Contract testing at the
deserialization layer catches this class of regression immediately.

---

### 2. Smart Retry with 429 / Retry-After Awareness (`src/client/base_client.py`)

The retry logic distinguishes between two fundamentally different failure classes:

- **Transient infrastructure failures** (502, 503, 504, connection reset, timeout):
  Retry with exponential backoff — these will resolve on their own.
- **Logic errors** (400, 401, 403, 404): Hard fail immediately. These are
  deterministic. Retrying a 401 is not resilience; it is noise that masks
  the real problem and wastes time.
- **Rate limiting** (429 with `Retry-After`): Sleep exactly the server-specified
  duration before retrying. The sleep is capped at a configured maximum to
  prevent a misconfigured server from issuing an indefinitely long sleep — a
  subtle denial-of-service vector in shared environments.

This distinction matters because conflating these classes is the primary source
of "flaky" tests in production test suites. A test that retries a 401 fourteen
times and eventually times out is not flaky — it is incorrectly implemented.

**Financial relevance:** Market data APIs, broker APIs, and order management APIs
all enforce strict rate limits. A test suite that ignores `Retry-After` will have
its IP banned, will corrupt rate-limit counters shared with production traffic, and
will generate false-positive failures that erode trust in the entire test pipeline.

---

### 3. Circuit Breaker (`src/utils/circuit_breaker.py`)

After a configurable number of consecutive failures, the circuit transitions to
`OPEN` and all subsequent calls fail immediately without reaching the network.
After a recovery timeout, one probe request is allowed through (HALF-OPEN state).

**Financial relevance:** If an upstream API is degraded, a test suite without a
circuit breaker will execute every test, every retry, against a struggling service.
This amplifies the incident. In a financial system, this is called a "thundering
herd" — it turns a degraded upstream into a fully downed one. The circuit breaker
is the test suite's contribution to system-wide stability during an outage.

---

### 4. Structured JSON Logging with Secret Masking (`src/utils/logger.py`)

Every log line is a JSON object. No plain text. Every line carries:
- `run_id`: unique per test-runner process invocation
- `correlation_id`: unique per HTTP request, injected as `X-Correlation-ID` header
- `timestamp` in ISO 8601 UTC
- Masked sensitive headers (`Authorization`, `X-API-Key`, `Cookie`) — values
  are replaced with `***REDACTED***` by a structlog processor before the log
  event reaches any sink

**Financial relevance:** A plain-text log that leaks an `Authorization: Bearer`
token is not just a quality problem — it is a security incident. In a regulated
environment, that token appearing in a log file may constitute a compliance
violation. The masking processor runs unconditionally; there is no code path that
allows a credential to reach a log file unmasked.

JSON-structured logs can be ingested directly by Splunk, Datadog, CloudWatch
Logs Insights, or any SIEM without a parsing rule. This is a hard requirement
in any SOC 2-compliant environment.

---

### 5. SLO Enforcement as a First-Class Test Assertion

Every HTTP response is checked against a configurable `SLO_RESPONSE_TIME_MS`
threshold. A response that arrives with a 200 status but takes 8 seconds is
treated as a test failure with the event name `slo_breach`.

**Financial relevance:** In a latency-sensitive trading system, a position
valuation endpoint that takes 8 seconds is not a working endpoint. A test suite
that only validates correctness and ignores latency provides false assurance. The
SLO assertion makes latency a first-class correctness property — as it is in
production SLAs.

---

### 6. Data Idempotency and the Orphan Registry (`conftest.py`)

Test data is generated using Faker with UUID-fragment suffixes, ensuring zero
collision probability across parallel runs and shared staging environments.

The `created_booking` fixture uses `yield` inside a `try/finally` block.
Teardown runs unconditionally — whether the test passes, fails, raises a
`KeyboardInterrupt`, or is cancelled by `pytest-timeout`. Teardown failures
are logged but never re-raised, so a cleanup side-effect cannot mask the
original test failure.

A session-scoped orphan registry tracks every created resource. At session
end, any resource not explicitly cleaned up by its test is swept by a
finalizer. Running the suite 100 consecutive times leaves the target database
in exactly the same state as before the first run.

**Financial relevance:** A test suite that leaves orphan records in a shared
staging environment is contaminating the environment for every other team
that uses it. In a trading system, orphan records can affect risk calculations,
position aggregations, and regulatory reporting — all of which consume data from
the same environment your tests just polluted.

---

### 7. Faker-Based Synthetic Data (`src/utils/data_factory.py`)

The `BookingDataFactory` generates four categories of test data:
- `realistic()`: Human-looking names, realistic prices, random stay lengths
- `max_length()`: Every string field at its maximum allowed length — truncation regression
- `minimum_price()`: `totalprice = 0` — boundary value for financial amount fields
- `unicode_names()`: Names from Japanese, Arabic, Chinese, and Russian locales

The factory seed is logged on every test. Any failure is reproducible by
passing the logged seed back to `BookingDataFactory(seed=<value>)`.

**Financial relevance:** Hardcoded `"TestUser"` payloads miss an entire class of
bugs. Unicode encoding bugs in customer name fields affect real customers. Zero-
amount record handling bugs affect real financial records. Max-length truncation
bugs silently corrupt data. These are the payloads that find production bugs
before production does.

---

### 8. Containerisation with Non-Root User (`Dockerfile`, `docker-compose.yml`)

The test runner executes as uid 1001 (`testrunner`), not root. The Docker image
uses a multi-stage build: a `deps` stage installs packages, the `runtime` stage
copies only what is needed. The `.dockerignore` prevents `.env` files from ever
being baked into an image layer — even accidentally.

`docker-compose up` produces an environment byte-for-byte identical to GitHub
Actions, eliminating the "works on my machine" class of CI failures.

**Financial relevance:** Running arbitrary code as root inside a container is
rejected by most enterprise Kubernetes security policies (PodSecurityPolicy,
OPA Gatekeeper, Kyverno). A test framework that cannot run in a hardened
container environment cannot run in production-grade infrastructure.

---

### 9. CI/CD Pipeline with Security Audit Gate (`.github/workflows/api-tests.yml`)

The pipeline has two sequential jobs:
1. **`security-audit`**: `pip-audit` scans all dependencies for known CVEs.
   The test job **cannot run** if this fails. Dependencies with unpatched
   vulnerabilities are a build-time failure, not a runtime surprise.
2. **`api-tests`**: Runs against the target environment. Secrets are validated
   for presence before execution. All artifacts (JSONL logs, HTML report,
   JUnit XML) are uploaded unconditionally — a passing run and a failing run
   both produce a full audit trail.

**Financial relevance:** In a regulated financial environment (SOC 2, ISO 27001,
PCI DSS), every software component must have a documented vulnerability management
process. A CI pipeline that blocks on known CVEs is the automation of that process.

---

## What This Framework Is Not

It is not a performance framework (no load generation, no percentile tracking).
It is not a UI testing framework. It is not a chaos engineering framework.

It is precisely what it claims to be: a production-grade API contract and
functional correctness testing framework, built to the operational standards
of a system where data integrity is non-negotiable and observability is
not optional.

---

## Running It

```bash
# Local — requires .env populated from .env.example
cp .env.example .env
# fill in API_BASE_URL, API_USERNAME, API_PASSWORD

# Option A: native Python
pip install -r requirements.txt
pytest

# Option B: Docker (exact CI parity)
docker-compose up --build

# Option C: specific test category
docker-compose run --rm api-tests pytest -m crud -v
docker-compose run --rm api-tests pytest -m contract -v

# Reproduce a specific failure by seed
# (grab seed value from logs/test_run.jsonl after a failure)
docker-compose run --rm api-tests pytest tests/functional/test_bookings_crud.py -v -k "lifecycle"
```

---

*The code is the documentation. Every module-level docstring explains not just
what the code does, but why the decision was made — because in a production
system, the reasoning matters as much as the implementation.*
