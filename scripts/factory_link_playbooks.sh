#!/usr/bin/env bash
# Link each channel's private playbook into styles/custom/ so it loads by name.
# Channel content lives in channels/ (a separate private repo, see FACTORY.md).
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$root/styles/custom"
for pb in "$root"/channels/*/brand/playbook.yaml; do
  [ -e "$pb" ] || continue
  channel="$(basename "$(dirname "$(dirname "$pb")")")"
  [ "$channel" = "_template" ] && continue
  ln -sfn "../../channels/$channel/brand/playbook.yaml" "$root/styles/custom/$channel.yaml"
  echo "linked styles/custom/$channel.yaml"
done
