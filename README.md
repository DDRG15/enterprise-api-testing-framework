# Enterprise API Testing Framework

Let's be real: testing a REST API on the happy path is trivial. Testing one at the reliability standards of a Tier-1 financial system—where a dropped packet means real money is lost—is an entirely different beast. I engineered this framework with Python, `pytest`, and a healthy dose of operational paranoia to guarantee one thing: **zero false positives.**

---

## Architecture Overview

Here is the blueprint. Notice the dual-strategy circuit breaker—because relying on a single point of failure is a rookie move.

```text
api-testing-framework/
│
├── .github/
│   └── workflows/
│       └── api-tests.yml         # CI/CD pipeline with security audit gate
│
├── config/
│   └── settings.py               # Pydantic-validated config — fails at startup on bad config
│
├── src/
│   ├── client/
│   │   ├── base_client.py        # Core HTTP client: retries, circuit breaker, SLO, logging
│   │   └── booking_client.py     # Domain client: typed methods returning Pydantic models
│   ├── models/
│   │   └── booking.py            # API contracts as Pydantic models — catches schema drift
│   └── utils/
│       ├── circuit_breaker.py          # In-memory circuit breaker (The Windows/Local Fallback)
│       ├── circuit_breaker_redis.py    # Distributed Redis circuit breaker for parallel workers
│       └── logger.py                   # Structured JSON logger with secret masking
│
├── tests/
│   ├── functional/
│   │   └── test_bookings_crud.py # Full CRUD lifecycle + idempotency tests
│   └── contract/
│       └── test_schema_contracts.py # Schema drift detection tests
│
├── conftest.py                   # Session/function fixtures, cross-platform filelock, teardown
├── pytest.ini                    # pytest config with global timeout and JUnit/HTML output
├── requirements.txt
├── .env.example                  # Template — copy to .env, never commit .env
└── .gitignore
```

---

## Engineering Principles

### Zero False Positives
- Retries use exponential backoff **only** on transient codes (429, 502, 503, 504).
- Logic errors (400, 401, 403, 404) **hard fail immediately** — retrying them is just noise.
- `pytest-randomly` randomizes test order to expose hidden order dependencies.
- `pytest-timeout` provides a global 60s watchdog for hangs outside HTTP calls.
- `pytest-xdist` parallel execution coordinated safely via cross-platform OS locks (`filelock`).

### Data Integrity & Idempotency
- All payloads use UUID-derived identifiers — zero collision between parallel runs.
- `created_booking` fixture uses `yield` + `finally` — teardown is unconditional.
- Teardown mechanisms use a dedicated bypass client to guarantee cleanup even if upstream circuits are open.
- An orphan registry sweeps any resource not cleaned up by its owning test.

### Absolute Observability
- Every log line is a JSON object — parseable by Splunk/Datadog/CloudWatch.
- On failure: URL, method, headers (masked), payload, status, body, elapsed time logged.
- `X-Correlation-ID` on every request — full request trace reconstructable from logs.
- A unique `run_id` is bound to every log line for cross-run filtering.

### Graceful Handling & Timeouts
- Every HTTP request has an explicit `(connect_timeout, read_timeout)` tuple.
- Timeout exceptions are caught, duration is logged, test fails clearly.
- Dual-strategy Circuit Breaker (Redis/In-Memory) prevents cascading failures during upstream outages.

### Security — Zero Trust
- No credentials, tokens, or URLs in source code.
- Pydantic `FrameworkSettings` reads exclusively from environment variables.
- Sensitive headers (Authorization, Cookie, X-API-Key) are masked in logs.
- SSL verification is architecturally non-negotiable — `verify=False` is impossible.

### Contract Testing
- Every API response is deserialized through a Pydantic model.
- Schema drift (renamed field, wrong type, missing required field) fails immediately.
- Separate `contract` test suite distinguishes schema failures from logic failures.

### SLO Enforcement
- Every response is checked against `SLO_RESPONSE_TIME_MS`.
- A 200 that took 5 seconds is treated as a failure.

---

## Local Setup

### Option A: Native Python (Windows/macOS/Linux)
Runs the suite locally using the fallback In-Memory circuit breaker. Perfect for rapid development.

```bash
# 1. Clone and enter the repository
git clone <your-repo-url>
cd api-testing-framework

# 2. Create a virtual environment (Don't skip this, trust me)
python -m venv .venv
source .venv/bin/activate       # macOS/Linux
.venv\Scripts\activate          # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment (NEVER commit .env)
cp .env.example .env
# Edit .env with your API values

# 5. Run tests in parallel across all CPU cores
pytest -n auto -v
```

### Option B: Docker Environment (Enterprise CI Parity)
Runs the exact immutable container image used in CI, alongside a Redis instance for the distributed circuit breaker.

