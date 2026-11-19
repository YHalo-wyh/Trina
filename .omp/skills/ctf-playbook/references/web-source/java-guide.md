# Java White-Box Guide

Use this guide for Java source-review challenges, Spring or Servlet web audit
tasks, and CTF web problems that ship Java application code, `.class`, or
`.jar` artifacts.

## Start Order

1. Open `java-route-mapper/SKILL.md` to identify routes, frameworks, and
   parameter structures.
2. Open `java-auth-audit/SKILL.md` to map Spring Security, Shiro, session, JWT,
   filter, and interceptor behavior.
3. Open `java-vuln-scanner/SKILL.md` when dependency versions or known CVEs may
   be part of the challenge.
4. Open `java-route-tracer/SKILL.md` before making strong exploitability claims
   about sink reachability.
5. Use the sink-specific playbooks only after route and auth context is clear.

## When Only Binaries Are Available

- Prefer source code if it exists.
- If only `.class` or `.jar` files are available, use the decompile-strategy
  references bundled inside the Java playbooks.
- Use `java-vuln-scanner/scripts/scan_dependencies.py` when dependency scanning
  is useful and the local environment supports it.

## Sink-Focused Playbooks

- Route and path modeling: `java-route-mapper`, `java-route-tracer`
- Auth and access control: `java-auth-audit`
- SQL injection and dynamic query review: `java-sql-audit`
- File handling: `java-file-upload-audit`, `java-file-read-audit`
- XML parsing and XXE: `java-xxe-audit`
- Dependency and component risk: `java-vuln-scanner`
- Full orchestration reference: `java-audit-pipeline`

## Shared References

- `shared/OUTPUT_STANDARD.md` for output style
- `shared/SEVERITY_RATING.md` for severity calibration

## Working Rules

- Map routes and auth before picking sink-specific playbooks.
- Track parameter controllability across controller, service, and DAO layers.
- Keep binary-only findings grounded in decompile evidence, not guesses.
- Use a second category reference when the Java task also needs crypto, reverse,
  or generic web exploitation techniques.
