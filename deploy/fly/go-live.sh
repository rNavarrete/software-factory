#!/bin/sh
# Switches the factory from its practice run to real work on the pilot
# project, then takes the first real ticket through its first steps
# (docs/go-live.md). Run from the root of the software-factory checkout, on
# main, after the go-live change is merged. Safe to run again: whatever is
# already done is skipped, and an interrupted run picks up where it stopped.
# It stops only where you must act:
#   1. the factory's own Linear key (hidden; it goes straight into Fly),
#   2. replacing the worker routine's saved instructions (in your browser),
#   3. a usage reading from the factory's claude.ai account,
#   4. two Todo moves and one edit in Linear, and answering one question.
# Needs: fly and gh (both signed in), git, curl, and the routine's start key
# in Keychain (where the routine setup put it).
set -eu
PATH="$PATH:$HOME/.fly/bin:/opt/homebrew/bin:/usr/local/bin"
APP=rnavarrete-factory
REPO=rNavarrete/software-factory
ROUTINE=trig_01CHWbQ267i1CMLGUym1kGd9
ROLANDO=cd9ec650-f957-4f25-b5f0-9c14bcae49c8
WORK=ENG-200   # the real sample ticket: one worker starts for it
EDITED=ENG-202 # edited after its Todo move: it must never start
PROBED=ENG-201 # changed only through the Linear connector, to see how Linear records it
RECORD=/data/go-live
# How often to look at the service, and how long to watch after a restart.
POLL=${POLL:-30}
RESTART_WAIT=${RESTART_WAIT:-200}
# Your keyboard, even if this script's input is redirected.
exec 3<"${FACTORY_TTY:-/dev/tty}"

say() { printf '\n== %s\n' "$*"; }
stop() {
    printf '\nStopped: %s\n' "$*" >&2
    exit 1
}
ask() {
    printf '%s' "$1"
    IFS= read -r ANSWER <&3 || ANSWER=""
}
hidden() {
    printf '%s' "$1"
    stty -echo <&3 2>/dev/null || true
    IFS= read -r VALUE <&3 || VALUE=""
    stty echo <&3 2>/dev/null || true
    echo
}
trap 'stty echo <&3 2>/dev/null || true' EXIT
trap 'exit 130' HUP INT TERM

# One command on the service machine, as root. Fly's SSH tunnel sometimes
# times out right after a deploy, so it is tried three times.
on_host() {
    for _ in 1 2 3; do
        if OUT=$(fly ssh console --app "$APP" -C "$1" 2>/dev/null); then
            printf '%s\n' "$OUT" | tr -d '\r'
            return 0
        fi
        sleep 10
    done
    stop "Fly's SSH connection keeps failing. Run this script again in a minute."
}
secrets_now() { SECRETS=$(fly secrets list --app "$APP"); }
have() { printf '%s\n' "$SECRETS" | grep -qw "$1"; }
# Deploys exactly the commit on main: a clean export, so untracked files in
# this folder never reach the image.
deploy() {
    BUILD=$(mktemp -d)
    git archive --format=tar HEAD | (cd "$BUILD" && tar -xf -)
    fly deploy "$BUILD" --app "$APP" --config "$BUILD/deploy/fly/fly.toml" \
        --dockerfile "$BUILD/deploy/fly/Dockerfile" --ha=false
    rm -rf "$BUILD"
}
# The running service's home: /data/factory once live mode is really running.
# These are plain assignments, so a failed look stops the script.
host_home() { HOME_NOW=$(on_host "cat /run/factory-home"); }
# One look at the service: its status and its queue.
look() {
    STATUS_NOW=$(on_host "/app/factory status")
    QUEUE_NOW=$(on_host "/app/factory queue")
}
# What that look says about fires: how many there are, and which launched a
# worker with a session ("launched" plus its link) or didn't (refused, unknown).
fires() { printf '%s\n' "$STATUS_NOW" | sed -n 's/^Fires on record: //p'; }
launched() { printf '%s\n' "$STATUS_NOW" | grep -c "^Fire $1-a[0-9]*-f[0-9]*: launched https://" || true; }
failed() { printf '%s\n' "$STATUS_NOW" | grep -E "^Fire $1-a[0-9]+-f[0-9]+: (not-launched|launch-outcome-unknown)" || true; }
fired_for() { printf '%s\n' "$STATUS_NOW" | grep -c "^Fire $1-" || true; }
last_item() { printf '%s\n' "$QUEUE_NOW" | awk -v k="$1" '$1 == k { line = $0 } END { print line }'; }
# Fly runs a command without a shell, so anything with shell syntax goes
# through sh -c.
recorded() { [ "$(on_host "sh -c 'cat $RECORD/$1 2>/dev/null || true'")" = "${2:-done}" ]; }
record() { on_host "sh -c 'mkdir -p $RECORD && echo ${2:-done} > $RECORD/$1'" >/dev/null; }

