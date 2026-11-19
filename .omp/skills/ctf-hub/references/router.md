# CTF Routing Catalog

Use this file to decide which local playbook to open first.

## Default order

1. If the category is unknown, read `upstreams/ctf-skills/solve-challenge/SKILL.md`.
2. If the task is obviously PHP or Java white-box auditing, jump to `php-web-audit.md` or `java-web-audit.md`.
3. Otherwise open the category playbook from `upstreams/ctf-skills/`.

## Routing table

| Situation | Strong signals | Open first |
| --- | --- | --- |
| Unknown or mixed CTF challenge | Only a zip, one-line prompt, remote endpoint, or mixed files | `upstreams/ctf-skills/solve-challenge/SKILL.md` |
| Web exploitation | HTTP endpoint, HTML/JS/PHP templates, cookies, JWT, upload logic, SSTI, SSRF, SQLi | `upstreams/ctf-skills/ctf-web/SKILL.md` |
| Binary exploitation | ELF/PE binary, remote service, crash on long input, ROP/heap/shellcode hints | `upstreams/ctf-skills/ctf-pwn/SKILL.md` |
| Cryptography | `enc.py`, `sage`, primes, ciphertexts, AES/RSA/ECC/PRNG hints | `upstreams/ctf-skills/ctf-crypto/SKILL.md` |
| Reverse engineering | `.exe`, `.apk`, `.wasm`, `.pyc`, obfuscated binary, VM, anti-debug | `upstreams/ctf-skills/ctf-reverse/SKILL.md` |
| Forensics / stego | `.pcap`, `.pcapng`, `.dd`, `.raw`, image/audio/video puzzles, disk or memory dump | `upstreams/ctf-skills/ctf-forensics/SKILL.md` |
| OSINT | Person/place/time/source lookup, geolocation, image search, username tracing | `upstreams/ctf-skills/ctf-osint/SKILL.md` |
| Malware | Packed script/binary, C2 traffic, PE triage, config extraction, sandbox evasion | `upstreams/ctf-skills/ctf-malware/SKILL.md` |
| Jail / encoding / misc | Python jail, shell jail, weird encoding, logic game, sandbox escape | `upstreams/ctf-skills/ctf-misc/SKILL.md` |
| PHP white-box audit | `composer.json`, `routes/*.php`, Laravel/Symfony/ThinkPHP/Yii/WordPress markers | `php-web-audit.md` |
| Java white-box audit | `pom.xml`, `build.gradle`, `.jar`, `.class`, Spring MVC, Servlet, Struts | `java-web-audit.md` |
| AWDP / AWD pwn（攻防兼备、修补包） | `update.sh`、`xx.tar.gz` 修补包、每轮攻防、题目自带 `flag` 与 Dockerfile、赛制为 AWDP | `awdp-pwn.md`（**先于** ctf-pwn 读） |

## File and directory heuristics

- If the workspace is already split into folders like `web/`, `pwn/`, `crypto/`, `reverse/`, use the matching category playbook first.
- If the artifact is a source tree rather than a single challenge file, favor the language-specific audit path over the generic category path.
- If a web challenge ships full application source, decide between:
  - generic `ctf-web` for black-box exploitation, narrow source review, or mixed-stack web tasks
  - `php-web-audit.md` or `java-web-audit.md` for structured white-box repository audits

## Cross-category pivots

- Web + Crypto: JWT forgery, HMAC bypass, custom signature logic, password reset tokens
- Web + Reverse: WASM, obfuscated JavaScript, native modules, APK-backed endpoints
- Reverse + Pwn: reverse the protocol or binary state machine before exploitation
- Forensics + Crypto: recover keys or decode encrypted blobs from captures or dumps
- Forensics + OSINT: extract clues locally, then verify via public sources
- Misc + Crypto: jail escapes that require arithmetic, encoding tricks, or custom primitives

## Local vendored roots

- Generic CTF playbooks: `upstreams/ctf-skills/`
- PHP audit playbooks: `upstreams/php-code-audit-skill/`
- Java audit playbooks: `upstreams/java-audit-skills/skills/`
- AWDP pwn 修补工具: `upstreams/tools/AwdPwnPatcher/`（跨架构 patch）、`upstreams/tools/ToPaWaf/`（amd64 通防）
