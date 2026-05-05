import hmac
import hashlib
import os
import httpx
from fastapi import Request, HTTPException

def verify_signature(payload: bytes, signature: str) -> bool:
    secret = os.getenv("GITHUB_WEBHOOK_SECRET").encode()
    expected = "sha256=" + hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)

async def fetch_pr_diff(repo_full_name: str, pr_number: int, token: str) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3.diff",
    }
    async with httpx.AsyncClient() as client:
        # Fetch the diff
        diff_resp = await client.get(
            f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}",
            headers=headers
        )
        diff_text = diff_resp.text

        # Fetch list of changed files
        files_resp = await client.get(
            f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/files",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        )
        files = files_resp.json()

        # Fetch full content of each changed file
        file_contents = {}
        for f in files:
            if f.get("status") == "removed":
                continue
            content_resp = await client.get(
                f["contents_url"],
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
            )
            content_data = content_resp.json()
            if content_data.get("encoding") == "base64":
                import base64
                file_contents[f["filename"]] = base64.b64decode(
                    content_data["content"]
                ).decode("utf-8", errors="ignore")

    return {"diff": diff_text, "files": file_contents}