say "Checking where you are"
if [ ! -f deploy/fly/fly.toml ] || [ ! -f deploy/pilot/onboarding.json ]; then
    stop "Run this from the root of the software-factory folder."
fi
[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || stop "Switch to main first: git checkout main"
git fetch -q origin main
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || stop "Your copy of main isn't the latest. Run: git pull"
git diff --quiet HEAD -- controller deploy verify || stop "This folder has local changes. Deploy only what is on main."
grep -q '"intake_enabled": true' deploy/pilot/onboarding.json || stop "The go-live change isn't on main yet."
fly auth whoami >/dev/null 2>&1 || stop "Sign in to Fly first: fly auth login"
gh auth status >/dev/null 2>&1 || stop "Sign in to gh first: gh auth login"
CI=$(gh api "repos/$REPO/commits/$(git rev-parse HEAD)/check-runs" \
    --jq '[.check_runs[] | select(.name == "checks") | .conclusion] | first // "missing"')
[ "$CI" = success ] || stop "CI on main isn't green yet (checks: $CI). Wait for it, then run this again."
secrets_now
for name in FACTORY_REVIEW_DISPATCHER FACTORY_REVIEW_MODEL FACTORY_REVIEW_TOKEN; do
    have "$name" || stop "The Codex review isn't set up yet ($name is missing). Run first: sh deploy/fly/setup-reviewer.sh"
done
# Asks GitHub whether the review token may start the review, without starting
# one. An image too old to have this check prints neither line and is skipped.
TOKEN_CHECK=$(on_host "sh -c 'cd /app && FACTORY_SECRETS_DIR=/run/factory-secrets python3 -s -m controller.review check-token 2>&1 || true'")
case "$TOKEN_CHECK" in
    *"review token: not ok: "*)
        stop "The Codex review can't start: ${TOKEN_CHECK##*review token: not ok: }
On GitHub: Settings > Developer settings > Fine-grained tokens > the factory's review token > Edit.
Set Actions to \"Read and write\", click Update, then run this again." ;;
esac

say "1. The factory's own Linear login"
if have FACTORY_LINEAR_KEY; then
    echo "Already set."
else
    cat <<'EOF'
The factory needs its own Linear user, so nothing it writes can pass for yours.
In your browser:
  a. In Linear: Settings > Members > Invite. Invite rolando@hellotableread.com as a Member.
     If Linear asks you to pay for the seat, stop here and tell Claude in the thread.
  b. Open a private window, sign in to Linear as rolando@hellotableread.com, and accept.
  c. Still as that user: Settings > Security & access > Personal API keys > New API key.
     Name: factory. Permissions: choose only "Read" and "Create comments".
     If it offers team access, choose Engineering only.
  d. Copy the key and come back here.
EOF
    hidden "Paste the factory's Linear key and press Enter (nothing will show): "
    case "$VALUE" in
        lin_api_*) ;;
        *) stop "That isn't a Linear API key (they start with lin_api_)." ;;
    esac
    # The key goes to Linear on standard input, never on a command line.
    WHO=$(printf 'Authorization: %s\n' "$VALUE" | curl -sS --max-time 20 -H @- \
        -H 'Content-Type: application/json' --data '{"query":"{ viewer { id name } }"}' \
        https://api.linear.app/graphql) || stop "Couldn't reach Linear to check the key. Run this again."
    ID=$(printf '%s' "$WHO" | sed -n 's/.*"id" *: *"\([^"]*\)".*/\1/p')
    NAME=$(printf '%s' "$WHO" | sed -n 's/.*"name" *: *"\([^"]*\)".*/\1/p')
    [ -n "$ID" ] || stop "Linear didn't accept that key. Make a new one and run this again."
    [ "$ID" != "$ROLANDO" ] || stop "That key is yours. Make it while signed in as the factory's user."
    printf 'FACTORY_LINEAR_KEY=%s\n' "$VALUE" | fly secrets import --app "$APP" --stage >/dev/null
    VALUE=""
    echo "Saved in Fly. It acts as: $NAME"
fi

say "2. The worker routine's start key"
if have FACTORY_ROUTINE_TOKEN; then
    echo "Already set."
else
    VALUE=$(security find-generic-password -s software-factory -a "routine-token/$ROUTINE" -w 2>/dev/null) || VALUE=""
    if [ -z "$VALUE" ]; then
        echo "It isn't in your Keychain. On the factory claude.ai account, open the factory-worker"
        echo "routine at https://claude.ai/code/routines, then Edit, the API trigger, Generate token."
        hidden "Paste the start key and press Enter (nothing will show): "
    fi
    case "$VALUE" in
        sk-ant-*) ;;
        *) stop "That isn't a routine start key (they start with sk-ant-)." ;;
    esac
    printf 'FACTORY_ROUTINE_TOKEN=%s\n' "$VALUE" | fly secrets import --app "$APP" --stage >/dev/null
    VALUE=""
    echo "Copied into Fly without showing it."
