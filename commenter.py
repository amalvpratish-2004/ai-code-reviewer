import httpx

SEVERITY_EMOJI = {
    "critical": "🔴 **Critical**",
    "warning": "🟡 **Warning**",
    "suggestion": "🟢 **Suggestion**"
}

async def post_review(
    repo_full_name: str,
    pr_number: int,
    token: str,
    comments: list,
    diff: str
) -> None:
    """Post review comments back to the GitHub PR."""
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json"
    }

    # Build summary counts
    critical = sum(1 for c in comments if c["severity"] == "critical")
    warnings = sum(1 for c in comments if c["severity"] == "warning")
    suggestions = sum(1 for c in comments if c["severity"] == "suggestion")

    # Build summary comment body
    if not comments:
        summary = "## 🤖 AI Code Review\n\n✅ **Looks good!** No issues found."
    else:
        summary = f"""## 🤖 AI Code Review

| Severity | Count |
|----------|-------|
| 🔴 Critical | {critical} |
| 🟡 Warning | {warnings} |
| 🟢 Suggestion | {suggestions} |

> Powered by Llama 3.1 70B via Groq
"""

    async with httpx.AsyncClient() as client:
        # Post the summary as a regular PR comment
        await client.post(
            f"https://api.github.com/repos/{repo_full_name}/issues/{pr_number}/comments",
            headers=headers,
            json={"body": summary}
        )

        # Post each individual comment as a review comment on the specific file
        for c in comments:
            body = f"{SEVERITY_EMOJI[c['severity']]}\n\n{c['comment']}"
            
            # Try to post as inline comment on the file
            # Falls back gracefully if line mapping fails
            try:
                await client.post(
                    f"https://api.github.com/repos/{repo_full_name}/issues/{pr_number}/comments",
                    headers=headers,
                    json={"body": f"**`{c['filename']}`** (line ~{c['line']})\n\n{body}"}
                )
            except Exception as e:
                print(f"   ❌ Failed to post comment: {e}")

    print(f"   ✅ Posted review: {critical} critical, {warnings} warnings, {suggestions} suggestions")