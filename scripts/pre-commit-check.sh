#!/usr/bin/env bash
# Pre-commit hook for tv-tracker.
#
# Enforces the git hygiene rules from CLAUDE.md so that the repo stays safe
# to promote from private to public. If this hook blocks a commit, fix the
# underlying issue — do not bypass with --no-verify.
#
# Install:
#   ln -s ../../scripts/pre-commit-check.sh .git/hooks/pre-commit

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

FAIL=0
fail() {
    echo "pre-commit: $1" >&2
    FAIL=1
}

STAGED_FILES="$(git diff --cached --name-only --diff-filter=ACMR)"

if [ -z "$STAGED_FILES" ]; then
    exit 0
fi

# ---------------------------------------------------------------------------
# Rule 1: reject staged files that are supposed to be git-ignored.
# ---------------------------------------------------------------------------
while IFS= read -r f; do
    [ -z "$f" ] && continue
    if git check-ignore -q "$f" 2>/dev/null; then
        fail "staged file '$f' matches a .gitignore pattern — refusing to commit"
    fi
done <<< "$STAGED_FILES"

# ---------------------------------------------------------------------------
# Rule 2: reject commits that introduce credential-ish filenames.
# ---------------------------------------------------------------------------
while IFS= read -r f; do
    [ -z "$f" ] && continue
    case "$f" in
        *credentials*|*secret*|*token*|*api_key*|*.key|*.pem|*.p12|*.pfx|*.env|*.env.*)
            fail "staged file '$f' looks like credentials — refusing to commit"
            ;;
    esac
done <<< "$STAGED_FILES"

# ---------------------------------------------------------------------------
# Rule 3: grep staged diffs for data that belongs in baselines/, not git.
# ---------------------------------------------------------------------------
DIFF_CONTENT="$(git diff --cached --no-color -U0 --diff-filter=ACMR -- '*.py' '*.md' '*.html' '*.json' '*.txt' '*.sh' '*.js' '*.css' 2>/dev/null || true)"

if [ -n "$DIFF_CONTENT" ]; then
    ADDED="$(printf '%s\n' "$DIFF_CONTENT" | grep -E '^\+[^+]' || true)"

    if [ -n "$ADDED" ]; then
        # A real TMDB v3 API key is a 32-char lowercase hex string; a TMDB v4
        # token is a long JWT. Catch both shapes near a tmdb mention.
        if printf '%s\n' "$ADDED" | grep -qiE 'tmdb[^[:alnum:]]{0,20}[a-f0-9]{32}'; then
            fail "staged diff contains what looks like a real TMDB API key — keep it in baselines/tmdb_api_key"
        fi
        if printf '%s\n' "$ADDED" | grep -qE 'eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}'; then
            fail "staged diff contains what looks like a JWT bearer token — sanitize before committing"
        fi
    fi
fi

# Sensitive GDPR-export filenames must never appear in test code or fixtures
# (docs and this hook legitimately name them when explaining the rules).
TESTS_ADDED="$(git diff --cached --no-color -U0 --diff-filter=ACMR -- 'tests/' 2>/dev/null | grep -E '^\+[^+]' || true)"
if [ -n "$TESTS_ADDED" ]; then
    if printf '%s\n' "$TESTS_ADDED" | grep -qE 'access_token\.csv|refresh_token\.csv|user_personal_data\.csv'; then
        fail "staged test/fixture diff references sensitive GDPR-export files — fixtures must not embed them"
    fi
fi

# ---------------------------------------------------------------------------
# Rule 4: run the test suite (fast, offline by design).
# ---------------------------------------------------------------------------
if [ -d "tests" ] && [ -d ".venv" ]; then
    if ! .venv/bin/pytest -q -m 'not network' >/dev/null 2>&1; then
        fail "pytest failed — commit blocked (run .venv/bin/pytest -m 'not network' to see why)"
    fi
fi

if [ "$FAIL" -ne 0 ]; then
    echo "" >&2
    echo "pre-commit: commit blocked. Fix the issues above." >&2
    echo "pre-commit: do NOT bypass with --no-verify — that's exactly what this hook exists to prevent." >&2
    exit 1
fi

exit 0
