"""Analyze eligible GitHub changes and publish an AI-generated issue.

The workflow sends change metadata and a bounded diff to an external model.
Model output is validated before it is posted to GitHub.
"""

import json
import os
import re
import time
from urllib.parse import quote

MODEL_NAME = "Llama-3.3-70B-Instruct"
ENDPOINT = "https://models.inference.ai.azure.com/chat/completions"


def collect_diff(files, limit):
    """Collect changed paths and bound the patch text sent to the model."""
    paths = []
    chunks = []
    remaining = limit
    truncated = False
    for file in files:
        paths.append(file.filename)
        chunk = f"File: {file.filename}\nPatch:\n{file.patch or '[Patch unavailable]'}\n\n"
        if len(chunk) > remaining:
            truncated = True
        if remaining:
            chunks.append(chunk[:remaining])
            remaining -= min(len(chunk), remaining)
    return "".join(chunks) + ("\n[Diff truncated...]" if truncated else ""), paths


def detect_change_type(files: list, diff: str, context: str) -> str:
    """Determine the analysis role based on changed file paths and diff content.

    Inspects the list of changed files and diff text to infer what kind of
    review is most appropriate. Returns a role key that maps to a specific
    prompt persona.

    Args:
        files: List of changed file paths.
        diff: The accumulated diff text.
        context: Commit message or PR title/body.

    Returns:
        A string role key: "security", "deps", "ci", "docs", "frontend",
        "backend", "config", or "general".
    """
    files_lower = [f.lower() for f in files]
    diff_lower = diff.lower()

    has_security_keywords = any(kw in diff_lower for kw in [
        "secret", "token", "password", "api_key", "apikey", "auth",
        "credential", "private_key", "access_key", "bearer",
        "vulnerability", "cve-", "injection", "xss", "csrf",
    ])
    has_security_files = any(f for f in files_lower if any(kw in f for kw in [
        "security", "auth", ".env", "secret",
    ]))
    if has_security_keywords or has_security_files:
        return "security"

    has_dep_files = any(f for f in files_lower if any(kw in f for kw in [
        "requirements", "package.json", "package-lock", "pipfile",
        "poetry.lock", "cargo.toml", "go.sum", "gemfile",
        "dependabot", "renovate",
    ]))
    if has_dep_files:
        return "deps"

    has_ci_files = any(f for f in files_lower if any(kw in f for kw in [
        ".github/workflows/", "jenkinsfile", ".gitlab-ci",
        ".circleci", "dockerfile", "docker-compose",
        ".github/actions/",
    ]))
    if has_ci_files:
        return "ci"

    has_doc_files = all(
        any(kw in f for kw in [
            "readme", "contributing", "changelog", "license",
            "docs/", "doc/", ".md", ".rst", ".txt",
        ])
        for f in files_lower
    )
    if has_doc_files and files_lower:
        return "docs"

    has_frontend_files = any(f for f in files_lower if any(kw in f for kw in [
        ".html", ".css", ".jsx", ".tsx", ".vue", ".svelte",
        "frontend/", "public/", "static/", "assets/",
    ]))
    has_frontend_keywords = any(kw in diff_lower for kw in [
        "classname", "style=", "onclick", "addeventlistener",
        "document.get", "innerhtml", "appendchild",
    ])
    if has_frontend_files or has_frontend_keywords:
        return "frontend"

    has_backend_files = any(f for f in files_lower if any(kw in f for kw in [
        ".py", ".go", ".java", ".rb", ".rs", ".php",
        "api/", "server/", "backend/", "lib/", "src/",
    ]))
    if has_backend_files:
        return "backend"

    has_config_files = any(f for f in files_lower if any(kw in f for kw in [
        ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf",
        ".json", ".env",
    ]))
    if has_config_files:
        return "config"

    return "general"


# ---------------------------------------------------------------------------
# Severity guidance shared across all prompt roles.
# ---------------------------------------------------------------------------
severity_guide = """
Use the following severity scale. You have 7 levels — pick the one that best matches the ACTUAL impact:

CRITICAL — Production-breaking changes, security vulnerabilities (exposed secrets, SQL injection, XSS, CSRF, broken auth), data loss risks, or changes that could cause service outages.

HIGH — Significant logic changes affecting core functionality, new or modified API endpoints, permission and access control modifications, database schema changes, removal of important functionality, breaking changes to public interfaces.

ELEVATED — New substantial features or modules, large-scale refactors that change behavior across multiple files, integration with external services or APIs, changes to error handling or retry logic, modifications to data processing pipelines.

MEDIUM — New utility functions or helpers, workflow and CI/CD pipeline changes, configuration changes that affect runtime behavior, dependency version updates, adding new files with meaningful functionality, structural reorganization of existing code.

MODERATE — Minor feature additions, small behavioral changes, adding validation or input checks, improving logging or error messages, updating environment variables or build settings, adding new labels or issue templates.

LOW — Documentation updates (README, CONTRIBUTING, comments), cosmetic UI changes without behavior impact, code formatting or style fixes, renaming without behavior change, adding badges or metadata, updating .gitignore or editor configs.

INFORMATIONAL — Trivial changes: fixing typos, whitespace adjustments, comment rewording, version bumps in non-critical files, adding blank lines.

IMPORTANT: Do NOT default to LOW or INFORMATIONAL. Carefully evaluate the actual scope and impact. Most code changes that add or modify functionality should be MEDIUM or higher. Use the full range of the scale.
"""

