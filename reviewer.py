import ast
import os
import json
from groq import Groq

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

SKIP_PATTERNS = [
    "generated", "vendor", "node_modules", "migrations",
    "package-lock.json", "yarn.lock", ".min.js", ".min.css"
]

def should_skip_file(filename: str) -> bool:
    """Skip files that don't need review."""
    return any(pattern in filename.lower() for pattern in SKIP_PATTERNS)

def get_complexity_score(code: str, filename: str) -> int:
    """
    Use Python's built-in AST to count branches in Python files.
    Returns a complexity score — higher means more complex.
    Only works on Python files; returns 0 for others.
    """
    if not filename.endswith(".py"):
        return 0
    try:
        tree = ast.parse(code)
        score = 0
        for node in ast.walk(tree):
            # Each branch adds complexity
            if isinstance(node, (
                ast.If, ast.For, ast.While, ast.Try,
                ast.ExceptHandler, ast.With, ast.Assert
            )):
                score += 1
        return score
    except SyntaxError:
        return 0

def build_prompt(filename: str, diff: str, full_code: str) -> str:
    """Build the review prompt sent to the LLM."""
    return f"""You are a senior software engineer doing a thorough code review.

You will review the following file change and provide specific, actionable feedback.

FILE: {filename}

DIFF (what changed):
{diff}

FULL FILE CONTENT (for context):
{full_code[:3000]}

Review the code for:
1. Bugs or logic errors
2. Security vulnerabilities (SQL injection, XSS, exposed secrets, etc.)
3. Missing error handling
4. Performance issues
5. Code clarity and maintainability

Respond ONLY with a JSON array. No explanation, no markdown, just raw JSON.
Each item must have exactly these fields:
- "line": the approximate line number in the file (integer)
- "severity": one of "critical", "warning", or "suggestion"
- "comment": a specific, actionable comment explaining the issue and how to fix it

Example format:
[
  {{"line": 12, "severity": "critical", "comment": "This function divides by 'count' which could be zero if the list is empty. Add a check: if count == 0: return None"}},
  {{"line": 28, "severity": "warning", "comment": "User input is used directly in the query without sanitization. Use parameterized queries instead."}}
]

If there are no issues, return an empty array: []
"""

def parse_llm_response(response_text: str) -> list:
    """Safely parse the LLM's JSON response."""
    try:
        # Strip any accidental markdown code fences
        clean = response_text.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        clean = clean.strip()
        
        comments = json.loads(clean)
        
        # Validate structure
        valid = []
        for item in comments:
            if all(k in item for k in ["line", "severity", "comment"]):
                if item["severity"] in ["critical", "warning", "suggestion"]:
                    valid.append(item)
        return valid
    except (json.JSONDecodeError, KeyError, TypeError):
        return []

async def review_pr(diff: str, files: dict) -> list:
    """
    Main review function.
    Takes the PR diff and file contents, returns list of review comments.
    """
    all_comments = []

    for filename, full_code in files.items():
        # Skip generated/vendor files
        if should_skip_file(filename):
            print(f"   ⏭️  Skipping {filename}")
            continue

        # Get complexity score — only send complex files to LLM
        complexity = get_complexity_score(full_code, filename)
        print(f"   📊 {filename} — complexity score: {complexity}")

        # Build prompt and call Groq
        prompt = build_prompt(filename, diff, full_code)

        try:
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,  # Low temperature = more consistent, less creative
                max_tokens=1000
            )
            raw = response.choices[0].message.content
            print(f"   🤖 LLM response for {filename}: {raw[:100]}...")

            comments = parse_llm_response(raw)
            
            # Tag each comment with the filename
            for c in comments:
                c["filename"] = filename
            
            all_comments.extend(comments)
            print(f"   ✅ Found {len(comments)} issues in {filename}")

        except Exception as e:
            print(f"   ❌ Error reviewing {filename}: {e}")
            continue

    return all_comments