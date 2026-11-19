# Upstream Sources

This skill vendors three upstream repositories as offline references.

## Source repositories

- `https://github.com/ljagiello/ctf-skills`
- `https://github.com/0xShe/PHP-Code-Audit-Skill`
- `https://github.com/RuoJi6/java-audit-skills`
- `https://github.com/aftern00n/AwdPwnPatcher` (AWDP 二进制 patch 工具，本地修了 2 个 bug)
- `https://github.com/ErodedElk/ToPaWaf` (AWD/AWDP amd64 通防)

## Local vendored paths

- `references/upstreams/ctf-skills/`
- `references/upstreams/php-code-audit-skill/`
- `references/upstreams/java-audit-skills/`
- `references/upstreams/tools/AwdPwnPatcher/`
- `references/upstreams/tools/ToPaWaf/`

## Refresh workflow

Run:

```bash
./scripts/sync_upstreams.sh
```

The script refreshes each vendored copy from its GitHub archive and replaces the local directory in place.