```bash
# 1. Configure environment
cp .env.example .env

# 2. Run the full suite with Docker Compose
docker-compose up --build

# 3. Run specific test categories
docker-compose run --rm api-tests pytest -m crud -v
docker-compose run --rm api-tests pytest -m contract -v
```

---

## CI/CD — GitHub Actions

### Required Secrets
Configure these in: `Settings → Secrets and variables → Actions`

| Secret | Description |
|---|---|
| `API_BASE_URL` | Full base URL of the API (e.g. `https://api.definitely-not-prod-shot-my-foot-trading.internal`) |
| `API_USERNAME` | Auth username |
| `API_PASSWORD` | Auth password |
| `API_TOKEN` | Optional pre-issued token |
| `SSL_CA_BUNDLE` | Optional path to custom CA bundle |

### Pipeline Stages
1. **Security Audit** — `pip-audit` scans all dependencies for CVEs. Tests do not run against a compromised dependency tree.
2. **API Tests** — Full suite runs. Secrets are injected as env vars, never as CLI arguments.
3. **Artifact Upload** — Logs, HTML report, and JUnit XML are uploaded regardless of pass/fail.
4. **PR Comment** — On failure in a PR, a comment is posted with a direct link to the failing run.

---

## Extending for a Real Financial System

| Area | Extension |
|---|---|
| **Auth** | Replace token auth with OAuth2/mTLS in `booking_client.authenticate()` |
| **Environments** | Add `env`-scoped `FrameworkSettings` subclasses for dev/staging/prod SLOs |
| **Pact** | Export Pydantic models to a Pact broker for consumer-driven contracts |
| **Performance** | Add `pytest-benchmark` marks on SLO-sensitive endpoints |
| **Alerting** | POST the structured JSONL log to a webhook on failure |

---

## The War Stories / I Shot Myself in the Foot

Look, I'm not going to sit here and pretend this architecture materialized perfectly out of thin air. Real engineering is messy, and I tripped over my own shoelaces a few times building this. Here is the unfiltered truth about the bugs we squashed so you don't have to:

1. **The "Oops, Global Install" Disaster:** Yeah, I got a bit too fast in the terminal and accidentally ran `pip install -r requirements.txt` directly into my global Windows Python environment because I forgot to activate my virtual environment (`.venv`). It triggered a massive dependency conflict cascade. Lesson learned: always isolate your environment. Always. 
2. **The `fcntl` Trap:** While building the `pytest-xdist` parallel execution lock, I tried to be clever and used `fcntl` for process coordination. Guess what? `fcntl` is a Unix-only kernel call. It blew up spectacularly the second I ran it natively on my Windows machine. I had to rip it out and replace it with the cross-platform `filelock` package so it works flawlessly everywhere.
3. **The "Production-Ready" Reality Check:** It hit me hard during debugging: "Production-Ready" doesn't just mean your code runs perfectly on a pristine Linux server inside a Docker container. It means the system is resilient enough that it won't crash when someone clones the repo onto their Windows laptop and runs `pytest` out of the box. Graceful degradation (like falling back to an in-memory circuit breaker when Redis isn't found) is a feature, not an afterthought.

### The "Localhost is a Lie" Reality Check

Let's not kid ourselves: passing 100% of tests using pristine, synthetic `Faker` data in a local sandbox is great, but the real internet is a chaotic, stochastic jungle.

I am not going to insult your intelligence by quoting some arbitrary "expect a 1% variance" metric when moving to production. The unfiltered truth? I don't know the exact failure rate you'll see in the wild, because I haven't pointed this at *your* specific infrastructure yet. It could run flawlessly, or the transition from synthetic data to live traffic could metaphorically blow your arm off.

Here is what actually happens when you leave the deterministic vacuum of local testing:

* **The WAF Wall:** Real-world firewalls (Cloudflare, Akamai) do not care about your `pytest-xdist` parallel execution. If you fire a high-concurrency async battery from a single IP, the WAF will classify you as a DDoS attack and throttle you into the ground.
* **The Legacy DB Trap:** `Faker` generates beautifully chaotic, high-entropy UTF-8 payloads. Your upstream 10-year-old `latin1` database backend might just choke on them and throw a 500 Internal Server Error.
* **The Network Jitter:** Real packets traverse unpredictable BGP hops. Synthetic local packets don't. Your perfectly tuned `read_timeout` settings *will* occasionally get breached simply by the physics of internet routing.

**The SRE Takeaway:** This framework is a precision instrument, but it assumes a baseline of deterministic sanity. If you are pointing this at a live production system for the first time, run a single-threaded Canary Probe with known-good static data first. Do not unleash the full concurrent stochastic battery on day one and act surprised when the pager goes off.
