# 验收矩阵与证据判读

规格版本 `1.0`，适用产品基线 `24fb637`。正文要求以 [规格](specification.md) 为准，机器关系清单为 [traceability.json](traceability.json)。本表把 What、场景、How、Sensor、Outcome 和 Evidence 连起来，公开测试的通过只适用于列明断言。

## 1. 要求、场景、任务与可观察结果

| Req ID | 场景 | 责任任务 | 可观察结果 | 适用边界 |
|---|---|---|---|---|
| REQ-01 任务输入与动作协议 | SC-P01、SC-N01 | TASK-01 | 合法对象进入执行；坏协议返回反馈，未知配置不被静默接受 | 按规格列明范围 |
| REQ-02 目标范围裁决 | SC-A01 | TASK-01 | 预检模型零调用，越界目标工具零调用 | 按规格列明范围 |
| REQ-03 注册项与撤权 | SC-N02、SC-A07 | TASK-01 | 非法注册项和撤权目标拒绝；不承诺完整参数绑定 | OQ-01、OQ-02 |
| REQ-04 工具契约与错误隔离 | SC-P01、SC-N03 | TASK-01 | 坏参数零执行，工具异常与超时形成结构化错误 | OQ-03 |
| REQ-05 动态风险与高危参数 | SC-A02 | TASK-02 | 等级和危险 payload 依据实际参数重新裁决 | 按规格列明范围 |
| REQ-06 HTTP 跳转与重试 | SC-A03 | TASK-02 | 跨源目的端零命中，方法及重试语义正确 | 按规格列明范围 |
| REQ-07 证据与验证对象 | SC-P02、SC-A04 | TASK-03 | 同对象带证据升级，阴性和超时不伪造或抹去事实 | OQ-09 |
| REQ-08 目标成功与报告结论 | SC-P03、SC-N04、SC-A04 | TASK-03 | 成功和 verdict 分开计算，无证据与受限不伪称安全 | OQ-01 |
| REQ-09 审计链与完整性 | SC-A05 | TASK-04 | 有效链通过，修改/缺失记录检出，外部真实性另验 | 按规格列明范围 |
| REQ-10 人工介入与拒绝 | SC-N05、SC-P04、SC-A07 | TASK-05 | 拒绝和超时不执行，提示进入模型，stop 限制新调度 | OQ-03 |
| REQ-11 预算与无进展停止 | SC-N06 | TASK-05 | 预算及重复事实触发停止，费用不声称硬限额 | OQ-03、OQ-06 |
| REQ-12 原子快照与兼容 | SC-N07 | TASK-06 | 替换失败旧文件不变，状态还原可读，不冒称自动续跑 | OQ-03 |
| REQ-13 调度与遵从反馈 | SC-P05 | TASK-06 | 当前覆盖驱动下一注册项，明确 ID 错配不算遵从 | OQ-01、OQ-04 |
| REQ-14 咨询建议边界 | SC-P06、SC-A06 | TASK-07 | 建议不执行不写事实，预算与回放有界 | OQ-06 |
| REQ-15 已实现位置的脱敏 | SC-P07 | TASK-04 | 审计/报告/子快照按列明范围保护口令 | OQ-05 |
| REQ-16 报告与只读复核 | SC-P08 | TASK-07 | 报告可读，review 指出无证据声明且不补测 | OQ-09 |
| REQ-17 真值与指标范围 | SC-P09 | TASK-08 | 计数和分母按已声明范围计算 | OQ-06 |
| REQ-18 两轮真实机制验证 | SC-P10 | TASK-08 | 新靶机、新端口和新运行均有证据与有效审计 | 按规格列明范围 |
| REQ-19 仓库追踪 | SC-N08 | TASK-08 | 引用和关系真实存在，坏引用隔离试验能失败 | 按规格列明范围 |
| REQ-20 人工连续性交接 | SC-P11 | TASK-08 | 字段与路径检查通过；异机复现和人工签署待完成 | OQ-07、OQ-08 |

## 2. 精确验证入口

以下测试 ID 使用 `unittest discover -s tests` 的实际命名。完整测试可用 V-FULL 运行，局部命令和退出语义见 [Harness](harness-manifest.md)。文件链接用于定位测试，代码中的函数名用于定位具体断言；不得用同名但未被发现的函数伪装测试证据。

