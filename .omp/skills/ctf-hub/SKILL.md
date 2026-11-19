---
name: ctf-hub
description: Unified local entrypoint for CTF problem solving and audit routing. Use when Codex needs to triage a CTF challenge, choose the right local playbook by challenge category or artifact type, solve standard CTF tasks (web, pwn, crypto, reverse, forensics, OSINT, malware, misc), or audit PHP/Java web source code in CTF and security competition scenarios.
---

# CTF Hub

Use this skill as the single local router for CTF work.

## Quick Start

1. Identify the input shape first: challenge files, remote service, web URL, source tree, or mixed artifacts.
2. Read `references/router.md` to classify the task and find the first local playbook to open.
3. If the task is still ambiguous, open `references/upstreams/ctf-skills/solve-challenge/SKILL.md` and let it drive the initial recon.
4. Load only the relevant vendored playbook after classification; do not bulk-read all upstream skills.

## Route By Task Type

### Standard CTF categories

For normal category-based challenges, read the matching vendored playbook:

- Web exploitation: `references/upstreams/ctf-skills/ctf-web/SKILL.md`
- Binary exploitation / pwn: `references/upstreams/ctf-skills/ctf-pwn/SKILL.md`
- Crypto: `references/upstreams/ctf-skills/ctf-crypto/SKILL.md`
- Reverse engineering: `references/upstreams/ctf-skills/ctf-reverse/SKILL.md`
- Forensics / stego / PCAP / disk / memory: `references/upstreams/ctf-skills/ctf-forensics/SKILL.md`
- OSINT: `references/upstreams/ctf-skills/ctf-osint/SKILL.md`
- Malware: `references/upstreams/ctf-skills/ctf-malware/SKILL.md`
- Jails / encodings / misc hybrids: `references/upstreams/ctf-skills/ctf-misc/SKILL.md`

### AWDP / AWD pwn (攻防兼备)

If the challenge ships an `update.sh`, a `xx.tar.gz` patch bundle, a bundled `flag`, or the event
is AWDP/AWD with per-round attack+defense:

1. Read `references/awdp-pwn.md` **first** — it covers the patch-bundle format, minimal-patch
   recipes, and attack-side discipline that plain CTF playbooks do not.
2. Then read `references/upstreams/ctf-skills/ctf-pwn/SKILL.md` for the exploitation primitives.
3. Patch tooling: `references/upstreams/tools/AwdPwnPatcher/` (x86/x64/arm/aarch64/mips) and
   `references/upstreams/tools/ToPaWaf/` (amd64 generic WAF).

### Live GDB debugging (pwno-mcp)

For hands-on binary debugging (registers, heap, memory, stepping, pwncli sessions), this machine
has a **pwno-mcp** server wired into OMP over HTTP. Read `references/mcp-pwno.md` before driving it —
it lists the 36 tools, the call sequence, and two environment traps (debuginfod stalling the MI
channel, and a leftover inferior corrupting it) that otherwise surface as confusing GDB timeouts.

### PHP white-box web auditing

If the target is a PHP codebase or a PHP-heavy web challenge:

1. Read `references/php-web-audit.md`.
2. If the scope is broad or unclear, open `references/upstreams/php-code-audit-skill/php-audit-pipeline/SKILL.md`.
3. If the user asks about one concrete class of bug, jump straight to the matching `php-*-audit/SKILL.md` file listed in `references/php-web-audit.md`.

### Java white-box web auditing

If the target is a Java codebase, `.jar`, `.class`, Spring app, Servlet app, or other Java web service:

1. Read `references/java-web-audit.md`.
2. If the scope is broad or unclear, open `references/upstreams/java-audit-skills/skills/java-audit-pipeline/SKILL.md`.
3. If the user asks about one concrete class of bug, jump straight to the matching `java-*-audit/SKILL.md` file listed in `references/java-web-audit.md`.

## Operating Rules

- Triage before exploiting. Confirm the category from files, extensions, service behavior, framework markers, and challenge text.
- Prefer the vendored local copies under `references/upstreams/` over refetching from the network.
- Pivot across categories when the evidence points there. Many challenges combine web + crypto, reverse + pwn, or forensics + OSINT.
- Start from the pipeline playbooks for PHP and Java when the repository is unfamiliar or the vuln class is not yet known.
- Use the narrower vuln-specific audit playbooks only after you already know the sink family or the user asked for a targeted check.

## Local Resources

- `references/router.md`: top-level decision tree
- `references/php-web-audit.md`: PHP routing and vuln-to-playbook mapping
- `references/java-web-audit.md`: Java routing and vuln-to-playbook mapping
- `references/upstream-sources.md`: upstream URLs and local vendored paths
- `references/upstreams/`: offline copies of the three upstream repositories
- `scripts/sync_upstreams.sh`: refresh vendored copies from GitHub
