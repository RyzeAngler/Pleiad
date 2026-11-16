# Pleiad 持续协作模式交付说明

初始交付日期为 2026-11-12，源码基线为 `2ef982d05b85167cad70758c8064ab03458a94f5`。持续协作能力于 2026-11-16 随 Pleiad **0.3.0** 发布到正式 PyPI，版本与最终验证记录见 [0.3.0 发布说明](pleiad-0.3.0-release.md)。下文第 7 节保留初始源码交付时的验证记录。Canvas 和 Weave 产品业务尚待接入新模式。

## 1. 交付范围

新增 `CollaborationGroup`，与 `SubagentGroup`、Graph、Handoff 并列。它不继承、转发或适配旧子智能体组。成员身份和 Session 跨工作轮次保留；每个新 Work 调用一次现有 Runner；活跃 followup 在结束门仍开放时追加到原 Work/Run。模型、工具、图片解析和执行循环继续由 Runner 负责。

已提供：动态创建、消息、活跃追加、空闲新轮、精确中断、只读 wait-any、结果读取、成员关闭、作用域关闭、父子暂停恢复、共享预算、内存/SQLite 存储、重启核对、原 Run 对账、公开进度投影。宿主继续拥有产品 Execution、授权、模型客户端及部署独占权。

## 2. 精确导入与绑定

```python
from pleiad.budgets import BudgetLedger
from pleiad.orchestration.collaboration.group import CollaborationGroup
from pleiad.orchestration.collaboration.endpoint import MemberEndpoint
from pleiad.orchestration.collaboration.binding import CollaborationHost, WorkAdmission, WorkBinding
from pleiad.orchestration.collaboration.contracts import (
    CloseRequest, CollaborationLimits, CommandReceipt, EventCursor, EventPage,
    InterruptRequest, MessageRequest, MessageStatus, SpawnRequest,
    WorkPermit, WorkProposal, WorkRejection,
)
from pleiad.orchestration.collaboration.backends.in_memory import InMemoryCollaborationStore
from pleiad.orchestration.collaboration.backends.sqlite import SqliteCollaborationStore
from pleiad.orchestration.collaboration.tools import collaboration_tools
```

所有 `__init__.py` 为空，没有聚合导出或兼容别名。`MemberRecord`、`WorkRecord` 定义在 `state.py`；`CollaborationEvent` 定义在 `contracts.py`。

```python
group = CollaborationGroup(
    scope_id,
    root_session=root_session,
    sessions=session_store,
    store=collaboration_store,
    host=CollaborationHost(admit_work, rebind_work),
    budgets=shared_budget_ledger,
    limits=CollaborationLimits(max_active_runs=4, max_queued_work=32),
    close_on_exit=False,  # 服务生命周期退出时保留成员，供下一部署独占恢复
)
async with group:
    root_endpoint = group.root
    # 将 collaboration_tools(root_endpoint) 登记到根 Runner 的 ToolRegistry，
    # 并把需要的 ToolRef 放进根 Agent。实际 Runner 自动打开/关闭该成员的 ToolScope。
```

子成员必须至少绑定一个来自其 endpoint 的协作工具；否则准入明确失败。工具允许集合由宿主选择，模型不能选择可信 actor、Session、Runner、grants 或客户端。所有组合模式必须传入**同一个 BudgetLedger 实例**。

`close_on_exit` 默认仍为 `True`，适用于明确结束整个协作作用域的调用。常驻产品应显式传入 `False`，并由同一个服务任务进入和退出 `async with group`：退出会停止新命令和调度、取消并结算所有拥有的子 Work、退出 TaskGroup，但保留 scope 的 open 状态及成员各自原有状态。已关闭成员不会被重新打开，未知结果不会退款或重发。宿主必须先结束自己拥有的根 Run；组不关闭借用的根 Session、Runner 或客户端。

