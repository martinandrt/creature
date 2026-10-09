#!/bin/bash
# the board over every home, refreshed every 10 s; homes are found again each time (new ones appear)
export UV_CACHE_DIR="$TMPDIR/uv-cache"
cd "$HOME/Desktop/creature" || exit 1
while true; do
  homes=()
  while IFS= read -r runs; do homes+=("$(dirname "$runs")"); done < <(
    find "$HOME/Desktop/creature-homes" "$HOME/Desktop" -maxdepth 3 -type d -name runs -path "*creature-*" 2>/dev/null | sort -u)
  .venv/bin/python -m creature.board "$HOME/Desktop/creature-homes/board.html" "${homes[@]}" > /dev/null 2>&1
  sleep 10
done
