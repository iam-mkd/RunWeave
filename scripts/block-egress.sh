#!/usr/bin/env bash
# Makes the rest of a GitHub Actions job offline (.github/workflows/examples.yml): every
# proxy variable points at a closed local port, loopback stays reachable, Node's fetch is
# told to follow the variables, and no provider key may be set. Then it proves the block.
set -euo pipefail

blocked="${BLOCKED_PROXY:-http://127.0.0.1:9}"
port="${blocked##*:}"

for key in OPENAI_API_KEY ANTHROPIC_API_KEY; do
  if [ -n "${!key:-}" ]; then
    echo "::error::$key is set; the examples must run without provider keys"
    exit 1
  fi
done

if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
  echo "::error::Something listens on 127.0.0.1:$port; it cannot act as a closed proxy"
  exit 1
fi

{
  for name in HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy; do
    echo "$name=$blocked"
  done
  echo "NO_PROXY=127.0.0.1,localhost"
  echo "no_proxy=127.0.0.1,localhost"
  echo "NODE_USE_ENV_PROXY=1"
} >> "$GITHUB_ENV"

for url in https://api.openai.com/v1/models https://api.anthropic.com/v1/models; do
  if HTTPS_PROXY="$blocked" https_proxy="$blocked" curl -sS --max-time 10 -o /dev/null "$url"; then
    echo "::error::$url was reachable through $blocked"
    exit 1
  fi
done
echo "Outbound traffic now goes to $blocked (closed); loopback is exempt."
