# DESIGN.md — AI Code Reviewer

This document explains the technical decisions made while building this project, the alternatives considered, and what I'd do differently at scale. Written as a reference for anyone reading the codebase and for my own interview preparation.

---

## Problem Statement

Code reviews are valuable but slow. Senior engineers are a bottleneck — they can't review every PR immediately, and when they do, they often miss subtle bugs because they're reviewing quickly. The goal was to build an automated first-pass reviewer that catches obvious issues instantly, so human reviewers can focus on higher-level concerns like architecture and design.

---

## Architecture Overview

The system is a GitHub App — a server-side integration that receives webhook events from GitHub, processes them, and writes back to the GitHub API.

The core flow is:

1. GitHub sends a `pull_request` webhook event to our server when a PR is opened or updated
2. We verify the webhook signature to confirm it came from GitHub
3. We fetch the PR diff and full file contents via the GitHub REST API
4. We run each changed file through an AI review pipeline
5. We post the results back as PR comments via the GitHub API
6. We store PR metadata in a Postgres database for future analytics

The system is intentionally stateless per request — each webhook event is fully self-contained. No session state, no job queues (at this scale).

---

## Key Design Decisions

### Why a GitHub App instead of OAuth?

GitHub Apps and OAuth Apps both let you interact with GitHub's API, but they're fundamentally different.

OAuth Apps authenticate as a user — every action appears as if the user themselves did it. This is inappropriate for an automated bot; you don't want your bot appearing as your personal account in PR comments.

GitHub Apps authenticate as the app itself, with fine-grained permissions scoped per installation. You can grant it only "Pull requests: read & write" and "Contents: read" — nothing else. Each repo installation gets its own short-lived access token (valid for 1 hour), which is far more secure than a long-lived OAuth token.

The tradeoff is complexity — GitHub Apps require JWT authentication using an RSA private key, which is more involved to set up. Worth it for the security model.

### Why Groq instead of OpenAI?

Both provide access to capable LLMs via API. The decision came down to cost.

OpenAI's API is paid. For a student project with no revenue, spending money on API calls is unsustainable. Groq provides free access to Llama 3.3 70B with a generous rate limit of 14,400 requests per day — more than enough for this project.

Llama 3.3 70B is also genuinely capable at code review. In informal testing it catches real bugs, not just style issues.

The tradeoff is reliability — Groq has had occasional model deprecations (we hit one during development when `llama-3.1-70b-versatile` was decommissioned). The fix was a one-line model name change, but in a production system you'd want a fallback model configured.

### Why FastAPI instead of Flask or Django?

Three reasons: async support, automatic API docs, and speed.

FastAPI is built on Starlette and supports async/await natively. This matters because our webhook handler makes multiple sequential HTTP calls to GitHub (fetch diff, fetch file list, fetch file contents). With Flask's synchronous model, the server thread would block waiting for each HTTP response. With FastAPI + httpx (async HTTP client), the event loop can handle other requests while waiting for GitHub to respond.

Django would be overkill — it's designed for full web applications with templates, ORM migrations, admin panels, and more. We need a lean API server with two endpoints.

The tradeoff is that FastAPI is newer and has a smaller ecosystem than Flask/Django. For this use case that's not a problem.

### Why Neon Postgres instead of SQLite or a hosted MySQL?

SQLite is file-based and doesn't work well on platforms like Render where the filesystem is ephemeral (wiped on each deploy). We need a persistent external database.

Neon is serverless Postgres with a genuinely free tier — 0.5GB storage, no connection limits for this traffic level. Crucially, it supports `pgvector`, which we'd need in a future version to store embeddings of past reviews for similarity search.

Standard MySQL would also work, but Postgres has better support in the Python ecosystem (SQLAlchemy's asyncpg driver is more mature than aiomysql) and is what most backend roles use.

### Why SQLAlchemy async instead of raw asyncpg?

Raw `asyncpg` would be faster and have less overhead, but SQLAlchemy gives us an ORM that makes it easy to define models as Python classes, run table migrations, and query without writing raw SQL strings.

The tradeoff is complexity — SQLAlchemy's async support requires `greenlet` as a dependency, which caused a deployment issue (it wasn't included in `requirements.txt` initially). Lesson learned: always install dependencies in a fresh venv and freeze from there.