保留模式退出后，旧实例不能接受新的根 Run 或工作；重新使用仍需宿主排除旧部署、读取持久 `owner_epoch`、调用 `recover(expected_epoch=...)` 再进入上下文。新一轮使用新根 Run 的真实权限和预算，对原 member 的 followup 创建新 Work/Run，沿用原 Session。显式 `group.close()` 始终永久关闭 scope 和全部成员，即使 `close_on_exit=False` 也不会改变此语义。服务停止不是 checkpoint 暂停：需要保留在途原 Run 的宿主应按第 4 节先处理父子暂停/恢复。

宿主需要提供两个异步回调：

| 回调 | 返回值与责任 |
| --- | --- |
| `admit_work(proposal: WorkProposal, endpoint: MemberEndpoint)` | 返回 `WorkAdmission(permit, binding)` 或 `WorkRejection`。按 SDK 预先持久化的 `proposal.work_id` 幂等登记子 Execution/授权；此时不能发起模型或收费操作 |
| `rebind_work(proposal: WorkProposal, permit: WorkPermit, endpoint: MemberEndpoint)` | 返回当前 `WorkBinding` 或拒绝。在排队启动和恢复原 checkpoint 前重新检查权限、配置、Resolver；保持 binding_key 和实际 Agent/工具/上下文指纹 |

`WorkBinding` 是 frozen/slotted dataclass，持有 Agent、Runner、binding_key、context 和 handoffs。`WorkPermit` 是持久化 Pydantic 模型，保存 authorization_ref、binding_key、RunLimits。SDK 将请求限制与实际 host/Agent/父 Run 策略相交，并固定期限和预留额度；permit 中不保存客户端或凭据。

回调在作用域准入临界区内执行，必须及时返回，不得回入该 group。该版本采用单拥有者、串行事实提交；不承诺慢远程授权期间的低延迟控制，也未进行吞吐或尾延迟压测。需要重型外部检查时，宿主应事先完成检查，让回调执行当前授权核验和幂等登记。

## 3. 公共操作契约

`authority` 是实际 Runner 传给工具的 `ToolContext`，或者宿主从原 Run/checkpoint 构造的当前可信授权；不得从模型参数读取或伪造父预算。

| 方法 | 输入与返回 | 语义 |
| --- | --- | --- |
| `endpoint.spawn(request, authority)` | `SpawnRequest → CommandReceipt` | 新 member/Session/Work；profile_id 由宿主准入；受理不等于运行完成 |
| `endpoint.followup(request, authority)` | `MessageRequest → CommandReceipt` | 开放的活跃轮返回 `active_run`；其他允许状态返回 `new_work`；paused 明确拒绝 |
| `endpoint.send_message(request, authority)` | `MessageRequest → CommandReceipt` | 不启动空闲成员，不占新工作预算；普通消息不强制延续最终模型步骤 |
| `endpoint.interrupt(request, authority)` | `InterruptRequest → CommandReceipt` | 同时匹配 member/work/run；等待原句柄结算，保留成员与历史 |
| `endpoint.wait(member_ids, after=cursor, timeout_seconds=30, limit=100)` | `EventPage` | 非空严格后代集合、wait-any、独立游标；timeout 0 可只读查询；不消费消息或重记费用 |
| `endpoint.list_members()` | `tuple[MemberRecord, ...]` | 当前 scope 的成员身份、父子关系、Session、开放状态 |
| `endpoint.status(member_id)` | `tuple[WorkRecord, ...]` | 仅授权后代；包含历史 Work。模型工具只投影身份、阶段、原因和 resolution |
| `endpoint.message_status(message_id)` | `MessageStatus` | 分开返回 received/delivered/consumed/retained/revoked；实际消费以 Session 的 step_id 为准 |
| `endpoint.read_result(work_id)` | `RunReceipt \| None` | 原持久 RunReceipt；尚未产生 Run 或收据时为 None，应结合 status 的 phase/reason 判断 |
| `endpoint.close_member(request, authority)` | `CloseRequest → CommandReceipt` | 关闭整个后代子树、结算所有工作、保留可查询历史 |
| `endpoint.resume_work(work_id, authority, decisions=())` | `None` | 宿主专用，恢复原 Run；当前父 Run 暂停时保存决定，父 Runner 恢复账本并打开作用域后才调度 |
| `group.recover(expected_epoch=...)` | `None` | 在进入 async context 前调用；宿主须先确保旧部署已停止；不重放未知模型/工具请求 |
| `group.reconcile_work(work_id, runner=..., request_id=..., expected_epoch=..., expected_revision=..., evidence=())` | `None` | 宿主提供原 Run 的版本和可信证据，调用 Runner.reconcile；不新开 Run、不自动退款 |
| `group.close()` | `None` | 停止全部新准入，取消并等待所拥有工作；不关闭借用的根 Session、Runner 或共享客户端 |

