import os
import time
import jwt
import httpx

def generate_jwt() -> str:
    app_id = os.getenv("GITHUB_APP_ID")
    # Read from env var, replace literal \n with real newlines
    private_key = os.getenv("GITHUB_PRIVATE_KEY", "").replace("\\n", "\n")

    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + 600,
        "iss": app_id
    }
    return jwt.encode(payload, private_key, algorithm="RS256")

async def get_installation_token(installation_id: int) -> str:
    token = generate_jwt()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json"
            }
        )
        return resp.json()["token"]