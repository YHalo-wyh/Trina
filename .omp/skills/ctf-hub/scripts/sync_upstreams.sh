#!/usr/bin/env bash
set -euo pipefail

skill_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
vendor_root="$skill_dir/references/upstreams"
tmp_root="$(mktemp -d)"
trap 'rm -rf "$tmp_root"' EXIT

download_repo() {
  local owner="$1"
  local repo="$2"
  local ref="$3"
  local dest_name="$4"
  local zip_path="$tmp_root/${dest_name}.zip"
  local extract_root="$tmp_root/extracted-${dest_name}"
  local url="https://codeload.github.com/${owner}/${repo}/zip/refs/heads/${ref}"

  mkdir -p "$extract_root"
  echo "Downloading ${owner}/${repo}@${ref}"
  curl --http1.1 -LfsS -o "$zip_path" "$url"
  unzip -oq "$zip_path" -d "$extract_root"

  local top_dir
  top_dir="$(find "$extract_root" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  if [[ -z "${top_dir}" ]]; then
    echo "Failed to extract ${owner}/${repo}" >&2
    return 1
  fi

  rm -rf "$vendor_root/$dest_name"
  mkdir -p "$(dirname "$vendor_root/$dest_name")"
  mv "$top_dir" "$vendor_root/$dest_name"
}

# ctf-pwn/SKILL.md is a LOCAL ENRICHMENT of upstream (machine's WSL toolchain
# table + ~70 inline technique sections). Stash it so a refresh cannot lose it.
pwn_skill="$vendor_root/ctf-skills/ctf-pwn/SKILL.md"
pwn_skill_stash=""
if [[ -f "$pwn_skill" ]]; then
  pwn_skill_stash="$tmp_root/SKILL.md.enriched"
  cp "$pwn_skill" "$pwn_skill_stash"
fi

download_repo "ljagiello" "ctf-skills" "main" "ctf-skills"
download_repo "0xShe" "PHP-Code-Audit-Skill" "main" "php-code-audit-skill"
download_repo "RuoJi6" "java-audit-skills" "main" "java-audit-skills"
download_repo "aftern00n" "AwdPwnPatcher" "master" "tools/AwdPwnPatcher"
download_repo "ErodedElk" "ToPaWaf" "main" "tools/ToPaWaf"

# --- Restore local customizations ------------------------------------------
if [[ -n "$pwn_skill_stash" ]]; then
  cp "$pwn_skill_stash" "$pwn_skill"
  echo "Restored enriched ctf-pwn/SKILL.md (re-merge any new upstream sections by hand)"
fi

# Same for the upstream bug fixes in AwdPwnPatcher.fix_eh_frame_flags(); see
# references/awdp-pwn.md for what they are and why the trampoline SIGSEGVs without them.
if ! grep -q "self.binary.mmap\[offset:offset + 4\]" \
     "$vendor_root/tools/AwdPwnPatcher/AwdPwnPatcher.py"; then
  echo "WARNING: AwdPwnPatcher reverted to upstream; re-apply the fix_eh_frame_flags()"
  echo "         fix documented in references/awdp-pwn.md, or the trampoline will SIGSEGV."
fi

echo "Vendored upstreams refreshed under $vendor_root"