`SpawnRequest` 字段为 request_id、profile_id、title、content；`MessageRequest` 为 request_id、member_id、content、可选 expected_run_id。content 使用现有 `InputEnvelope`，可携带文本、ResultReference 或文本/图片块。SDK 为消息分配稳定输入身份并覆盖其 source/kind，保留真实内容；不用模型提供的 source 充当调用权限。

`InterruptRequest` 必须含 request_id、member_id、expected_work_id、expected_run_id。`CloseRequest` 为 request_id、member_id。`CommandReceipt` 包含 request_id、accepted、route、member_id，以及可选 work_id/run_id/message_id/code。route 只可能为 new_work、active_run、message、interrupt、close、rejected。相同 scope/actor/request_id 和载荷返回**完全相同**的原回执；不添加会改变结果的 duplicate 字段。不同载荷抛出 `CollaborationConflict`。

普通准入拒绝通过 accepted=false/code 返回，如 queue_full、member_limit、depth_limit、budget_exhausted、run_paused、run_mismatch、reconciliation_required。无效权限或身份冲突使用 BusinessError 子类 `CollaborationConflict`；存储及所有权损坏使用 `CollaborationStorageError`。模型工具不把拒绝描述成任务成功。

模型工具名：`spawn_agent`、`send_message`、`followup_task`、`interrupt_agent`、`close_agent`、`list_agents`、`agent_status`、`read_agent_result`、`wait_agents`。恢复和部署接管是宿主操作，不暴露给模型。

## 4. 顺序、预算与所有权

- 一个成员最多一个 dispatching/running/paused/stopping writer；原 writer 释放后才启动下一项。Work.phase 与 RunReceipt.state、member.state、消息消费相互独立。
- 活跃 followup 保留原 owner、预算、期限和 grants；没有新的产品 Execution 或预算预留。路由在受理时固定，结束后重试也不会变成新 Work。
- 结束边界使用通用 `ToolScope.before_finish`。协作模式只因显式 followup 延续子 Run；没有该钩子的旧运行模式保持原行为。根 Run 的普通 live 用户输入继续使用既有语义。
- Session 新增 `enqueue(..., run_id=..., invocation_id=...)` 定向元数据。目标 Run 和实际消费 Run 分开，只有 SessionWriter 在安全步骤边界完成消费。已接受但未消费的旧轮追加在取消后不会被下一轮领取；未来 invocation 的输入也不会被当前 live Run 误领。
- 根 Run 正常结束或取消会停止、结算它拥有的工作，成员和 Session 保留。若要保留等待批准的子 Run，宿主必须暂停父 Run；准备子恢复决定后再调用父 `Runner.resume`。新工作由新的根 Run 取得新的权限和期限。
- 新 Work 立即独占预留，包括排队任务。暂停保留预留和成员 writer，但释放执行槽位。明确未开始的工作按零用量结算；未知结果按原预留保守扣留，不自动退款。模型 token 预算不代表图片收费账单。
- 非根成员创建新工作必须立即有可用槽位，不能排在等待自己的祖先后面；控制与阻塞等待限于严格后代，消息可在 scope 内投递。root 不占子执行槽位。
- 成员及消息容量、队列、深度和页大小可通过 CollaborationLimits 配置；max_members 是 scope 生命周期内的成员总数上限。已投递但尚未消费的消息也计入收件箱上限。
- 模型供应商失败、图片授权拒绝会形成对应成员的失败收据，不取消无关兄弟。状态、存储或拥有权故障仍向上抛出，触发组级清理。