### The two-pass review approach

A naive implementation would send every changed file directly to the LLM. This wastes API credits on simple files that don't need AI analysis.

The current approach does AST analysis first (for Python files) to compute a complexity score. High-complexity files get LLM review. Low-complexity files still get LLM review in the current implementation (complexity score is logged but not yet used as a filter) — this is a planned optimization.

The AST parser uses Python's built-in `ast` module, which is zero-cost and runs in milliseconds. It counts branches (if, for, while, try, with, assert) as a proxy for cyclomatic complexity. A function with 10+ branches almost certainly has reviewable code; a function with 0 branches is likely a simple data class or utility.

### Why not post inline diff comments?

GitHub's "pull request review" API allows attaching comments to specific lines in the diff. This is what Copilot and CodeRabbit do — comments appear inline next to the relevant code.

The challenge is diff line mapping. GitHub's diff format uses "position" numbers that count lines from the top of the diff, not the original file. Mapping from "line 42 in the file" to "position 18 in the diff" requires parsing the unified diff format, which is error-prone.

The current implementation posts comments as regular PR comments with the filename and approximate line number mentioned in the body. This is simpler and reliable. Proper inline diff comments are on the roadmap.

---

## What I'd Do Differently at Scale

**Job queue for webhook processing.** Currently, the webhook handler does all work synchronously within the HTTP request. If Groq is slow or GitHub rate-limits us, the request times out and GitHub retries. At scale, the webhook handler should immediately return 200 and push the job to a queue (Redis + Celery, or BullMQ) for async processing.

**Caching installation tokens.** Each PR review generates a new GitHub installation token via a JWT exchange. Installation tokens are valid for 1 hour. At scale, we'd cache them in Redis keyed by `installation_id` with a 55-minute TTL, saving one API round-trip per review.

**Per-repo configuration.** Currently review settings are global. Repos should be able to drop a `.codereview.yaml` file to configure severity thresholds, file patterns to skip, or specific rules to enable/disable. This is a straightforward feature to add.

**Model fallback.** If Groq is unavailable, fall back to a smaller local model (via Ollama) or a different provider. Currently the bot silently skips files if the LLM call fails.

**Structured logging.** The current implementation uses `print()` statements. In production you'd use Python's `logging` module with structured JSON output, log levels, and a log aggregation service.

**Rate limiting per repo.** If a repo opens 50 PRs at once (e.g. a bot creating PRs), we'd send 50 simultaneous requests to Groq and hit rate limits. A per-repo token bucket would smooth this out.

---

## Deployment Architecture

The app runs as a single process on Render's free tier (0.1 CPU, 512MB RAM). This is sufficient because:

- The app is I/O bound, not CPU bound — most time is spent waiting for GitHub API and Groq responses
- SQLAlchemy async + httpx means the single process handles concurrent requests efficiently via the event loop
- 512MB RAM is far more than needed — observed usage is ~60MB at rest

The keep-alive cron job (pinging `/ping` every 10 minutes) prevents Render's free tier from spinning down the service. Without this, the first webhook after 15 minutes of inactivity would have a ~30 second cold start, causing GitHub to timeout and retry.

---

## Security Considerations

**Webhook signature verification** is the first thing the webhook handler does, before any processing. The `X-Hub-Signature-256` header contains an HMAC-SHA256 of the request body using the webhook secret. We use `hmac.compare_digest` instead of `==` for constant-time comparison, preventing timing attacks.

**Private key storage.** The GitHub App's RSA private key is stored as an environment variable on Render, not in the codebase. It's in `.gitignore` locally. Never committed to Git.

**Minimal permissions.** The GitHub App is granted only "Pull requests: read & write" and "Contents: read". It cannot access issues, delete branches, or modify repo settings.

**No user data stored.** We store PR metadata (repo name, PR number, title, timestamp) but never store file contents, diffs, or LLM outputs in the database. This keeps our data footprint minimal.

---