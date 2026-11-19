# Web Source Router

Use this router only for white-box or source-assisted web challenges.

For black-box web exploitation, start with
`references/categories/web/SKILL.md` instead.

## Pick PHP

Start with [php-guide.md](php-guide.md) when the project shows one or more of
these signals:

- `.php` files dominate the codebase
- `composer.json` or `composer.lock` is present
- framework markers such as Laravel, Symfony, WordPress, ThinkPHP,
  CodeIgniter, or Yii
- request handling revolves around `$_GET`, `$_POST`, `$_REQUEST`,
  `$_FILES`, cookies, or `php://input`

## Pick Java

Start with [java-guide.md](java-guide.md) when the project shows one or more of
these signals:

- `pom.xml`, `build.gradle`, or `settings.gradle` is present
- `src/main/java`, `WEB-INF`, `.class`, or `.jar` is present
- framework markers such as Spring MVC, Servlet, JAX-RS, Struts, or Shiro
- controllers, filters, interceptors, and service classes dominate the entry
  points

## Shared Rule

Route source challenges by framework and entry points first, then by sink type.
Do not jump directly to a sink-specific playbook before you know:

1. where the route or handler lives
2. how authentication is enforced
3. whether the input is really controllable
4. whether the sink is actually reachable