### REQ-01 的 Sensor

执行入口：`V-FULL`。实现位置：[trina/brain/prompts.py](../../trina/brain/prompts.py)、[trina/config.py](../../trina/config.py)。

- `test_brain.TestParseAction.test_fenced_json`
- `test_brain.TestReActLoop.test_invalid_json_is_recovered`
- `test_collaboration.TestCollaboration.test_bad_budget_config_is_rejected`

事实文件：[unit-tests.json](../../evidence/20261021-course-pack/unit-tests.json)。执行状态按逐测试记录读取，未运行、失败和跳过不能计作通过。

### REQ-02 的 Sensor

执行入口：`V-FAST`、`V-FULL`。实现位置：[trina/scope.py](../../trina/scope.py)、[trina/brain/agent.py](../../trina/brain/agent.py)。

- `test_brain.TestReActLoop.test_out_of_scope_is_blocked_without_llm`
- `test_brain.TestReActLoop.test_mid_run_pivot_to_out_of_scope_is_denied`
- `test_scope.TestScopeDecisions.test_public_requires_switch_and_explicit_cidr`

事实文件：[unit-tests.json](../../evidence/20261021-course-pack/unit-tests.json)。执行状态按逐测试记录读取，未运行、失败和跳过不能计作通过。

### REQ-03 的 Sensor

执行入口：`V-POLICY`。实现位置：[trina/policy.py](../../trina/policy.py)、[configs/ranges/lab.yaml](../../configs/ranges/lab.yaml)。

- `test_policy.TestController.test_strict_requires_registered_test_id`
- `test_policy.TestController.test_strict_rejects_unregistered_target`
- `test_policy.TestController.test_test_id_tool_mismatch_is_denied`
- `test_policy.TestController.test_revocation_blocks_further_checks`

事实文件：[unit-tests.json](../../evidence/20261021-course-pack/unit-tests.json)。执行状态按逐测试记录读取，未运行、失败和跳过不能计作通过。

### REQ-04 的 Sensor

执行入口：`V-FULL`。实现位置：[trina/tools/registry.py](../../trina/tools/registry.py)、[trina/tools/__init__.py](../../trina/tools/__init__.py)。

- `test_registry.TestRegistry.test_unknown_param_rejected`
- `test_registry.TestRegistry.test_missing_required_rejected`
- `test_registry.TestRegistry.test_type_coercion_and_enum`
- `test_registry.TestRegistry.test_exception_isolated`
- `test_registry.TestRegistry.test_timeout`

事实文件：[unit-tests.json](../../evidence/20261021-course-pack/unit-tests.json)。执行状态按逐测试记录读取，未运行、失败和跳过不能计作通过。

### REQ-05 的 Sensor

执行入口：`V-FAST`、`V-FULL`。实现位置：[trina/scope.py](../../trina/scope.py)、[trina/tools/external.py](../../trina/tools/external.py)、[trina/tools/creds.py](../../trina/tools/creds.py)。

- `test_eval_report_lab.TestScopeAwareToolBehaviour.test_dynamic_level_enforced_by_registry`
- `test_scope.TestForbiddenPatterns.test_payload_in_query_string_is_scanned`
- `test_scope.TestActionLevels.test_destructive_requires_host_allowlist`
- `test_scope.TestForbiddenPatterns.test_brute_scale_cap`

事实文件：[unit-tests.json](../../evidence/20261021-course-pack/unit-tests.json)。执行状态按逐测试记录读取，未运行、失败和跳过不能计作通过。

### REQ-06 的 Sensor

执行入口：`V-HTTP`。实现位置：[trina/tools/http.py](../../trina/tools/http.py)。

- `test_http.TestHttpRedirects.test_cross_origin_redirect_never_contacts_destination`
- `test_http.TestHttpRedirects.test_303_changes_post_to_get_without_replaying_body`
- `test_http.TestHttpRedirects.test_307_preserves_form_and_redirect_cookie`
- `test_http.TestHttpRedirects.test_redirect_loop_is_bounded`
- `test_http.TestHttpRedirects.test_post_transport_error_is_not_retried`

