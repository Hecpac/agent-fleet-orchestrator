#!/usr/bin/env bash
set -euo pipefail

echo "System:"
sw_vers
uname -m

echo
echo "Hardware:"
sysctl -n hw.memsize hw.physicalcpu hw.logicalcpu machdep.cpu.brand_string

echo
echo "Ollama:"
if command -v ollama >/dev/null 2>&1; then
  ollama --version
  ollama list
else
  echo "ollama not found"
fi

echo
echo "Optional tools:"
for tool in cmux just python3 jq; do
  if command -v "$tool" >/dev/null 2>&1; then
    printf "%-10s %s\n" "$tool" "$(command -v "$tool")"
  else
    printf "%-10s missing\n" "$tool"
  fi
done