## 5. 存储、事件和恢复

使用独立 `collaboration.sqlite3`；不要传入现有 Session 数据库。SQLite 后端与内存后端使用同一份验证后的 scope journal，物理表为 `collaboration_journals(scope_id, revision, epoch, payload)`，原子保存成员、命令、Work、消息/outbox、完整预算镜像、事件和投影游标。选择快照日志与现有 SessionStore 一致，避免为同一单拥有者事务维护十套实体写入；高规模拆表和索引不在本次交付内。

每次写入检查 revision 和 owner_epoch；独占恢复通过 expected_epoch 接管。epoch 只围住本地提交，不会自动终止旧进程或远端动作。SQLite 工作在 `asyncio.to_thread` 中执行；取消等待者不会丢失已经提交的事务、本地版本更新或已启动 Runner 的句柄。

事件有稳定 event_id、scope_id、sequence、timestamp、kind、member_id，及可选 work_id/run_id/message_id/source_sequence/step_id/actor_id。游标是 `EventCursor(scope_id, sequence)`。事件包括 work.accepted/running/paused/terminal/unknown、member.created/closed、message.accepted/delivered/consumed、work.interrupt_requested，以及 `run.step.*`、`run.tool.*`、`run.message.completed` 等投影。事件返回元数据，不复制私有推理或整个会话；结果另查原收据。Run 进度可从原 Session 事件补投影，消费事实以原 InputRecord 为准。

恢复按后代优先核对原 invocation：

1. 原收据已提交：恢复结果、原 charge_id、结果消息和未投影进度；跨库投递重试复用同一 input_id。
2. 原 checkpoint 合法：保留原 Run 和预留，等待宿主明确决定；恢复仍需 rebind_work 当前授权。
3. 准备/排队且证实没有 invocation：结算为未开始；尚无回执的准备命令固化为 recovery_interrupted，重试不会再调用宿主准入。
4. 其他在途状态：标记 resolution=unknown，保守扣留，拒绝该成员的新工作，等待 reconcile_work。不存在“恢复失败便再调用一次模型”的路径。

同一账本混合 Graph、旧子组及新组时，镜像包含全部池和预留；新组只结算属于自己的工作。不同的活跃账本不能被恢复镜像覆盖。根/其他组合模式的原 checkpoint 必须由各自拥有者恢复；不宣称一个 scope journal 能独立恢复整个产品部署。

当前实现使用进程内通知，持久事实负责重放。它没有分布式租约、自动接管、跨进程长轮询、存储配额、自动历史裁剪或新的监控导出器；观测继续使用现有 Runner Trace 和新增事件。列表/历史采用完整 journal 读取，性能与保留策略需由实际部署压测后确定。

## 6. 可运行示例和本地接入

完整离线示例：[examples/collaboration.py](../packages/pleiad/examples/collaboration.py)。它使用真实 Agent/Runner/Session/ToolRuntime，仅模型传输为确定脚本；根模型直接调用 spawn_agent，后续示例工具在真实 ToolContext 下展示消息、followup、wait、interrupt、close。无需 API key。

```bash
uv run --directory packages/pleiad --frozen python examples/collaboration.py
```

预期根结果 succeeded，三个 Work 均 terminal、拥有三个不同 Run，子成员始终使用同一个 Session。该示例也在 wheel 与 sdist 的干净安装环境中执行。

Canvas 可在独立 Python 3.12 环境安装本地源码：

```bash
uv venv --python 3.12 /tmp/pleiad-canvas-collaboration
uv pip install --python /tmp/pleiad-canvas-collaboration/bin/python \
  -e /Users/w/Documents/Projects/g-agent/packages/pleiad
```

核心机制不需要新依赖或 extras；调用实际提供商时另外安装所需 provider extra，例如 openai/volcengine。不要用消费者原 Git 固定依赖覆盖本地验证安装；绝对路径不进入发行依赖。联调必须同时记录实际 module `__file__`、distribution 版本、SDK HEAD/dirty 状态、Canvas 代码版本。

