# PHP Web Audit Routing

Use this file when the target is a PHP repository or a PHP-backed CTF web challenge.

## Start here

1. If the repository scope is broad or the vuln class is unknown, open `upstreams/php-code-audit-skill/php-audit-pipeline/SKILL.md`.
2. If the task is route discovery or request modeling, open `upstreams/php-code-audit-skill/php-route-mapper/SKILL.md`.
3. If the task is auth, permission bypass, or IDOR, open `upstreams/php-code-audit-skill/php-auth-audit/SKILL.md`.
4. If the task needs source-to-sink evidence, open `upstreams/php-code-audit-skill/php-route-tracer/SKILL.md`.
5. If the vuln class is already known, jump directly to the matching audit playbook below.

## Vuln-to-playbook map

| Need | Open |
| --- | --- |
| SQL injection | `upstreams/php-code-audit-skill/php-sql-audit/SKILL.md` |
| NoSQL injection | `upstreams/php-code-audit-skill/php-nosql-audit/SKILL.md` |
| Command injection | `upstreams/php-code-audit-skill/php-cmd-audit/SKILL.md` |
| SSRF | `upstreams/php-code-audit-skill/php-ssrf-audit/SKILL.md` |
| XSS | `upstreams/php-code-audit-skill/php-xss-audit/SKILL.md` |
| Arbitrary file read / path traversal | `upstreams/php-code-audit-skill/php-file-read-audit/SKILL.md` |
| File upload | `upstreams/php-code-audit-skill/php-file-upload-audit/SKILL.md` |
| Arbitrary file write | `upstreams/php-code-audit-skill/php-file-write-audit/SKILL.md` |
| Zip Slip / archive extraction | `upstreams/php-code-audit-skill/php-archive-extract-audit/SKILL.md` |
| XXE | `upstreams/php-code-audit-skill/php-xxe-audit/SKILL.md` |
| Deserialization / object injection | `upstreams/php-code-audit-skill/php-deser-audit/SKILL.md` |
| Template injection / SSTI | `upstreams/php-code-audit-skill/php-tpl-audit/SKILL.md` |
| LDAP injection | `upstreams/php-code-audit-skill/php-ldap-audit/SKILL.md` |
| Expression injection / eval / assert | `upstreams/php-code-audit-skill/php-expr-audit/SKILL.md` |
| CSRF | `upstreams/php-code-audit-skill/php-csrf-audit/SKILL.md` |
| Open redirect | `upstreams/php-code-audit-skill/php-open-redirect-audit/SKILL.md` |
| CRLF / response splitting | `upstreams/php-code-audit-skill/php-crlf-audit/SKILL.md` |
| Session / cookie issues | `upstreams/php-code-audit-skill/php-session-cookie-audit/SKILL.md` |
| Security config / CORS / debug / headers | `upstreams/php-code-audit-skill/php-config-audit/SKILL.md` |
| Crypto / key handling | `upstreams/php-code-audit-skill/php-crypto-audit/SKILL.md` |
| Business logic | `upstreams/php-code-audit-skill/php-logic-audit/SKILL.md` |
| Filesystem abuse / TOCTOU / symlink issues | `upstreams/php-code-audit-skill/php-filesystem-audit/SKILL.md` |
| Logging / audit trail issues | `upstreams/php-code-audit-skill/php-logging-audit/SKILL.md` |
| Dependency CVEs | `upstreams/php-code-audit-skill/php-vuln-scanner/SKILL.md` |
| Chain multiple findings into exploit narrative | `upstreams/php-code-audit-skill/php-exploit-chain-audit/SKILL.md` |

## Framework-specific playbooks

Open the matching framework playbook after or alongside the pipeline when framework markers are present:

- Laravel: `upstreams/php-code-audit-skill/php-laravel-audit/SKILL.md`
- Symfony: `upstreams/php-code-audit-skill/php-symfony-audit/SKILL.md`
- ThinkPHP: `upstreams/php-code-audit-skill/php-thinkphp-audit/SKILL.md`
- Yii: `upstreams/php-code-audit-skill/php-yii-audit/SKILL.md`
- WordPress: `upstreams/php-code-audit-skill/php-wordpress-audit/SKILL.md`
- CodeIgniter: `upstreams/php-code-audit-skill/php-codeigniter-audit/SKILL.md`

## Selection rules

- Prefer the pipeline when the user says "审一下这个 PHP 项目", "看看有没有洞", or "先全量摸一遍".
- Prefer the narrow `php-*-audit` playbook when the user already points to one sink family or one suspected vuln class.
- Read `upstreams/php-code-audit-skill/shared/` files only when the selected playbook refers to them; do not preload the whole shared directory.