# ---------------------------------------------------------------------------
# Structured output contract sent to the model.
# ---------------------------------------------------------------------------
base_instructions = """
Return only a raw JSON object with no markdown formatting. The JSON must have these exact keys:

"issue_title": string — include severity prefix like [CRITICAL], [HIGH], [ELEVATED], [MEDIUM], [MODERATE], [LOW], or [INFO] at the start,
"severity": string — one of: critical, high, elevated, medium, moderate, low, informational,
"issue_body": string — must include these sections:
  ## Problem
  (clear description with exact file paths and line numbers if known)

  ## Code Reference
  (the exact problematic code snippet or the key changed code)

  ## Suggested Fix
  (concrete code or steps to fix — or "No action required" for informational changes)

  ## Permalink
  (placeholder: PUT_PERMALINK_HERE — will be replaced automatically)

"labels": list of strings — standard GitHub labels plus the severity level,
"affected_file": string — the most relevant filename from the diff (or "" if unknown),
"affected_line": integer — approximate line number of the issue (or 1 if unknown),
"summary": string — 2-3 sentence plain-English summary for the PR comment

The issue_title, issue_body and summary MUST be written entirely in English.
"""

# ---------------------------------------------------------------------------
# Prompt role definitions — keyed by label match or auto-detected type.
# ---------------------------------------------------------------------------
PROMPT_ROLES = {
    "security": (
        "Act as a Strict Security Auditor. Perform a deep security audit "
        "(OWASP Top 10, CWE patterns). Find real vulnerabilities with exact "
        "file/line references. Check for exposed secrets, injection vectors, "
        "broken auth, insecure deserialization, and misconfigurations."
    ),
    "review": (
        "Act as a Strict Code Reviewer. Analyze code quality using SOLID, DRY, "
        "and KISS principles. Point to exact lines that violate these principles. "
        "Evaluate naming, error handling, and separation of concerns."
    ),
    "qa": (
        "Act as a QA Engineer. Identify edge cases, missing test coverage, "
        "untested error paths, and potential regressions. Reference exact "
        "functions and lines that need tests."
    ),
    "perf": (
        "Act as a Performance Expert. Analyze algorithmic complexity, identify "
        "O(n²) patterns, unnecessary allocations, N+1 queries, blocking I/O, "
        "and missed caching opportunities. Reference exact lines."
    ),
    "pm": (
        "Act as a Product Manager. Generate user-facing Release Notes with "
        "clear impact descriptions. Focus on what changed for the end user, "
        "not implementation details."
    ),
    "deps": (
        "Act as a Security & Dependency Auditor. Analyze all new or changed "
        "dependencies: check for known vulnerabilities (CVEs), license "
        "compatibility (MIT/Apache/GPL), package size impact, maintenance "
        "status, and whether each dep is actively maintained. Reference exact "
        "file and line where each dependency is added or changed."
    ),
    "arch": (
        "Act as a Software Architect. Review changes for architectural issues: "
        "violation of separation of concerns, tight coupling, wrong layer "
        "dependencies, anti-patterns (God object, spaghetti logic, magic "
        "numbers, circular dependencies). Reference exact files and lines."
    ),
    "ci": (
        "Act as a DevOps/CI Engineer. Review the CI/CD pipeline changes for "
        "correctness, security (token permissions, secret exposure), efficiency "
        "(caching, parallelism, job dependencies), and best practices. Check "
        "for overly broad permissions, missing timeout limits, and potential "
        "race conditions in workflows."
    ),
    "docs": (
        "Act as a Technical Writer. Review the documentation changes for "
        "completeness, accuracy, clarity, and consistency. Check that code "
        "examples are correct, links are valid, and the structure is logical. "
        "Note any missing sections or outdated information."
    ),
    "frontend": (
        "Act as a Frontend Engineer. Review the UI/UX changes for accessibility "
        "(a11y), responsive design, performance (bundle size, render blocking), "
        "XSS risks in DOM manipulation, proper event handling, and browser "
        "compatibility. Reference exact files and lines."
    ),
    "backend": (
        "Act as a Senior Backend Engineer. Review the changes for correctness, "
        "error handling, input validation, resource management (file handles, "
        "connections), concurrency safety, and API contract compliance. "
        "Reference exact files and lines."
    ),
    "config": (
        "Act as a Configuration & Infrastructure Reviewer. Analyze the config "
        "changes for correctness, security implications (exposed ports, "
        "permissive CORS, debug mode in production), consistency across "
        "environments, and potential breaking changes for existing deployments."
    ),
    "general": (
        "Act as a Senior Software Engineer reviewing a colleague's changes. "
        "Provide a thorough assessment covering: what was changed and why, "
        "whether the changes introduce any risks or bugs, code quality, "
        "and the overall scope and impact on the project."
    ),
}