Canvas/Weave 接入方需补齐：按 work_id 幂等创建 child Execution 和 parent_execution_id；绑定 profile allowlist、当前 grants 和 Session-aware ImageResolver；消费带游标事件并投影产品状态；持有作用域 async context；处理父 Run 暂停、恢复及关闭；持有部署独占权后才执行恢复。执行身份、消息顺序、ModelStep、Graph 和 Trace 仍是独立事实。

## 7. 验证证据

以下命令从仓库根目录执行，环境为 macOS arm64 / Python 3.12，均已成功退出。默认测试未访问真实模型供应商。

| 检查 | 实际命令 | 结果 |
| --- | --- | --- |
| SDK 格式 | `uv run --directory packages/pleiad --frozen ruff format --check .` | 260 files already formatted |
| SDK lint | `uv run --directory packages/pleiad --frozen ruff check .` | All checks passed |
| SDK 严格类型 | `uv run --directory packages/pleiad --frozen mypy` | 263 source files，无错误 |
| 示例类型 | `uv run --directory packages/pleiad --frozen mypy examples/collaboration.py` | 1 source file，无错误 |
| 协作与 Session 专项 | `uv run --directory packages/pleiad --frozen pytest -q tests/sessions/test_store.py tests/orchestration/collaboration/test_collaboration.py` | 103 passed，5.67s |
| SDK 确定性回归 | `uv run --directory packages/pleiad --frozen pytest -q -m 'not oci and not provider_e2e'` | **993 passed, 13 deselected, 1 warning**，21.71s |
| Weave 格式 | `uv run --directory apps/weave --frozen ruff format --check .` | 83 files already formatted |
| Weave lint | `uv run --directory apps/weave --frozen ruff check .` | All checks passed |
| Weave 严格类型 | `uv run --directory apps/weave --frozen mypy` | 85 source files，无错误 |
| Weave 确定性回归 | `uv run --directory apps/weave --frozen pytest -q -m 'not packaging and not oci and not provider_e2e'` | **277 passed, 3 deselected**，87.00s |
| 包安装与资源 | `uv run --directory apps/weave --frozen pytest -q -m packaging` | **2 passed, 278 deselected**，19.13s；wheel/sdist 干净安装后执行离线协作示例 |
| 导入边界 | `uv run --project apps/weave --frozen lint-imports --config .importlinter` | **9 kept, 0 broken**；247 files、1026 dependencies |
| 完整离线示例 | `uv run --directory packages/pleiad --frozen python examples/collaboration.py` | Root: succeeded；3 个 terminal Work、3 个不同 Run |
| 差异完整性 | `git diff --check` | 通过 |

SDK 唯一 warning 来自已安装 DashScope 包的 Assistants API 弃用提示。13 项 SDK deselected 是按标记排除的真实供应商/OCI 检查；Weave 与 packaging 的 deselected 是上述各自命令的选集边界，不能算作通过。

文档使用本地 Mermaid/Playwright 渲染及校验脚本 `/tmp/pleiad-collaboration-architecture/render.mjs`、`verify.mjs`，由 `/Users/w/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node` 执行。结果：16/16 图源一致，26 个 Markdown 本地链接、71 个 HTML 本地链接通过；1440px 桌面无横向溢出、坏锚点或页面异常，820px 平板无页面溢出。已检查阅读版截图。可保留的验证产物见 [document-validation.json](assets/agent-collaboration/document-validation.json) 和 [离线阅读版](assets/agent-collaboration/architecture.html)。这些是文档验证，不是产品 UI 验收。

实际本地安装身份核验命令：

```bash
uv run --directory packages/pleiad --frozen python -c 'from importlib.metadata import version; import pleiad.orchestration.collaboration.group as module; print(module.__file__); print(version("pleiad"))'
```