fi

say "3. The worker routine's instructions"
prompt() { git show HEAD:controller/adapter/routine_prompt.md | awk 'f { print } /^---$/ { f = 1 }'; }
PROMPT_SHA=$(prompt | shasum -a 256 | cut -c1-64)
if recorded routine-prompt "$PROMPT_SHA"; then
    echo "Already the current version."
else
    prompt | pbcopy
    cat <<'EOF'
The routine's saved instructions predate repairs, so they would turn a repair away.
The current text is now on your clipboard.
  a. Signed in to the factory claude.ai account, open https://claude.ai/code/routines,
     then the factory-worker routine, then Edit.
  b. Click in Instructions, select all, and paste over it. Change nothing else. Save.
EOF
    ask "Press Enter once it is saved: "
    record routine-prompt "$PROMPT_SHA"
fi

host_home
if [ "$HOME_NOW" = /data/factory ]; then
    echo
    echo "The factory is already running live."
elif have FACTORY_MODE; then
    # Set (perhaps only staged) by an earlier run that stopped before its
    # deploy finished: the key checks already passed, so finish the switch.
    say "5. Switching to live"
    deploy
else
    say "4. Checking the new keys, still in practice mode"
    deploy
    OUT=$(on_host "sh -c 'cd /app && FACTORY_SECRETS_DIR=/run/factory-secrets python3 -s -m controller.intake probe $PROBED $ROLANDO'")
    printf '%s\n' "$OUT"
    if printf '%s\n' "$OUT" | grep -q PROBLEM; then
        stop "The factory's Linear key acts as you. Make a new one as the factory's user."
    fi
    printf '%s\n' "$OUT" | grep -q "^  20" || stop "Couldn't read $PROBED's history. Tell Claude in the thread."
    if printf '%s\n' "$OUT" | grep -q "counts as Rolando's own action"; then
        # Known limit Rolando accepted (2026-10-08, docs/go-live.md): the
        # connector signs in as him, so Linear can't tell its changes from his.
        # Claude sessions never move pilot tickets to Todo.
        echo "Known limit: Linear records the Linear connector's changes as yours."
        echo "Claude sessions never move pilot tickets to Todo."
    fi
    OUT=$(on_host "sh -c 'cd /app && FACTORY_SECRETS_DIR=/run/factory-secrets python3 -s -m controller.report probe ENG-178'")
    printf '%s\n' "$OUT"
    printf '%s\n' "$OUT" | grep -q '^OK:' || stop "The posting check failed. Tell Claude in the thread."

    say "5. Switching to live"
    printf 'FACTORY_MODE=live\n' | fly secrets import --app "$APP" --stage >/dev/null
    deploy
fi

say "6. Waiting for the live service"
i=0
while :; do
    host_home
    [ "$HOME_NOW" != /data/factory ] || break
    i=$((i + 1))
    [ "$i" -le 20 ] || stop "The live service didn't come up. Check: fly logs --app $APP --no-tail"
    sleep 15