# Maps commit-message bracket labels and PR labels to role keys.
LABEL_TO_ROLE = {
    "sec": "security", "security": "security", "audit": "security",
    "review": "review", "refactor": "review", "code-review": "review",
    "qa": "qa", "test": "qa", "testing": "qa",
    "perf": "perf", "performance": "perf", "optimize": "perf",
    "pm": "pm", "release": "pm", "product": "pm",
    "deps": "deps", "dependencies": "deps", "dep": "deps",
    "arch": "arch", "architecture": "arch",
    "ci": "ci", "devops": "ci", "pipeline": "ci", "workflow": "ci",
    "docs": "docs", "doc": "docs", "documentation": "docs",
    "frontend": "frontend", "ui": "frontend", "ux": "frontend", "css": "frontend",
    "backend": "backend", "api": "backend", "server": "backend",
    "config": "config", "infra": "config", "infrastructure": "config",
}

def validate_model_result(result):
    """Reject malformed model output before it reaches the GitHub API."""
    if not isinstance(result, dict):
        raise ValueError("Model output must be a JSON object")
    for field, maximum in (("issue_title", 256), ("issue_body", 60000)):
        value = result.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f"Model output has an invalid {field}")
    for field in ("severity", "affected_file", "summary"):
        if field in result and not isinstance(result[field], str):
            raise ValueError(f"Model output has an invalid {field}")
    if len(result.get("summary", "")) > 60000:
        raise ValueError("Model summary is too long")
    if "labels" in result and not isinstance(result["labels"], list):
        raise ValueError("Model labels must be a list")


