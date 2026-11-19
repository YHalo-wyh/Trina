# CTF skill 来源与版本记录

最后核对：2026-11-12

## 1. 核心技能包（ctf-playbook 的 references + 顶层 skill）

- Upstream: `https://github.com/ljagiello/ctf-skills`
- License: MIT
- 本次核对的上游 HEAD: `a16e19a`（2026-11-12，`2ad85d3..a16e19a` 仅改动 `evals/` 依赖，
  技能内容与 `2ad85d3` 相同）
- 上一版记录: `2ad85d368a86c2bc1ce13143a3768f4e55fd1c61`（2026-11-10 刷新）

核对结果（按内容归一化后逐文件比对，共 192 个文件）：

| 状态 | 数量 | 说明 |
|---|---|---|
| 与上游 HEAD 完全一致 | 175 | 已是最新 |
| 已同步（本次更新） | 2 | `ctf-skills-scripts/generate_catalog.py`（原 52d1edd 版）、`skill_security_auditor.py`（原 ed2a356 版） |
| 本次新增 | 1 | `ctf-skills-scripts/verify_crypto_examples.py`（上游 scripts/ 新增） |
| 本地刻意保留的差异 | 13 | 见下方「已知偏差」 |
| 本地未安装 | 1 | `ctf-malware/scripts-and-obfuscation.md` |

## 2. 已知偏差（有意为之，不要"修正"）

1. **13 个 SKILL.md 去掉了 `compatibility:` 字段**：上游按 Agent Skills 规范加了这个字段，
   但本机 Codex 的 skill 校验只看 `name` / `description` / `metadata`，其它已装 skill 也都不带该字段。
   学习与检索内容与上游一致。
2. **`solve-challenge/SKILL.md` 导语改写**：上游是 "You're a skilled CTF player..."，本地为
   "Solve the challenge and recover the flag from reproducible evidence."（更贴合 Codex 的指令语气）。
3. **`ctf-malware/tools-mcp.md` 去掉了指向 `scripts-and-obfuscation.md` 的两处链接**：
   该文件因含大量恶意样本代码被 Windows Defender 拦截，未安装。malware 入口本身仍保留精简指引。

## 3. Web 源码审计技能包（references/web-source）

### PHP（35 个 skill，39/40 文件与上游一致）

- Upstream: `https://github.com/0xShe/PHP-Code-Audit-Skill`
- 上游 HEAD: `69d883e`（2026-03-24）
- 未安装：仓库根目录 `README.md`（非技能内容）
- 结论：**已是最新**，无需更新

### Java（9 个 skill，约 691KB）

- 本地内容：`java-audit-pipeline / java-auth-audit / java-file-read-audit / java-file-upload-audit /
  java-route-mapper / java-route-tracer / java-sql-audit / java-vuln-scanner / java-xxe-audit`
  （含 JWT、Shiro、Spring Security、MyBatis、Struts、XXE 等分册）
- 可追溯的上游：`https://github.com/RuoJi6/java-audit-skills`（GitHub 已重命名/转向）
- 现上游 HEAD: `8a66d3c`（2026-06-16 "Rename skill to audit-skills"），只剩
  `skills/audit-skills/`（5 文件 / 82KB：SKILL.md + java.md 2.6KB + net.md + 48KB 漏洞 YAML + 扫描脚本），
  定位是「最小化轻量化 skill，只负责安全边界」，**不包含**本地这 9 个分册。
- 在 `DaoYiSec/SecSkills` 索引里的候选仓库（`yunmengya/PHP_AUDIT_SKILLS`、`youki992/zh-audit-skills-hub`、
  `AuroraProudmoore/java-audit-skill`）均不含这 9 个目录，故本地这套 Java 分册**没有可验证的上游**。
- 结论：**保持原样**。用上游的 82KB 轻量版替换 691KB 分册属于能力降级，不是"升级"。
  如需切换，命令见下（会删除 9 个分册）：

  ```powershell
  # 备份后切换为上游轻量版（二选一，执行前先确认）
  Copy-Item "$env:USERPROFILE\.codex\skills\ctf-playbook\references\web-source\java" `
            "$env:USERPROFILE\.codex\skills\.backup-java-audit-20261112" -Recurse
  Remove-Item "$env:USERPROFILE\.codex\skills\ctf-playbook\references\web-source\java" -Recurse
  # 再从 8a66d3c 的 skills/audit-skills 复制进去，并同步修改 references/web-source/router.md 的链接
  ```

## 4. 维护方式

```powershell
git clone --depth 1 https://github.com/ljagiello/ctf-skills.git $env:TEMP\ctf-skills-upstream
# 按内容（CRLF → LF）逐文件比对，只同步真正变化的文件，保留第 2 节的 13 处偏差
```

`verify_crypto_examples.py` 的默认根目录是仓库里的 `ctf-crypto`，在本机会话式布局下需要显式指定：

```powershell
python "$env:USERPROFILE\.codex\skills\ctf-skills-scripts\verify_crypto_examples.py" `
       --root "$env:USERPROFILE\.codex\skills\ctf-playbook\references\categories\crypto"
# 2026-11-12 实跑：Checked 370 fences: 361 syntax OK, 60 skipped (9 sage fallback, 51 oracle/network)
```
