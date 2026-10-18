#!/usr/bin/env bash
# Deploy the latest commit of $DEPLOY_BRANCH (default balls) on the Vultr box and restart the server in tmux.
# .github/workflows/deploy.yml runs this over SSH on every push; it can also be run by hand.
# Everything is inside main() so bash has read the whole file before `git reset` can rewrite it.
set -euo pipefail

main() {
  cd "$(dirname "$(readlink -f "$0")")"
  exec 9>/tmp/dubhacks-deploy.lock
  flock 9 # one deploy at a time

  local branch=${DEPLOY_BRANCH:-balls}
  local before after
  before=$(git rev-parse HEAD)
  git fetch --quiet origin "$branch"
  git reset --quiet --hard "origin/$branch" # the server's checkout always matches GitHub
  after=$(git rev-parse HEAD)
  echo "deploying $branch: ${before:0:7} -> ${after:0:7}"

  if [ "$before" != "$after" ] && ! git diff --quiet "$before" "$after" -- package.json package-lock.json; then
    npm install --no-audit --no-fund --loglevel=error
  fi
  if [ "$before" != "$after" ] && ! git diff --quiet "$before" "$after" -- model/requirements.txt; then
    .venv/bin/pip install --quiet -r model/requirements.txt
  fi

  # The server runs in window 0 of tmux session "dubhacks"; stop it there and start it again.
  local start='USE_CHAIN=1 npm start 2>&1 | tee .server.log'
  if tmux has-session -t dubhacks 2>/dev/null; then
    tmux send-keys -t dubhacks:0 C-c
    for _ in $(seq 1 30); do
      ss -ltn | grep -qE ':3000 |:8899 ' || break
      sleep 1
    done
  else
    tmux new-session -d -s dubhacks -c "$PWD"
  fi
  tmux send-keys -t dubhacks:0 "export PATH=\"\$HOME/.local/share/solana/install/active_release/bin:\$PATH\"; $start" Enter

  for _ in $(seq 1 120); do
    if curl -sf -m 2 http://127.0.0.1:3000/v1/health | grep -q '"ready":true'; then
      echo "server is up on ${after:0:7}"
      return 0
    fi
    sleep 1
  done
  echo "server did not report ready within 120s; recent log:" >&2
  tail -n 40 .server.log >&2
  return 1
}

main "$@"
exit