def call_model(prompt: str, model_token: str, retries: int = 3, delay: int = 5) -> dict:
    """Send a review prompt to the hosted model and parse the JSON reply."""
    import requests

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {model_token}"
    }
    payload = {
        "messages": [
            {"role": "system", "content": "You are a professional software auditor. Always return valid JSON only. No markdown, no explanation, just the JSON object."},
            {"role": "user", "content": prompt}
        ],
        "model": MODEL_NAME,
        "temperature": 0.1
    }

    for attempt in range(retries):
        try:
            resp = requests.post(ENDPOINT, headers=headers, json=payload, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            raw = data['choices'][0]['message']['content'].strip()
            raw = re.sub(r'^```json\s*|```$', '', raw, flags=re.MULTILINE).strip()
            result = json.loads(raw)
            validate_model_result(result)
            return result
        except (requests.RequestException, ValueError, TypeError, KeyError, IndexError) as e:
            print(f"Attempt {attempt + 1} failed: {e}")
            if attempt < retries - 1:
                time.sleep(delay)

    raise RuntimeError("All model requests failed.")


def main():
    """Analyze an eligible event and publish the model response."""
    from github import Auth, Github

    gh_token = os.environ.get("GITHUB_TOKEN")
    model_token = os.environ.get("GH_MODELS_TOKEN")
    repo_name = os.environ.get("REPOSITORY")
    event_name = os.environ.get("EVENT_NAME")
    allowed_users = {user.strip().lower() for user in os.environ.get("ALLOWED_USER", "").split(",") if user.strip()}

    if event_name not in {"push", "pull_request"} or not allowed_users:
        print("No eligible event or allowed users configured. Skipping.")
        return
    if not all((gh_token, model_token, repo_name)):
        raise ValueError("GITHUB_TOKEN, GH_MODELS_TOKEN and REPOSITORY must be configured")

    gh = Github(auth=Auth.Token(gh_token))
    try:
        repo = gh.get_repo(repo_name)
        run_analysis(repo, repo_name, event_name, model_token, allowed_users)
    finally:
        gh.close()


def run_analysis(repo, repo_name, event_name, model_token, allowed_users):
    pr_ref = None
    if event_name == "push":
        actor_login = os.environ.get("GITHUB_ACTOR", "").strip().lower()
        if actor_login not in allowed_users:
            print(f"Action performed by {actor_login}, not in allowed list. Skipping.")
            return
        commit_sha = os.environ.get("COMMIT_SHA", "")
        if not commit_sha:
            raise ValueError("COMMIT_SHA must be configured for push events")
        commit = repo.get_commit(commit_sha)
        pr_match = re.search(r'\(#(\d+)\)', commit.commit.message)
        dedup_key = f"PR #{pr_match.group(1)}" if pr_match else f"commit:{commit_sha[:7]}"
        event_context = f"Commit Message: {commit.commit.message}"
        trigger_labels = [label.lower() for label in re.findall(r'\[(.*?)\]', commit.commit.message)]
        diff_text, changed_files = collect_diff(commit.files, 10000)
        sha = commit_sha
    else:
        pr_number = int(os.environ.get("PR_NUMBER", ""))
        pr_ref = repo.get_pull(pr_number)
        if pr_ref.user.login.strip().lower() not in allowed_users:
            print("Pull request author is not in the allowed list. Skipping.")
            return
        dedup_key = f"PR #{pr_number}"
        event_context = f"PR Title: {pr_ref.title}\nPR Body: {pr_ref.body or ''}"
        trigger_labels = [label.name.lower() for label in pr_ref.labels]
        diff_text, changed_files = collect_diff(pr_ref.get_files(), 80000)
        sha = pr_ref.head.sha

    if len(diff_text.strip()) < 50:
        print("Diff too small to analyze. Skipping.")
        return
    for issue in repo.get_issues(state="all"):
        if f"Generated from {dedup_key} |" in (issue.body or ""):
            print(f"Issue for {dedup_key} already exists (#{issue.number}), skipping.")
            return

    role_key = next((LABEL_TO_ROLE[label] for label in trigger_labels if label in LABEL_TO_ROLE), None)
    if not role_key:
        role_key = detect_change_type(changed_files, diff_text, event_context)
        print(f"Auto-detected change type: {role_key}")
    role_instruction = PROMPT_ROLES.get(role_key, PROMPT_ROLES["general"])
    prompt = f"""{role_instruction}

Do NOT invent problems that do not exist in the diff. Base your analysis strictly on what you see.
{severity_guide}
Context: {event_context}
Changed files: {', '.join(changed_files)}
Changes: {diff_text}
{base_instructions}"""
    result = call_model(prompt, model_token)
    title_keyword = result["issue_title"][:40].lower()
    for issue in repo.get_issues(state="closed"):
        if title_keyword in (issue.title or "").lower():
            print(f"Similar closed issue found: #{issue.number} — skipping.")
            return

    affected_file = result.get("affected_file", "")
    affected_line = result.get("affected_line", 1)
    if not isinstance(affected_line, int) or isinstance(affected_line, bool) or affected_line < 1:
        affected_line = 1
    if affected_file in changed_files:
        permalink = f"https://github.com/{repo_name}/blob/{sha}/{quote(affected_file, safe='/')}#L{affected_line}"
    else:
        permalink = "_No specific file identified_"
    issue_body = result["issue_body"].replace("PUT_PERMALINK_HERE", permalink)
    actor_name = os.environ.get("GITHUB_ACTOR", "unknown")
    footer = f"\n\n---\n*Generated from {dedup_key} | Auto-detected role: `{role_key}` | Processed by actor: {actor_name}*"

    severity = result.get("severity", "medium").lower()
    severity_label_map = {
        "critical": "severity: critical", "high": "severity: high",
        "elevated": "severity: elevated", "medium": "severity: medium",
        "moderate": "severity: moderate", "low": "severity: low",
        "informational": "severity: informational",
    }
    available_labels = {label.name for label in repo.get_labels()}
    extra_labels = [severity_label_map.get(severity, "severity: medium")]
    all_labels = sorted({label for label in result.get("labels", []) + extra_labels
                         if isinstance(label, str) and label in available_labels})
    issue = repo.create_issue(title=result["issue_title"], body=issue_body + footer, labels=all_labels)
    print(f"Created issue #{issue.number}: {issue.title}")

    if pr_ref and result.get("summary"):
        comment = (f"### AI Analysis Summary\n\n{result['summary']}\n\n"
                   f"**Severity:** `{severity.upper()}` | **Role:** `{role_key}`\n\n"
                   f"Full details: #{issue.number}")
        pr_ref.create_issue_comment(comment)
        print(f"Posted summary comment to PR #{pr_ref.number}")


if __name__ == "__main__":
    main()
