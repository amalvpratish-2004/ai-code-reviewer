from fastapi import FastAPI, Request, HTTPException
from database import init_db, AsyncSessionLocal, PullRequest
from webhook import verify_signature, fetch_pr_diff
from github_auth import get_installation_token
import json

app = FastAPI()

@app.on_event("startup")
async def startup():
    await init_db()

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/ping")
def ping():
    return {"status": "alive"}

@app.post("/webhook")
async def webhook(request: Request):
    # Step 1: verify signature
    signature = request.headers.get("X-Hub-Signature-256", "")
    payload = await request.body()

    if not verify_signature(payload, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    # Step 2: parse event
    event_type = request.headers.get("X-GitHub-Event", "")
    data = json.loads(payload)

    if event_type != "pull_request":
        return {"status": "ignored"}

    action = data.get("action")
    if action not in ["opened", "synchronize"]:
        return {"status": "ignored"}

    # Step 3: extract PR info
    repo_full_name = data["repository"]["full_name"]
    pr_number = data["pull_request"]["number"]
    pr_title = data["pull_request"]["title"]
    installation_id = data["installation"]["id"]

    # Step 4: get auth token and fetch diff
    token = await get_installation_token(installation_id)
    pr_data = await fetch_pr_diff(repo_full_name, pr_number, token)

    # Step 5: store in database
    async with AsyncSessionLocal() as session:
        pr = PullRequest(
            repo=repo_full_name,
            pr_number=pr_number,
            pr_title=pr_title
        )
        session.add(pr)
        await session.commit()

    print(f"✅ PR #{pr_number} from {repo_full_name} received and stored")
    print(f"   Diff length: {len(pr_data['diff'])} chars")
    print(f"   Files changed: {list(pr_data['files'].keys())}")

    return {"status": "received", "pr": pr_number, "repo": repo_full_name}