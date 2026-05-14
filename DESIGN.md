# DESIGN.md — AI Code Reviewer

A complete reference for understanding how this project works — every package, every file, every important piece of code, and the full end-to-end workflow. Written so I can re-read this months later and understand the entire system without looking at the code.

---

## What This Project Does

When a pull request is opened or updated on a GitHub repo where this app is installed, a server receives the event, fetches the changed code, sends it to an LLM for analysis, and posts the findings as comments directly on the PR. The entire flow takes 10–15 seconds from PR open to bot comment appearing.

---

## Packages — What Each One Is and Why It's Here

### `fastapi`
The web framework that runs the server. It receives incoming HTTP requests and routes them to the right Python function. Used to define three endpoints: `/health` (check if server is alive), `/ping` (keep-alive for Render's free tier), and `/webhook` (receives PR events from GitHub). Chosen over Flask because it supports `async/await` natively — critical when your server makes multiple HTTP calls to GitHub and Groq and you don't want to block while waiting.

### `uvicorn`
The ASGI server that actually runs FastAPI. FastAPI defines what to do with requests; uvicorn is the process that listens on a port and feeds those requests in. When you run `uvicorn main:app --host 0.0.0.0 --port $PORT`, uvicorn starts the server and hands control to FastAPI.

### `httpx`
Async HTTP client — how your code makes outbound HTTP requests to the GitHub API and Groq API. Used instead of the popular `requests` library because `requests` is synchronous (blocks the thread while waiting for a response). With `httpx` and `async/await`, the server can handle other work while waiting for GitHub to respond. Used in `webhook.py` to fetch diffs and in `commenter.py` to post comments.

### `asyncpg`
The low-level async driver that speaks the Postgres wire protocol to Neon. You never call it directly — SQLAlchemy uses it under the hood. But it must be installed, otherwise SQLAlchemy has no way to actually connect to Postgres asynchronously.

### `sqlalchemy`
The ORM (Object Relational Mapper). Lets you define your database table as a Python class instead of writing raw SQL. Handles creating the table on startup, managing the connection pool, and running queries. The async version of SQLAlchemy (used here) uses asyncpg as its driver and greenlet internally for connection management.

### `greenlet`
A low-level concurrency library that SQLAlchemy's async engine requires internally. It bridges between async and sync code inside SQLAlchemy's connection pool. You never call it directly — it's a hidden dependency of SQLAlchemy async. If it's missing, the app crashes on startup with `No module named 'greenlet'`.

### `python-dotenv`
Reads your `.env` file and loads its contents into `os.environ`. Without this, environment variables like `DATABASE_URL` only exist if you manually set them in the terminal. With dotenv, you write them in `.env` once and every `os.getenv()` call in the code picks them up automatically. Only used in local development — on Render the variables are set in the dashboard.

### `PyGithub`
Official Python wrapper for the GitHub REST API. Installed for future use (dashboard, richer GitHub integration). Current API calls in `webhook.py` and `commenter.py` are done manually with `httpx` for more control over request headers and response handling.

### `PyJWT`
Generates and signs JSON Web Tokens. GitHub Apps authenticate using a JWT signed with your RSA private key — this proves to GitHub that the request is coming from your app and not an impersonator. PyJWT handles the RS256 signing algorithm. Without this, you cannot authenticate as your GitHub App at all.

### `cryptography`
The underlying cryptographic engine that PyJWT depends on for RSA operations. Provides the actual implementation of RSA-256 encryption. Never called directly — it's a dependency of PyJWT.

### `groq`
Official Groq Python SDK. Wraps Groq's REST API and gives a clean interface to send prompts to Llama 3.3 70B and receive responses. Used in `reviewer.py` to send code review prompts and parse back structured JSON responses. Chosen because Groq's free tier offers 14,400 requests/day with fast inference — zero cost for a student project.

---

## Files — What Each One Does and the Important Code

---

### `database.py`

Sets up the connection to Neon Postgres and defines the data model.

**Connection setup:**
```python
raw_url = os.getenv("DATABASE_URL")
clean_url = raw_url.split("?")[0].replace("postgresql://", "postgresql+asyncpg://")
engine = create_async_engine(clean_url, connect_args={"ssl": "require"})
```
Neon's connection string has query params like `?sslmode=require&channel_binding=require`. asyncpg doesn't understand these params — it uses its own SSL config. So we strip everything after `?` with `.split("?")[0]`, swap the driver prefix, and pass SSL separately via `connect_args`. This is the correct way to configure async Postgres with SSL.

**Session factory:**
```python
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
```
Creates a factory for database sessions. Every time you need to query the DB, you open a session using this factory with `async with AsyncSessionLocal() as session:`.

**The data model:**
```python
class PullRequest(Base):
    __tablename__ = "pull_requests"
    id = Column(Integer, primary_key=True)
    repo = Column(String, nullable=False)        # e.g. "username/reponame"
    pr_number = Column(Integer, nullable=False)  # e.g. 42
    pr_title = Column(String)                    # e.g. "Fix login bug"
    opened_at = Column(DateTime, default=datetime.utcnow)
```
Maps to the `pull_requests` table in Neon. Each row is one PR that the bot reviewed.

**Table creation on startup:**
```python
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
```
Runs once when the server boots. Checks if `pull_requests` exists in Neon — creates it if not. This is why you never had to manually create the table.

---

### `github_auth.py`

Handles GitHub App authentication — a two-step process to prove identity and get a scoped access token.

**Why two steps?** GitHub Apps use a JWT to prove they are the app, then exchange that JWT for a short-lived installation token scoped to a specific repo. This is more secure than a single long-lived token.

**Step 1 — Generate a JWT:**
```python
def generate_jwt() -> str:
    app_id = os.getenv("GITHUB_APP_ID")
    private_key = os.getenv("GITHUB_PRIVATE_KEY", "").replace("\\n", "\n")
    now = int(time.time())
    payload = {
        "iat": now - 60,   # issued 60 seconds ago — handles clock skew between servers
        "exp": now + 600,  # expires in 10 minutes
        "iss": str(app_id) # issuer = your GitHub App ID
    }
    return jwt.encode(payload, private_key, algorithm="RS256")
```
Signs the payload with your RSA private key using RS256. GitHub verifies this signature using your app's public key on their end. The `.replace("\\n", "\n")` is needed because when you paste a PEM key into an environment variable, newlines sometimes get stored as literal `\n` text — this converts them back to real newlines so the RSA parser can read the key correctly.

**Step 2 — Exchange JWT for an installation token:**
```python
async def get_installation_token(installation_id: int) -> str:
    token = generate_jwt()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        )
        return resp.json()["token"]
```
Each installation of your app (each repo it's installed on) has a unique `installation_id`. This call exchanges your 10-minute JWT for a 1-hour access token scoped specifically to that installation's repos. This token is what you use to fetch diffs and post comments.

---

### `webhook.py`

Two responsibilities: verify that requests actually came from GitHub, and fetch the code changes.

**Signature verification:**
```python
def verify_signature(payload: bytes, signature: str) -> bool:
    secret = os.getenv("GITHUB_WEBHOOK_SECRET").encode()
    expected = "sha256=" + hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```
GitHub signs every webhook payload with your webhook secret using HMAC-SHA256 and puts the result in the `X-Hub-Signature-256` header. This function recomputes that hash and checks if it matches. `hmac.compare_digest` is used instead of `==` to prevent timing attacks — a normal `==` exits as soon as it finds a mismatch, leaking information about how many characters matched. `compare_digest` always takes the same time regardless of where the mismatch is.

**Fetching the PR diff and file contents:**
```python
async def fetch_pr_diff(repo_full_name, pr_number, token) -> dict:
```
Makes three sequential GitHub API calls:

Call 1 — fetch the raw diff (the `+/-` changed lines):
```python
headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3.diff"}
diff_resp = await client.get(
    f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}",
    headers=headers
)
diff_text = diff_resp.text
```
The special `Accept: application/vnd.github.v3.diff` header tells GitHub to return the raw unified diff format instead of JSON.

Call 2 — fetch the list of changed files:
```python
files_resp = await client.get(
    f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/files",
    headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
)
files = files_resp.json()  # list of {filename, status, contents_url, ...}
```

Call 3 — fetch full content of each changed file:
```python
for f in files:
    if f.get("status") == "removed":
        continue  # skip deleted files — nothing to review
    content_resp = await client.get(f["contents_url"], headers=...)
    content_data = content_resp.json()
    if content_data.get("encoding") == "base64":
        file_contents[f["filename"]] = base64.b64decode(
            content_data["content"]
        ).decode("utf-8", errors="ignore")
```
GitHub returns file content base64-encoded. This decodes it back to plain text. `errors="ignore"` means if the file has non-UTF-8 bytes (e.g. a binary file that slipped through), those bytes are silently dropped instead of crashing.

Returns `{"diff": diff_text, "files": {"filename": "full content", ...}}`.

---

### `reviewer.py`

The AI engine. Takes the diff and file contents, returns a list of issues found.

**Skip filter:**
```python
SKIP_PATTERNS = ["generated", "vendor", "node_modules", "migrations",
                 "package-lock.json", "yarn.lock", ".min.js", ".min.css"]

def should_skip_file(filename: str) -> bool:
    return any(pattern in filename.lower() for pattern in SKIP_PATTERNS)
```
Files matching these patterns are auto-generated or third-party code — not worth reviewing.

**AST complexity analysis (Python files only):**
```python
def get_complexity_score(code: str, filename: str) -> int:
    if not filename.endswith(".py"):
        return 0
    tree = ast.parse(code)
    score = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.For, ast.While, ast.Try,
                             ast.ExceptHandler, ast.With, ast.Assert)):
            score += 1
    return score
```
Python's built-in `ast` module parses source code into a tree of nodes. Walking the tree and counting branch nodes gives a complexity score. A score of 0 means flat, simple code. A score of 10+ means deeply nested logic more likely to contain bugs. Runs in milliseconds at zero API cost. Non-Python files return 0 because Python's AST module only understands Python syntax.

**The LLM prompt:**
```python
def build_prompt(filename, diff, full_code) -> str:
    return f"""You are a senior software engineer doing a thorough code review.

FILE: {filename}

DIFF (what changed):
{diff}

FULL FILE CONTENT (for context):
{full_code[:3000]}

Review the code for:
1. Bugs or logic errors
2. Security vulnerabilities (SQL injection, XSS, exposed secrets)
3. Missing error handling
4. Performance issues
5. Code clarity and maintainability

Respond ONLY with a JSON array. No explanation, no markdown, just raw JSON.
Each item must have exactly these fields:
- "line": approximate line number (integer)
- "severity": one of "critical", "warning", or "suggestion"
- "comment": specific, actionable comment explaining the issue and how to fix it

If there are no issues, return an empty array: []"""
```
Both the diff and the full file are sent. The diff alone shows what changed but not why — the LLM needs the surrounding code to understand if a change introduces a bug. File content is capped at 3,000 characters to stay within Groq's token limits. Temperature is set to 0.1 (very low) for consistent, non-creative output.

**Parsing the LLM response:**
```python
def parse_llm_response(response_text: str) -> list:
    clean = response_text.strip()
    if clean.startswith("```"):         # strip markdown code fences if present
        clean = clean.split("```")[1]
        if clean.startswith("json"):
            clean = clean[4:]
    clean = clean.strip()
    comments = json.loads(clean)
    valid = []
    for item in comments:
        if all(k in item for k in ["line", "severity", "comment"]):
            if item["severity"] in ["critical", "warning", "suggestion"]:
                valid.append(item)
    return valid
```
Even though the prompt says "no markdown", the LLM sometimes wraps the JSON in code fences anyway. This strips those out. Then it validates the structure — every item must have all three required fields and a valid severity value. Malformed items are dropped. If the JSON is completely unparseable, returns `[]` rather than crashing.

**Main review function:**
```python
async def review_pr(diff: str, files: dict) -> list:
    all_comments = []
    for filename, full_code in files.items():
        if should_skip_file(filename):
            continue
        complexity = get_complexity_score(full_code, filename)
        prompt = build_prompt(filename, diff, full_code)
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=1000
        )
        raw = response.choices[0].message.content
        comments = parse_llm_response(raw)
        for c in comments:
            c["filename"] = filename   # tag each comment with its file
        all_comments.extend(comments)
    return all_comments
```
Loops through every changed file, skips generated ones, runs AST analysis, sends to Groq, parses the response, tags each comment with the filename, and accumulates everything into one list. Each file is wrapped in try/except so a failure on one file doesn't kill the whole review.

---

### `commenter.py`

Posts the review results back to the GitHub PR.

**Building the summary:**
```python
critical   = sum(1 for c in comments if c["severity"] == "critical")
warnings   = sum(1 for c in comments if c["severity"] == "warning")
suggestions = sum(1 for c in comments if c["severity"] == "suggestion")

summary = f"""## 🤖 AI Code Review
| Severity | Count |
|----------|-------|
| 🔴 Critical | {critical} |
| 🟡 Warning | {warnings} |
| 🟢 Suggestion | {suggestions} |
> Powered by Llama 3.3 70B via Groq"""
```
Posts a summary table as the first comment so the developer immediately sees the big picture without reading all individual comments.

**Posting individual comments:**
```python
for c in comments:
    body = f"{SEVERITY_EMOJI[c['severity']]}\n\n{c['comment']}"
    await client.post(
        f"https://api.github.com/repos/{repo_full_name}/issues/{pr_number}/comments",
        headers=headers,
        json={"body": f"**`{c['filename']}`** (line ~{c['line']})\n\n{body}"}
    )
```
Posts each issue as a separate PR comment with the filename, approximate line number, severity badge, and the comment text. Uses the issues comments endpoint (`/issues/{pr_number}/comments`) rather than the review comments endpoint (`/pulls/{pr_number}/comments`) because the review endpoint requires mapping file line numbers to diff position numbers — a complex parsing problem. Issue comments are simpler and reliable.

---

### `main.py`

The entry point. Creates the FastAPI app and wires everything together.

**Startup:**
```python
@app.on_event("startup")
async def startup():
    await init_db()
```
Runs once when the server boots. Creates the Neon table if it doesn't exist.

**The webhook endpoint — full orchestration:**
```python
@app.post("/webhook")
async def webhook(request: Request):
    # 1. Read raw body before parsing — needed for HMAC verification
    signature = request.headers.get("X-Hub-Signature-256", "")
    payload = await request.body()

    # 2. Security gate — reject anything not from GitHub
    if not verify_signature(payload, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    # 3. Parse and filter events
    event_type = request.headers.get("X-GitHub-Event", "")
    data = json.loads(payload)
    if event_type != "pull_request":
        return {"status": "ignored"}
    if data.get("action") not in ["opened", "synchronize"]:
        return {"status": "ignored"}

    # 4. Extract PR context from payload
    repo_full_name  = data["repository"]["full_name"]
    pr_number       = data["pull_request"]["number"]
    pr_title        = data["pull_request"]["title"]
    installation_id = data["installation"]["id"]

    # 5. Authenticate with GitHub
    token = await get_installation_token(installation_id)

    # 6. Fetch the code
    pr_data = await fetch_pr_diff(repo_full_name, pr_number, token)

    # 7. Run AI review
    comments = await review_pr(pr_data["diff"], pr_data["files"])

    # 8. Post results back to GitHub
    await post_review(repo_full_name, pr_number, token, comments, pr_data["diff"])

    # 9. Store in database
    async with AsyncSessionLocal() as session:
        pr = PullRequest(repo=repo_full_name, pr_number=pr_number, pr_title=pr_title)
        session.add(pr)
        await session.commit()

    return {"status": "reviewed", "pr": pr_number, "issues_found": len(comments)}
```
This is the entire workflow in sequence. `main.py` imports from every other module and is the only file that sees the whole picture. Every other file has one focused job.

---

## Full End-to-End Workflow

**1. PR opened on GitHub**
You push a branch and open a pull request on a repo where the GitHub App is installed.

**2. GitHub sends webhook**
GitHub sends a `POST` request to `https://ai-code-reviewer-eezo.onrender.com/webhook`. The request body is a JSON payload describing the PR event. The header `X-Hub-Signature-256` contains the HMAC-SHA256 signature of that body using your webhook secret. The header `X-GitHub-Event` is set to `pull_request`.

**3. Signature verification**
`main.py` reads the raw body bytes and the signature header before doing anything else. It passes them to `verify_signature` in `webhook.py`. The function recomputes the HMAC and compares using constant-time comparison. If it doesn't match, 401 is returned immediately. Nothing else happens.

**4. Event filtering**
The payload is parsed as JSON. The event type is checked — if it's not `pull_request`, return `{"status": "ignored"}`. The action is checked — if it's not `opened` or `synchronize`, return `{"status": "ignored"}`. GitHub sends webhooks for dozens of event types (stars, issues, comments, pushes); you only care about PR open and update events.

**5. JWT generation**
`get_installation_token` in `github_auth.py` calls `generate_jwt` first. It reads your App ID and RSA private key from environment variables, builds a JWT payload with issued-at, expiry, and issuer fields, and signs it using RS256. This JWT is valid for 10 minutes.

**6. Installation token exchange**
The JWT is sent to `https://api.github.com/app/installations/{installation_id}/access_tokens`. GitHub verifies the JWT signature using your app's public key, confirms the installation ID matches an active installation, and returns a 1-hour access token scoped to that installation's repos. This token is used for all subsequent GitHub API calls.

**7. Diff and file fetching**
`fetch_pr_diff` makes three GitHub API calls. First it fetches the unified diff (changed lines only). Then it fetches the list of changed files with metadata. Then for each non-deleted file it fetches the full file content, decodes it from base64, and stores it in a dictionary keyed by filename. The result is a diff string and a files dictionary.

**8. AI review**
`review_pr` loops through each file. It skips generated/vendor files. It runs AST complexity analysis on Python files. It builds a prompt containing the diff and full file content (capped at 3,000 chars). It calls Groq's API with Llama 3.3 70B at temperature 0.1. It parses the JSON response, validates the structure, tags each comment with the filename, and accumulates all comments.

**9. Posting comments**
`post_review` counts comments by severity and posts a markdown summary table as the first PR comment. Then it loops through each individual comment and posts it as a separate PR comment with the severity emoji, filename, approximate line number, and the comment text.

**10. Database write**
A `PullRequest` record is created in Neon with the repo name, PR number, and title. This is used for future analytics (review history, trends dashboard).

**11. Response to GitHub**
The webhook handler returns `{"status": "reviewed", "pr": pr_number, "issues_found": N}` with a 200 status. GitHub receives this, marks the webhook delivery as successful, and won't retry.

**12. Bot comments appear on PR**
You refresh the PR page and see the summary table and individual issue comments posted by the bot, typically within 10–15 seconds of opening the PR.

---

## Why Certain Design Choices Were Made

**GitHub App over OAuth App** — OAuth Apps act as a user. Every action would appear as if your personal account posted the comment. GitHub Apps act as the app itself with their own identity and fine-grained permissions. You grant only what's needed: "Pull requests: read & write" and "Contents: read". Nothing else.

**Groq over OpenAI** — OpenAI's API costs money. Groq's free tier gives 14,400 requests per day with fast inference on Llama 3.3 70B. For a student project with zero revenue this is the only practical choice.

**FastAPI over Flask** — Flask is synchronous. Every GitHub API call would block the server thread while waiting for a response. FastAPI with httpx is fully async — the server handles other requests while waiting for GitHub or Groq to respond. This matters because a single PR review makes 4–6 outbound HTTP calls.

**Neon Postgres over SQLite** — SQLite is file-based. On Render, the filesystem is ephemeral — it gets wiped on every deploy. Any data stored in SQLite would be lost every time you push. Neon is an external persistent database with a free tier that never gets wiped.

**Issue comments over inline diff comments** — GitHub's inline review comment API requires mapping from file line numbers to diff position numbers. A diff position is the count of lines from the top of the entire diff, not the line number in the file. Computing this mapping requires parsing the unified diff format line by line. Issue comments are simpler, reliable, and still clearly attribute comments to specific files and line numbers.

**Stripping query params from DATABASE_URL** — asyncpg doesn't support `sslmode` as a URL query parameter. It uses its own SSL configuration passed via `connect_args`. Neon's default connection string includes `?sslmode=require&channel_binding=require` which asyncpg rejects. Stripping everything after `?` and passing `connect_args={"ssl": "require"}` is the correct solution.

---

## Architecture Decisions for Scale (What Would Change at 10,000 Users)

**Job queue for async processing.** Currently the webhook handler does all work synchronously within the HTTP request — if Groq is slow, the request times out and GitHub retries. At scale, the webhook handler should immediately return 200 and push the job to a queue (Redis + Celery). A separate worker process pulls jobs from the queue and processes them independently.

**Cache installation tokens.** Currently a new installation token is fetched on every PR review. Installation tokens are valid for 1 hour. At scale these would be cached in Redis keyed by `installation_id` with a 55-minute TTL, saving one API round-trip per review.

**Per-repo configuration.** A `.codereview.yaml` file in each repo would let teams configure severity thresholds, file patterns to skip, and specific rules to enable. Currently all settings are global.

**Model fallback.** If Groq is unavailable, fall back to a smaller local model via Ollama or a different provider. Currently if Groq fails, the file is silently skipped.

**Rate limiting per repo.** A repo could theoretically trigger 50 simultaneous PR reviews (e.g. a bot creating PRs). Without rate limiting, all 50 would hit Groq simultaneously and get rate-limited. A per-repo token bucket in Redis would smooth this.