返回模块路径 `/Users/w/Documents/Projects/g-agent/packages/pleiad/build/__editable__.pleiad-0.2.0-py3-none-any/pleiad/orchestration/collaboration/group.py`，distribution 版本 `0.2.0`。这证明使用本地 editable 源码，不能据版本号推断发布包已包含新能力。SDK HEAD 为本文开头记录的基线，worktree dirty；Canvas 尚未接入，未记录 Canvas 验证版本。

| 需求 | 精确测试证据（SDK `tests/orchestration/collaboration/test_collaboration.py`） |
| --- | --- |
| 模型动态创建、独立并行成员 | `test_model_can_dynamically_create_two_members_through_bound_tools`；`test_same_profile_members_run_in_parallel_with_independent_sessions`（内存/SQLite） |
| 活跃追加与空闲新轮 | `test_active_followup_keeps_original_run_and_does_not_reserve_again`；`test_idle_followup_reuses_session_and_starts_a_new_invocation` |
| 消息、结束边界与幂等 | `test_message_does_not_reopen_final_model_step_or_wake_idle_member`；`test_conflicting_retry_does_not_create_a_second_member` |
| 精确中断、旧输入不重放 | `test_interrupt_requires_exact_run_and_settles_before_followup`；`test_cancelled_active_followup_is_never_replayed_into_the_next_run` |
| 竞争与取消受理窗口 | `test_two_followups_after_the_final_gate_get_distinct_serial_invocations`；`test_cancelling_spawn_acknowledgement_does_not_lose_the_started_child` |
| 只读 wait、关闭后查询 | `test_wait_cursors_are_readonly_and_results_remain_available_after_close` |
| 关闭幂等和生命周期封闭 | `test_closing_a_closed_member_with_a_new_request_does_not_reopen_its_lifecycle`；`test_closed_scope_rejects_a_new_runner_lifecycle` |
| 故障、容量、树与预算 | `test_provider_failure_does_not_cancel_an_unrelated_sibling`；`test_queue_is_bounded_and_closed_members_never_start_queued_work`；`test_nested_spawn_rejects_capacity_instead_of_queuing_behind_its_ancestor`；`test_member_and_depth_limits_reject_new_identities_before_host_admission`；`test_reserved_budget_prevents_a_second_child_from_starting` |
| 持久化、对账、预算镜像 | `test_recovery_preserves_unknown_work_without_resubmitting_the_model`；`test_recovered_scope_fences_old_writers_and_returns_original_command_receipts`；`test_shared_ledger_recovery_preserves_other_modes_reservations_and_unknown_charges` |
| 跨库提交窗口 | `test_recovery_recovers_committed_receipt_and_parent_result_after_projection_was_lost`；`test_recovery_never_reexecutes_a_prepared_command_whose_host_acknowledgement_was_lost` |
| 暂停与原 Run 恢复 | `test_paused_member_rejects_followup_and_resumes_the_original_checkpoint`；`test_paused_root_restores_its_ledger_before_dispatching_original_child_resumption` |
| 多步工具、图片与撤权 | `test_multimodal_child_uses_real_resolver_on_each_tool_step`；`test_followup_reauthorizes_image_history_before_another_provider_request`；`test_host_revocation_before_dispatch_stops_work_without_a_model_charge` |
| 消息背压、作用域、资源清理 | `test_inbox_limit_counts_delivered_but_unconsumed_messages`；`test_foreign_members_and_ancestor_waits_are_rejected`；`test_scope_close_cancels_all_owned_handles_and_preserves_borrowed_runners` |

底层 Session 的新契约另由 `tests/sessions/test_store.py` 中 `test_live_run_does_not_claim_another_invocations_reserved_input`、`test_targeted_input_acceptance_does_not_invalidate_the_active_writer` 验证，两个存储后端均覆盖。

持久输入丢失原 Run 的损坏路径由 `test_sqlite_reports_corruption_when_a_reserved_input_loses_its_owning_run` 验证，确保报告明确的 `SessionDataCorruptedError`。

发布边界：未做真实供应商、OCI 或 Canvas 前后端联调；未改消费者发行依赖、未发布新版本、未提交或推送 Git。离线/本地传输验证不能替代真实看图、生图或远端工具执行验收。