事实文件：[unit-tests.json](../../evidence/20261021-course-pack/unit-tests.json)。执行状态按逐测试记录读取，未运行、失败和跳过不能计作通过。

### REQ-07 的 Sensor

执行入口：`V-FULL`。实现位置：[trina/hypotheses.py](../../trina/hypotheses.py)、[trina/state.py](../../trina/state.py)、[trina/tools/exploit.py](../../trina/tools/exploit.py)。

- `test_decision_evidence.TestEvidenceRun.test_detected_upgrade_confirms_same_id_and_flushes_live_ledger`
- `test_decision_evidence.TestEvidenceRun.test_unrelated_or_unevidenced_findings_do_not_confirm`
- `test_probe_integrity.TestProbeIntegrity.test_fixed_sql_error_page_is_not_injection`
- `test_probe_integrity.TestProbeIntegrity.test_reflected_parameter_is_not_command_execution`
- `test_decision_evidence.TestEvidenceDecisions.test_confirmation_cannot_be_erased_by_later_timeout`

事实文件：[unit-tests.json](../../evidence/20261021-course-pack/unit-tests.json)。执行状态按逐测试记录读取，未运行、失败和跳过不能计作通过。

### REQ-08 的 Sensor

执行入口：`V-POLICY`、`V-FULL`。实现位置：[trina/brain/agent.py](../../trina/brain/agent.py)、[trina/brain/goal.py](../../trina/brain/goal.py)。

- `test_decision_evidence.TestEvidenceRun.test_model_final_cannot_create_flag_or_success`
- `test_brain.TestReActLoop.test_flag_shortcut_does_not_satisfy_task_goal`
- `test_policy.TestVerdictAndReview.test_inconclusive_when_nothing_executed`
- `test_policy.TestVerdictAndReview.test_inconclusive_when_tool_fails`
- `test_policy.TestVerdictAndReview.test_not_confirmed_when_checks_ran_without_findings`

事实文件：[unit-tests.json](../../evidence/20261021-course-pack/unit-tests.json)。执行状态按逐测试记录读取，未运行、失败和跳过不能计作通过。

### REQ-09 的 Sensor

执行入口：`V-AUDIT`、`V-CHAIN`。实现位置：[trina/audit.py](../../trina/audit.py)。

- `test_audit.TestAuditChain.test_chain_valid_after_writes`
- `test_audit.TestAuditChain.test_tamper_detected`
- `test_audit.TestAuditChain.test_deleted_record_detected`
- `test_audit.TestAuditChain.test_resume_continues_chain`

事实文件：[unit-tests.json](../../evidence/20261021-course-pack/unit-tests.json)。执行状态按逐测试记录读取，未运行、失败和跳过不能计作通过。

### REQ-10 的 Sensor

执行入口：`V-FULL`。实现位置：[trina/session.py](../../trina/session.py)、[trina/interactive.py](../../trina/interactive.py)。

- `test_interactive.TestApprovalModes.test_timeout_is_fail_closed`
- `test_interactive.TestAgentHumanHooks.test_human_rejection_skips_action`
- `test_interactive.TestAgentHumanHooks.test_steering_queue_reaches_agent`
- `test_interactive.TestAgentHumanHooks.test_stop_event_halts_loop`
- `test_interactive.TestConsole.test_revoke_and_grant`

事实文件：[unit-tests.json](../../evidence/20261021-course-pack/unit-tests.json)。执行状态按逐测试记录读取，未运行、失败和跳过不能计作通过。

### REQ-11 的 Sensor

执行入口：`V-POLICY`、`V-FULL`。实现位置：[trina/policy.py](../../trina/policy.py)、[trina/brain/failure.py](../../trina/brain/failure.py)。

- `test_policy.TestController.test_budget_stops_checks`
- `test_policy.TestController.test_no_new_evidence_triggers_stop`
- `test_decision_evidence.TestEvidenceRun.test_repeated_facts_trigger_stop_contract`
- `test_collaboration.TestCollaboration.test_chat_failure_feedback_is_sent_back_to_model`

事实文件：[unit-tests.json](../../evidence/20261021-course-pack/unit-tests.json)。执行状态按逐测试记录读取，未运行、失败和跳过不能计作通过。

