#!/bin/sh
# Turns on the factory's independent Codex review (ENG-156, docs/review.md).
# Run from the root of the software-factory checkout, on main, after the
# review workflow is merged. Safe to run again: steps already done are
# skipped. It stops only where you must act:
#   1. pasting the OpenAI API key (hidden; it goes straight into GitHub),
#   2. pasting the factory's review token (hidden; it goes straight into Fly).
# Needs: gh (signed in as you) and fly (signed in), both on this Mac.
set -eu
PATH="$HOME/.fly/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
APP=rnavarrete-factory
REPO=rNavarrete/software-factory
ENV=codex-review
# The model is a setting, not code. Check it is on OpenAI's Codex model list
# (https://learn.chatgpt.com/docs/models) before turning the review on.
MODEL="${MODEL:-gpt-6.1-sol}"

if [ ! -f deploy/fly/fly.toml ] || [ ! -f .github/workflows/codex-review.yml ]; then
    echo "Run this from the root of the software-factory folder, on main." >&2
    exit 1
fi
gh auth status >/dev/null 2>&1 || { echo "Sign in to gh first: gh auth login" >&2; exit 1; }
ME=$(gh api user --jq .login)

hidden() {
    stty -echo </dev/tty
    read -r VALUE </dev/tty
    stty echo </dev/tty
    echo
}

echo "== GitHub: the review's protected environment (only main can use it)"
if ! gh api "repos/$REPO/environments/$ENV" >/dev/null 2>&1; then
    gh api -X PUT "repos/$REPO/environments/$ENV" --silent \
        -F 'deployment_branch_policy[protected_branches]=false' \
        -F 'deployment_branch_policy[custom_branch_policies]=true'
    gh api -X POST "repos/$REPO/environments/$ENV/deployment-branch-policies" --silent \
        -f name=main -f type=branch
fi

echo "== GitHub: the OpenAI API key, kept only in that environment"
if ! gh secret list --repo "$REPO" --env "$ENV" | grep -q '^OPENAI_API_KEY'; then
    echo "Paste the OpenAI API key and press Enter (nothing will show):"
    hidden
    printf '%s' "$VALUE" | gh secret set OPENAI_API_KEY --repo "$REPO" --env "$ENV"
    unset VALUE
fi

have() { fly secrets list --app "$APP" | grep -q "$1"; }

echo "== Fly: who starts reviews, and which model"
if ! have FACTORY_REVIEW_DISPATCHER; then
    printf 'FACTORY_REVIEW_DISPATCHER=%s\n' "$ME" | fly secrets import --app "$APP" --stage
fi
if ! have FACTORY_REVIEW_MODEL; then
    printf 'FACTORY_REVIEW_MODEL=%s\n' "$MODEL" | fly secrets import --app "$APP" --stage
fi

echo "== Fly: the factory's review token"
if ! have FACTORY_REVIEW_TOKEN; then
    echo "Paste the review token (github_pat_...) and press Enter (nothing will show):"
    hidden
    case "$VALUE" in
        github_pat_*) ;;
        *) echo "That isn't a fine-grained GitHub token." >&2; exit 1 ;;
    esac
    printf 'FACTORY_REVIEW_TOKEN=%s\n' "$VALUE" | fly secrets import --app "$APP" --stage
    unset VALUE
fi

echo "== Deploy (one machine only)"
fly deploy . --app "$APP" --config deploy/fly/fly.toml \
    --dockerfile deploy/fly/Dockerfile --ha=false

echo
echo "Done. Reviews start on the next worker PR. Nothing has been run or paid for yet."
