#!/bin/bash
# The interpreters alone, pinned to one P-core (CPU 2): ADR 0030's interpreter.py (parsing batches of reported runs, as
# recording does) and loop.py (no library at all), each the median of 9 passes. Rotated rounds, so no candidate always
# runs first or hottest; each line is one run, with the hottest thermal zone just after it.
#   ./screen.sh ROUNDS IMAGE...      "host" is the machine's /usr/bin/python3.12
here=$(cd "$(dirname "$0")" && pwd); interp="$here/../dataplane-image-2026-10-30/interpreter.py"
rounds=$1; shift; cands=("$@"); n=${#cands[@]}
for r in $(seq 1 "$rounds"); do
  for k in $(seq 0 $((n-1))); do
    c=${cands[$(( (k + r) % n ))]}
    for w in interpreter loop; do
      script="$here/loop.py"; [ "$w" = interpreter ] && script="$interp"
      if [ "$c" = host ]; then out=$(taskset -c 2 /usr/bin/python3.12 "$script")
      else out=$(docker run --rm --cpuset-cpus 2 --network none -v "$script:/m.py:ro" --entrypoint sh "$c" \
                 -c 'exec $(command -v python3 || command -v python) /m.py'); fi
      t=$(cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | sort -n | tail -1)
      echo "{\"round\":$r,\"cand\":\"$c\",\"work\":\"$w\",\"ms\":${out% ms},\"temp_mC\":${t:-0}}"
    done
  done
done