### REQ-12 的 Sensor

执行入口：`V-FULL`。实现位置：[trina/state.py](../../trina/state.py)、[trina/brain/agent.py](../../trina/brain/agent.py)、[trina/board.py](../../trina/board.py)。

- `test_collaboration.TestCollaboration.test_atomic_save_keeps_previous_snapshot_when_replace_fails`
- `test_insight.TestStateRoundTrip.test_to_dict_from_dict_round_trip`
- `test_insight.TestStateRoundTrip.test_from_dict_rejects_error_payload`

事实文件：[unit-tests.json](../../evidence/20261021-course-pack/unit-tests.json)。执行状态按逐测试记录读取，未运行、失败和跳过不能计作通过。

### REQ-13 的 Sensor

执行入口：`V-FULL`。实现位置：[trina/brain/scheduler.py](../../trina/brain/scheduler.py)、[trina/brain/agent.py](../../trina/brain/agent.py)。

- `test_collaboration.TestCollaboration.test_scheduler_consumes_live_controller_coverage`
- `test_collaboration.TestCollaboration.test_same_tool_with_wrong_test_id_is_not_compliance`
- `test_insight.TestInsightSwitches.test_scheduler_off_leaves_no_directive`

事实文件：[unit-tests.json](../../evidence/20261021-course-pack/unit-tests.json)。执行状态按逐测试记录读取，未运行、失败和跳过不能计作通过。

### REQ-14 的 Sensor

执行入口：`V-FULL`。实现位置：[trina/collaboration.py](../../trina/collaboration.py)、[trina/tools/specialists.py](../../trina/tools/specialists.py)。

- `test_collaboration.TestCollaboration.test_advice_never_executes_or_mutates_facts`
- `test_collaboration.TestCollaboration.test_recursion_unregistered_and_cross_role_tools_are_rejected`
- `test_collaboration.TestCollaboration.test_forged_flags_status_and_findings_are_removed`
- `test_collaboration.TestCollaboration.test_invalid_response_consumes_budget`
- `test_collaboration.TestCollaboration.test_parent_must_issue_separate_action_and_record_replays_child`

事实文件：[unit-tests.json](../../evidence/20261021-course-pack/unit-tests.json)。执行状态按逐测试记录读取，未运行、失败和跳过不能计作通过。

### REQ-15 的 Sensor

执行入口：`V-AUDIT`、`V-FULL`。实现位置：[trina/audit.py](../../trina/audit.py)、[trina/state.py](../../trina/state.py)、[trina/tools/specialists.py](../../trina/tools/specialists.py)、[trina/report/markdown.py](../../trina/report/markdown.py)。

- `test_audit.TestAuditChain.test_secrets_redacted`
- `test_eval_report_lab.TestReport.test_credentials_redacted_in_output`
- `test_collaboration.TestCollaboration.test_snapshot_is_bounded_and_credential_password_is_absent`

事实文件：[unit-tests.json](../../evidence/20261021-course-pack/unit-tests.json)。执行状态按逐测试记录读取，未运行、失败和跳过不能计作通过。

### REQ-16 的 Sensor

执行入口：`V-FULL`。实现位置：[trina/report/builder.py](../../trina/report/builder.py)、[trina/report/html.py](../../trina/report/html.py)、[trina/review.py](../../trina/review.py)。

- `test_eval_report_lab.TestReport.test_sections_and_self_contained_html`
- `test_eval_report_lab.TestReport.test_save_writes_requested_formats`
- `test_eval_report_lab.TestReport.test_empty_state_still_renders`
- `test_policy.TestVerdictAndReview.test_review_flags_claim_without_evidence`

事实文件：[unit-tests.json](../../evidence/20261021-course-pack/unit-tests.json)。执行状态按逐测试记录读取，未运行、失败和跳过不能计作通过。

### REQ-17 的 Sensor

执行入口：`V-FULL`。实现位置：[trina/eval/metrics.py](../../trina/eval/metrics.py)、[trina/eval/harness.py](../../trina/eval/harness.py)。

