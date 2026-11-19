# PHP White-Box Guide

Use this guide for PHP source-review challenges, web source audit tasks, and
CTF web problems that ship PHP application code.

## Start Order

1. Open `php-route-mapper/SKILL.md` to enumerate routes, handlers, and
   parameter shapes.
2. Open `php-auth-audit/SKILL.md` to map auth, access control, and risky
   unauthenticated routes.
3. Open `php-vuln-scanner/SKILL.md` if dependency versions or known framework
   CVEs may matter.
4. If the framework is obvious, open one matching framework playbook:
   `php-laravel-audit`, `php-symfony-audit`, `php-wordpress-audit`,
   `php-codeigniter-audit`, `php-thinkphp-audit`, or `php-yii-audit`.
5. Open `php-route-tracer/SKILL.md` before making high-confidence exploitability
   claims about route-driven sinks.

## Shared References

Open these only when needed:

- `shared/PHP_SINK_REFERENCE.md` for sink families and what to prove
- `shared/EVIDENCE_POINT_IDS.md` for evidence naming and trace alignment
- `shared/IO_PATH_CONVENTION.md` for output and path conventions
- `shared/SEVERITY_RATING.md` for severity calibration

## Sink-Focused Playbooks

Pick the smallest matching sub-playbook after route discovery and auth mapping:

- Injection: `php-sql-audit`, `php-nosql-audit`, `php-cmd-audit`,
  `php-ssrf-audit`, `php-xss-audit`, `php-tpl-audit`, `php-ldap-audit`,
  `php-expr-audit`, `php-xxe-audit`
- File and archive handling: `php-file-read-audit`, `php-file-upload-audit`,
  `php-file-write-audit`, `php-archive-extract-audit`, `php-filesystem-audit`
- Auth and response handling: `php-auth-audit`, `php-csrf-audit`,
  `php-open-redirect-audit`, `php-crlf-audit`, `php-session-cookie-audit`
- Broader risk areas: `php-config-audit`, `php-crypto-audit`,
  `php-logic-audit`, `php-logging-audit`, `php-deser-audit`
- Final chaining: `php-exploit-chain-audit`

## Working Rules

- Treat route mapping, auth mapping, and tracing as the spine of the workflow.
- Prefer evidence-driven conclusions over keyword-only findings.
- Keep static-only findings as candidate risks if the route trace or sink reach
  is incomplete.
- Use framework-specific playbooks to catch config and convention bugs that a
  generic sink search may miss.