## 8. Canvas 接入补充：正常部署关停（2026-11-13）

新增 `close_on_exit=False`，解决常规服务退出误用永久 `close()` 导致重启后无法继续原成员的问题。现有 `SubagentGroup` 和默认永久关闭模式保持原行为。只修改协作组、端点关停准入、对应确定性测试及本文/架构说明，未发布、提交或推送。

新增行为证据：

| 契约 | 测试 |
| --- | --- |
| 正常退出取消结算 Work，重启后同 member/Session 接续新 Work/Run；内存和 SQLite 均覆盖 | `test_deployment_exit_preserves_member_session_for_recovered_followup` |
| 关停期间重复取消仍等待模型流清理，保留成员 | `test_cancelled_deployment_owner_settles_children_without_closing_members` |
| 未知调用保持阻塞，不重放、不退回已扣留额度 | `test_deployment_exit_keeps_unknown_work_blocked_without_replaying_or_refunding` |
| 显式 close 在保留模式下仍永久关闭，recover 不重新开放 | `test_explicit_close_still_terminates_a_preserved_deployment_scope` |

本次 SDK 验证：协作专项 **38 passed**；完整确定性测试 `uv run --directory packages/pleiad --frozen pytest -q -m 'not oci and not provider_e2e'` 为 **998 passed, 13 deselected, 1 warning**（22.82s）。Ruff lint 全通过，format check 为 **260 files already formatted**；严格 mypy 为 **263 source files** 无错误；`git diff --check` 通过。warning 仍为已安装 DashScope Assistants API 弃用提示。本补充未重新执行 Weave、packaging、真实供应商或 OCI 检查；第 7 节记录的是此前交付批次的证据，不等同于本补充再次执行。

同批 Canvas 联调还发现并修复真实父 Run 取消链路：父 Runner 已处理取消时直接关闭协作后代，子 Run 的已提交取消回执会被 `RunHandle.result()` 误判成调用者新的取消，截断父收据提交。`MemberEndpoint.close_run` 现在通过 `complete_owned` 在独立清理任务中结算后代，保留新的外层取消传播，未修改 `RunHandle` 的取消语义。新增 `test_cancelling_real_parent_run_commits_parent_and_child_receipts`，使用真实父/子 Runner，内存与 SQLite 各一例；仅在测试进程中移除清理隔离后，两例均重现原 CancelledError，说明断言能捕获此次回归。Canvas 的 `tests/test_agent_tasks.py::test_parent_cancellation_settles_all_real_children` 也已直接执行通过，未修改 Canvas 代码。

取消链路修复后的最终复跑：协作专项 **40 passed**（6.27s）；完整 SDK 确定性回归 **1000 passed, 13 deselected, 1 warning**（23.54s）；Ruff format/lint、严格 mypy 与 `git diff --check` 全部通过。此结果替代本节此前 38/998 的中间检查计数。


## 9. Canvas 本地消费者接入（2026-11-13）

`/Users/w/Documents/Projects/pleiad-canvas` 已通过 uv strict editable 安装本 SDK，并替换新任务的旧 SubagentGroup 路径。项目 scope 使用 `close_on_exit=False`；工作注册、公开状态、活跃追加、精确停止、消费状态、按原请求重试及重启后同成员/Session 继续已接入。旧临时任务仅保留历史读取。Canvas 尚未开放暂停检查点/批准恢复的用户流程，Weave 未在本轮接入。

Canvas 自身验证：后端 90 项、前端 56 项，Ruff/format、Pyright、前端 lint/build 通过；Chrome 实际通过追加 → 消费、停止兄弟任务、刷新恢复以及 320/390px 布局检查。模型传输为受控响应，未执行付费供应商联调。详细身份、安装与行为证据位于 Canvas `docs/pleiad-canvas/07-local-sdk-collaboration-20261113.md` 与 `design-qa.md`。

这只是本地消费者接入结果，未提交或发布任何分发版本。