- `test_eval_report_lab.TestMetricsMath.test_fp_and_fn_are_counted`
- `test_eval_report_lab.TestMetricsMath.test_out_of_scope_not_counted_as_fp`
- `test_eval_report_lab.TestMetricsMath.test_scenario_without_ground_truth_has_no_false_positives`
- `test_eval_report_lab.TestMetricsMath.test_alias_normalization_matches_across_spellings`

事实文件：[unit-tests.json](../../evidence/20261021-course-pack/unit-tests.json)。执行状态按逐测试记录读取，未运行、失败和跳过不能计作通过。

### REQ-18 的 Sensor

执行入口：`V-E2E`、`V-FULL`。实现位置：[scripts/verify_decision_evidence.py](../../scripts/verify_decision_evidence.py)、[trina/lab/http_target.py](../../trina/lab/http_target.py)。

- `test_eval_report_lab.TestLabFixtures.test_autostart_and_stop`
- `test_eval_report_lab.TestLabFixtures.test_existing_fixture_is_not_touched`

事实文件：[summary.json](../../evidence/20261021-course-pack/round-1/summary.json)、[summary.json](../../evidence/20261021-course-pack/round-2/summary.json)。此处以两轮真实请求结果为主，lab 单测仅作为夹具生命周期补充。

### REQ-19 的 Sensor

执行入口：`V-DOC`、`V-DIFF`。实现位置：[scripts/verify_course_pack.py](../../scripts/verify_course_pack.py)、[docs/course/traceability.json](../../docs/course/traceability.json)。

此要求包含文档机械检查及人工审查，没有伪造对应产品单元测试。

事实文件：[doc-check.json](../../evidence/20261021-course-pack/doc-check.json)、[structure-negative-check.json](../../evidence/20261021-course-pack/structure-negative-check.json)。结构阴性实验在隔离副本破坏文件或测试引用，确认检查返回非零。

### REQ-20 的 Sensor

执行入口：`V-DOC`。实现位置：[docs/course/continuity.md](../../docs/course/continuity.md)、[docs/course/open-questions.md](../../docs/course/open-questions.md)。

此要求包含文档机械检查及人工审查，没有伪造对应产品单元测试。

事实文件：[doc-check.json](../../evidence/20261021-course-pack/doc-check.json)、[structure-negative-check.json](../../evidence/20261021-course-pack/structure-negative-check.json)。静态字段和关系可以机械检查，MR-01 异机复现及 MR-02 教师接受仍待真实人员完成。

## 3. 旧 A1–A8 与当前规格

| 旧分类 | 当前 REQ | 解释 |
|---|---|---|
| A1 目标边界 | REQ-02、REQ-05、REQ-06 | 包含预检、动作参数和实际跳转目的地 |
| A2 注册裁决 | REQ-03、REQ-04 | 区分 strict 强制约束与 guided 提示，不声称完整参数绑定 |
| A3 预算停止 | REQ-11、REQ-13 | 停止合同与方向调度分开，覆盖计数限制保持可见 |
| A4 结论分类 | REQ-07、REQ-08 | 事实、目标成功与 verdict 各有判据 |
| A5 证据复现 | REQ-07、REQ-16、REQ-18 | 原始证据、只读检查与固定决策重放不等于通用独立复现器 |
| A6 审计 | REQ-09、REQ-15 | 内部哈希链与已定义脱敏位置 |
| A7 人工介入 | REQ-03、REQ-10、REQ-14 | 撤权、审批和子角色执行边界 |
| A8 交接恢复 | REQ-12、REQ-19、REQ-20 | 快照读取、资料入口和人工交接，不冒称自动续跑 |

## 4. 状态与人工判定

[验证事实](../../evidence/20261021-course-pack/README.md) 保存原始输出与环境，机器摘要只是索引，不能覆盖原件。REQ-01 至 REQ-18 具有自动化或实际运行路径，已知缺口仍按 OQ 保留；REQ-19 由机械引用及阴性实验检查；REQ-20 包含静态和人工两个部分。

公开回归不能证明教师隐藏测评、独立环境复现或真实模型收益。课程最终状态为待人工审查，不预填“所有需求已通过”。人工至少抽查 REQ-03、REQ-07 和 REQ-15 或同等跨层要求，核对规格、具体 Then、实际测试断言及证据边界，记录于 [审查记录](../../review/20261021-course-pack-review.md)。
