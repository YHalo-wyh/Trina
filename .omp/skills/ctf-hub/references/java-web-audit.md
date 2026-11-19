# Java Web Audit Routing

Use this file when the target is a Java repository, Java web challenge, or compiled Java artifact.

## Start here

1. If the repository scope is broad or the vuln class is unknown, open `upstreams/java-audit-skills/skills/java-audit-pipeline/SKILL.md`.
2. If the task is route and parameter discovery, open `upstreams/java-audit-skills/skills/java-route-mapper/SKILL.md`.
3. If the task is auth analysis or permission bypass, open `upstreams/java-audit-skills/skills/java-auth-audit/SKILL.md`.
4. If the task needs controller-to-sink tracing, open `upstreams/java-audit-skills/skills/java-route-tracer/SKILL.md`.
5. If the vuln class is already known, jump directly to the matching audit playbook below.

## Vuln-to-playbook map

| Need | Open |
| --- | --- |
| SQL injection | `upstreams/java-audit-skills/skills/java-sql-audit/SKILL.md` |
| File upload | `upstreams/java-audit-skills/skills/java-file-upload-audit/SKILL.md` |
| Arbitrary file read / path traversal | `upstreams/java-audit-skills/skills/java-file-read-audit/SKILL.md` |
| XXE | `upstreams/java-audit-skills/skills/java-xxe-audit/SKILL.md` |
| Auth / access control | `upstreams/java-audit-skills/skills/java-auth-audit/SKILL.md` |
| Dependency CVEs / component issues | `upstreams/java-audit-skills/skills/java-vuln-scanner/SKILL.md` |

## Selection rules

- Prefer the pipeline when the user asks for a full Java project audit or when the target framework is not yet clear.
- Prefer the narrow `java-*-audit` playbook when the sink family is already known or when you already have a traced route to inspect.
- If the target is only `.jar` or `.class`, note that the upstream playbooks expect `java-decompile-mcp`; if it is unavailable, fall back to source-level inspection and standard CLI decompilers while following the same routing logic.
- Read `upstreams/java-audit-skills/skills/shared/` or `references/` files only when the selected playbook points there.
