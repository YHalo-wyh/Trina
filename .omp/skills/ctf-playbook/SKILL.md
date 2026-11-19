---
name: ctf-playbook
description: Unified CTF challenge router and reference library for solving CTF, lab, and practice-box problems. Use when given a challenge description, binary, PCAP, image, archive, URL, source tree, model artifact, or service endpoint and you need to classify it as web, pwn, reverse, crypto, forensics, misc, osint, malware, or AI/ML, or when a web source challenge is clearly PHP or Java and needs a white-box audit workflow.
---

# CTF Playbook

## Overview

Classify the challenge first, then load only the smallest relevant reference set.
Use the bundled category references for standard CTF classes and the PHP or Java
white-box playbooks for source-based web challenges.

## Quick Start

1. Read [challenge-routing.md](references/challenge-routing.md) to classify the
   challenge from its files, keywords, and service behavior.
2. Open the matching category directory under `references/categories/` and start
   with that category's `SKILL.md`.
3. Load only the specific markdown files needed for the current technique.
4. If the challenge is a web source audit, read
   [web-source/router.md](references/web-source/router.md) and then the PHP or
   Java guide.
5. Reclassify and load a second category when the challenge is hybrid.

## Category Map

| Category | Start here | Common triggers |
| --- | --- | --- |
| AI/ML | [references/categories/ai-ml/SKILL.md](references/categories/ai-ml/SKILL.md) | model files, adversarial examples, prompt injection, jailbreaks, model extraction |
| Crypto | [references/categories/crypto/SKILL.md](references/categories/crypto/SKILL.md) | ciphertexts, primes, modular arithmetic, RSA, ECC, PRNG, Sage |
| Forensics | [references/categories/forensics/SKILL.md](references/categories/forensics/SKILL.md) | PCAP, disk image, memory dump, image/audio stego, event logs |
| Malware | [references/categories/malware/SKILL.md](references/categories/malware/SKILL.md) | packed samples, C2 traffic, obfuscated scripts, PE/.NET malware |
| Misc | [references/categories/misc/SKILL.md](references/categories/misc/SKILL.md) | pyjails, bashjails, encodings, SDR, VM/game puzzles, weird constraints |
| OSINT | [references/categories/osint/SKILL.md](references/categories/osint/SKILL.md) | geolocation, social media, usernames, DNS, public-record research |
| Pwn | [references/categories/pwn/SKILL.md](references/categories/pwn/SKILL.md) | overflow, ROP, heap, format string, sandbox escape, kernel |
| Reverse | [references/categories/reverse/SKILL.md](references/categories/reverse/SKILL.md) | binary analysis, WASM, bytecode, VMs, deobfuscation |
| Web | [references/categories/web/SKILL.md](references/categories/web/SKILL.md) | HTTP services, XSS, SQLi, SSRF, SSTI, auth bypass, black-box web |

## Web Source Challenges

Use the generic web category for black-box exploitation or source-assisted web
tasks that do not obviously belong to a language-specific audit flow.

Use [web-source/router.md](references/web-source/router.md) when the challenge
is a source review, framework audit, or white-box web task.

- Choose the PHP guide when the project is dominated by `.php`, `composer.json`,
  Laravel, Symfony, WordPress, ThinkPHP, CodeIgniter, Yii, or superglobals such
  as `$_GET` and `$_POST`.
- Choose the Java guide when the project is dominated by `pom.xml`,
  `build.gradle`, `src/main/java`, Spring MVC, Servlet, JAX-RS, `WEB-INF`,
  `.class`, or `.jar`.

## Working Rules

- Prefer artifact-driven classification over the challenge title alone.
- Keep a scratch list of files, endpoints, sinks, credentials, and candidate
  flags so pivots stay grounded in evidence.
- Load a second category reference when the first one explains only part of the
  challenge.
- Treat PHP and Java white-box findings as evidence-driven: do not mark a sink
  as confirmed exploitable just because a keyword matches.
- Validate flag candidates against the expected format and the artifact path
  where they were found.

## Bundled References

This skill bundles imported reference trees from three upstream sources. The
core CTF tree provenance is recorded in
[ctf-skills-source.md](references/ctf-skills-source.md).

- `ctf-skills` for broad CTF category coverage, including AI/ML
- `PHP-Code-Audit-Skill` for PHP white-box web workflows
- `java-audit-skills` for Java white-box web workflows