done
STATUS=$(on_host "/app/factory status")
printf '%s\n' "$STATUS"
if printf '%s\n' "$STATUS" | grep -q "Usage reading: none recorded"; then
    echo
    echo "Workers wait until the factory has a usage reading. Signed in to the factory"
    echo "claude.ai account, open https://claude.ai/settings/usage and type what it shows."
    while :; do
        ask "Current session, percent used (just the number): "
        SESSION=$ANSWER
        ask "Weekly limit, percent used: "
        WEEKLY=$ANSWER
        ask "Extra usage credits spent, in dollars (0 if none): "
        CREDITS=$ANSWER
        if printf '%s %s %s\n' "$SESSION" "$WEEKLY" "$CREDITS" |
            grep -Eq '^[0-9]+(\.[0-9]+)? [0-9]+(\.[0-9]+)? [0-9]+(\.[0-9]+)?$'; then
            break
        fi
        echo "Please type plain numbers, like 12 or 0."
    done
    on_host "/app/factory snapshot $SESSION $WEEKLY $CREDITS"
    STATUS=$(on_host "/app/factory status")
fi
if printf '%s\n' "$STATUS" | grep -Eq '^(Blocks new work|Alert|Hold):'; then
    stop "Something is holding the factory (above). Tell Claude in the thread."
fi

say "7. The first real ticket: $WORK"
TASK=$(printf '%s' "$WORK" | tr 'A-Z' 'a-z')
look
if [ "$(last_item "$WORK")" = "" ] && [ "$(fired_for "$TASK")" = 0 ]; then
    echo "In Linear itself (not through Claude), move $WORK, \"Show how many books are on the list\","
    echo "to Todo. The factory looks every minute and waits two minutes after a move."
fi
ASKED=""
LOOKS=0
while :; do
    look
    [ "$(launched "$TASK")" = 0 ] || break
    BAD=$(failed "$TASK")
    [ -z "$BAD" ] || stop "A worker start for $WORK didn't launch: $BAD. The ticket says what's next. Tell Claude in the thread."
    LOOKS=$((LOOKS + 1))
    if [ "$LOOKS" = 40 ]; then
        echo "Still waiting for a worker to start. The newest comment on $WORK says why."
    fi
    LINE=$(last_item "$WORK")
    case "$LINE" in
        *" question")
            if [ "$ASKED" != "$LINE" ]; then
                echo
                echo "The factory asked a question on $WORK. Answer it by changing that line"
                echo "of the ticket's description, then move the ticket to Backlog and back to Todo."
                ASKED=$LINE
            fi
            ;;
        *" open" | "" | *" replaced") ;;
        *) stop "The factory stopped $WORK ($LINE). It says why on the ticket. Tell Claude in the thread." ;;
    esac
    sleep "$POLL"
done
echo "A worker started for $WORK:"
printf '%s\n' "$STATUS_NOW" | grep "^Fire $TASK-.*: launched"

say "8. A restart starts nothing twice"
if recorded restart-checked; then
    echo "Already checked."
else
    look
    BEFORE=$(fires)
    fly apps restart "$APP"
    sleep "$RESTART_WAIT"
    look
    AFTER=$(fires)
    [ "$AFTER" = "$BEFORE" ] || stop "Another worker started after the restart ($BEFORE, now $AFTER). Tell Claude now."
    record restart-checked
    echo "Restarted; still $AFTER fire(s) on record."
fi

say "9. An edited ticket is stopped before it starts: $EDITED"
if recorded edit-checked; then
    echo "Already checked."
else
    EDITED_TASK=$(printf '%s' "$EDITED" | tr 'A-Z' 'a-z')
    look
    BEFORE=$(fires)
    LINE=$(last_item "$EDITED")
    if [ -z "$LINE" ]; then
        echo "In Linear itself, move $EDITED, \"Qualification check: edited after the Todo move\", to Todo."
    fi
    TOLD=""
    while :; do
        look
        LINE=$(last_item "$EDITED")
        case "$LINE" in
            *" open")
                if [ -z "$TOLD" ]; then
                    echo "The factory queued it. Now add one word anywhere in $EDITED's description and save."
                    TOLD=yes
                fi
                ;;
            *" authorization-withdrawn") break ;;
            "") ;;
            *) stop "$EDITED ended another way ($LINE). Tell Claude in the thread." ;;
        esac
        [ "$(fired_for "$EDITED_TASK")" = 0 ] || stop "The factory fired a worker for $EDITED. Tell Claude now."
        [ "$(fires)" = "$BEFORE" ] || stop "Another fire happened while checking $EDITED. Tell Claude now."
        sleep "$POLL"
    done
    record edit-checked
    echo "Stopped before starting, as it should. You can move $EDITED back to Backlog."
fi

echo
echo "Done. The factory is live with one worker on $WORK, and the rest runs by itself:"
echo "the review, and one repair if the review fails. If a repair needs you to confirm"
echo "the first worker finished, $WORK will say so and give you the exact command."
