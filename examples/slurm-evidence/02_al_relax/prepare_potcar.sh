#!/usr/bin/env bash
set -euo pipefail

psp_root="${VASP_PSP_DIR:-${VASPLOOM_POTCAR_LIBRARY:-/public/sourcecode/vasp/potpaw_PBE.64}}"
variant="Al"
source_file=""

for candidate in "$psp_root/$variant/POTCAR" "$psp_root/POTCAR.$variant"; do
  if [ -f "$candidate" ]; then
    source_file="$candidate"
    break
  fi
done

if [ -z "$source_file" ]; then
  echo "POTCAR not found for $variant under: $psp_root" >&2
  echo "Set VASP_PSP_DIR to your licensed PBE PAW library." >&2
  exit 1
fi

cp "$source_file" POTCAR
echo "Created POTCAR from the licensed local library."
grep -m 1 "TITEL" POTCAR || true
sha256sum POTCAR

