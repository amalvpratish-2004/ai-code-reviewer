# ai-code-reviewer

# AI Code Reviewer

A GitHub App that automatically reviews pull requests using **Llama 3.3 70B** via Groq. When a PR is opened or updated, the bot fetches the changed files, analyzes them for bugs, security issues, and code quality problems, and posts comments directly on the PR.

**Backend:** https://ai-code-reviewer-eezo.onrender.com

---

## Demo

Open a PR on any repo where the bot is installed and within seconds you'll see comments like:

```
🔴 Critical
Missing error handling — if the API call fails, the error is silently swallowed.
Wrap in try/catch and surface the error to the caller.

🟡 Warning
User input passed directly into the query without sanitization.
Use parameterized queries to prevent SQL injection.

🟢 Suggestion
This function is doing too many things. Consider splitting into smaller,
single-responsibility functions for easier testing.
```

---

## How It Works

```
GitHub PR opened / updated
         ↓
POST /webhook  (HMAC signature verified)
         ↓
Fetch PR diff + full file contents  (GitHub API)
         ↓
AST complexity analysis  (Python files)
         ↓
LLM review per file  (Llama 3.3 70B via Groq)
         ↓
Parse structured JSON response
         ↓
Post comments back to PR  (GitHub API)
         ↓
Store PR metadata in Neon Postgres
```

---

## Tech Stack

| Layer | Tool | Why |
|-------|------|-----|
| Backend | FastAPI + Python | Async, fast, great for webhook handlers |
| LLM | Llama 3.3 70B via Groq | Free tier, fast inference, high quality |
| Database | Neon Postgres + SQLAlchemy | Serverless Postgres, free tier |
| Hosting | Render | Free tier, auto-deploys from GitHub |
| Auth | GitHub App + JWT | Secure, scoped per-installation tokens |
| HTTP | httpx | Async HTTP client for GitHub API calls |

**Total monthly cost: $0**

---

## Project Structure

```
ai-code-reviewer/
├── main.py           # FastAPI app, webhook endpoint, orchestration
├── webhook.py        # HMAC signature verification, PR diff fetching
├── github_auth.py    # GitHub App JWT generation, installation tokens
├── reviewer.py       # AI review engine (AST analysis + Groq LLM)
├── commenter.py      # Posts review comments back to GitHub PR
├── database.py       # Neon Postgres connection, PullRequest model
├── render.yaml       # Render deployment config
├── requirements.txt  # Python dependencies
└── .python-version   # Pins Python 3.13
```

---

## Installation — Add Bot to Your Repo

1. Go to the GitHub App installation page
2. Click **Install**
3. Select the repositories you want reviewed
4. Open a PR — the bot comments within 10–15 seconds

---

## Local Development

**Prerequisites:** Python 3.13, a Neon Postgres database, a GitHub App, a Groq API key

**1. Clone and set up environment**

```bash
git clone https://github.com/amalvpratish-2004/ai-code-reviewer
cd ai-code-reviewer
python -m venv venv
venv\Scripts\activate        # Windows
source venv/bin/activate     # Mac/Linux
pip install -r requirements.txt
```

**2. Create `.env` file**

```env
DATABASE_URL=postgresql://user:password@host/neondb?sslmode=require
GITHUB_APP_ID=your-app-id
GITHUB_WEBHOOK_SECRET=your-webhook-secret
GITHUB_PRIVATE_KEY_PATH=private-key.pem
GROQ_API_KEY=your-groq-api-key
```

**3. Run locally**

```bash
uvicorn main:app --reload
```

**4. Expose local server for GitHub webhooks**

```bash
ngrok http 8000
# Update your GitHub App webhook URL to the ngrok URL
```

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | Neon Postgres connection string |
| `GITHUB_APP_ID` | Your GitHub App's numeric ID |
| `GITHUB_WEBHOOK_SECRET` | Webhook secret set in GitHub App settings |
| `GITHUB_PRIVATE_KEY` | Full contents of the `.pem` private key file |
| `GROQ_API_KEY` | API key from console.groq.com |

---

## What Gets Reviewed

The bot reviews every changed file in a PR except auto-generated files, dependency lock files, minified files, and deleted files.

For each file it checks:

- Bugs and logic errors
- Security vulnerabilities (injection attacks, exposed secrets, XSS)
- Missing error handling
- Performance issues
- Code clarity and maintainability

---

## Known Limitations

- Reviews only changed files, not the full codebase — cross-file bugs may be missed
- File content is capped at 3,000 characters per file to stay within LLM context limits
- Free Render tier has cold starts if idle — mitigated by keep-alive cron job
- Groq free tier: 14,400 requests/day — sufficient for personal and small team use

---

## Roadmap

- [ ] Inline diff comments at the exact line level
- [ ] Next.js dashboard showing review history and trends per repo
- [ ] `.codereview.yaml` config file per repo for custom rules and severity thresholds
- [ ] Unit and integration test suite with coverage badge

---

## License

MIT