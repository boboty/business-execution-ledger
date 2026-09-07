# 项目再评估与下一阶段

本次判断基于当前仓库代码、冻结规则、Application Tool Contract 及其独立复核。
本文不包含私有验收值或私有业务数据。

## BEL 今天是什么

BEL 已完成第一阶段切换，并已成为第一阶段合同执行事实与确定性业务状态的
System of Record。当前 Core 已形成：

- Evidence / Fact / Decision 分层与追溯；
- 采购与销售业务域分离；
- 合同、商品、发票、付款、出运及显式 Allocation / Relationship；
- 确定性采购匹配与人工确认边界；
- Period Close、Invoice Preparation、Exception & Task 等共享业务状态投影；
- PostgreSQL 运行时、迁移纪律、Cutover / Reconciliation / Data Product；
- 最小 Application Tool Contract v1。

BEL 不是 Agent Runtime，也不是普通“合同/发票/付款表单管理系统”。它的核心
价值是：以治理后的事实和关系为基础，通过确定性规则重建可追溯的业务执行
状态，并把不确定性显式暴露给人或上层操作者。

## 保留不变的核心边界

- `Evidence ≠ Fact ≠ Decision`；状态不能退化为人工维护的任意 `status` 字段。
- 事实修订使用 correction / supersession 语义，不把历史事实静默覆盖掉。
- 采购与销售模型保持结构性分离；多对多关系和 Allocation 必须显式表达。
- Business Core 不包含财务、税务、ERP 消费方词汇。
- Prompt 不是业务规则；权威业务状态由确定性代码计算。
- Operator / Agent 只能通过 Application / Tool Contract 操作 BEL，不能直接接触数据库。
- 不以“所有任务清零”代替业务事实正确，也不把不同类型的 blocker、advisory、Task
  强行合并成一个通用工作流对象。

## Tool Contract 独立复核后的关键判断

最小 Tool Contract 已经证明采购发票工作发现、Fact/Evidence/Allocation 检查、
确定性 matching 和再次读取权威状态这条纵向链路可以通过受限 JSON capability
表达；但它还没有证明一个带模型不确定性、重试和中断的 Runtime 可以安全运行。

独立复核暴露出三类下一阶段必须先解决的问题：

1. **单次响应一致性。** 当前 `invoice_work` 在 PostgreSQL READ COMMITTED 下由多次
   查询拼出一个结果；并发提交可能让同一响应同时包含新旧状态。该问题应在
   Application 读取边界解决，而不是要求上层模型自行修复。
2. **逻辑边界不等于执行边界。** `ToolContract` 是良好的逻辑 capability 边界，
   但同进程对象引用、数据库凭据、shell/CLI 等旁路仍可能绕开它。第一 Runtime 前
   需要可信 Host 和实际受限的执行/权限边界。
3. **完成语义必须保守。** batch matching 的授权范围、`OUTCOME_UNKNOWN` 后的当前
   状态对账、`UNMATCHED` 与 `human_work=[]` 的含义都不能被 Runtime 简化成“任务已完成”。
   必须冻结 bounded retry、stop 和 residual-work handoff 语义。

因此，下一步不是直接接 Pi，也不是继续增加新的业务 Tool。

## 当前选择的主要阶段：Executable Delegation Boundary

当前主线为：

```text
Application Tool Contract v1
        ↓
修复 single-response consistent view
        ↓
Trusted Host / private JSON transport
        ↓
model-free independent client
        ↓
authorization / retry / stop / residual-work semantics
        ↓
Gate
        ↓
Restricted Operator / Agent Runtime
```

这一阶段只证明“BEL 能否被安全委托操作”，不建设通用 Agent 平台。

### Gate 至少要证明

- 一个 operator-facing read response 内部有一致的观察视图；
- Runtime/client 无数据库凭据、无 Core 对象引用、无通用写旁路；
- WRITE capability 只能由可信 Host 授权；
- `OUTCOME_UNKNOWN` 后按当前权威状态对账，不做盲目重试；
- `UNMATCHED`、`HUMAN_CONFIRMATION_REQUIRED`、没有 MatchCase、没有 `human_work`
  保持各自真实语义；
- 模型或客户端不能通过 Prompt/脚本重新实现 matching 规则；
- 无对话历史也可以从 BEL 当前状态恢复并继续；
- 固定工作链若脚本已经足够，则 Agent 必须证明额外的复核/交接价值，否则不扩大
  Runtime 投资。

## Operator / Runtime 选择

Pi 仍然是第一 restricted operator runtime 的候选，但它不再是 Architecture Law。
PydanticAI、OpenAI Agents SDK、语言选择、进程数量、transport 和 Runtime 顺序都属于
可逆的实现决策。

当前不冻结一个抽象 `AgentRuntime Interface`。先让 Tool Contract + Host 边界成为
稳定 SPI；等第二个真实 consumer/runtime 出现后，再从两个实现中提炼真正共同的
Runtime abstraction。

## Intelligent Intake：明确延后，且位于 Core 之外

当前不在 BEL Core 内建设 Semantic Understanding / Semantic Normalization Layer。

这一周 BEL Core 的快速收敛本来就建立在一个正确前提上：最初 Excel / PDF 等原始
材料先由 Codex 辅助理解、归一化和人工复核关键歧义，再进入现有 BEL import / backfill
路径。对于当前本地、低频使用，这个流程已经足够，不需要提前产品化。

未来只有在新来源持续接入、重复归一化成本明显、多人使用或自动化 Intake 成为真实
需求时，才单独建设 `bel-intake`：

```text
Raw Excel / PDF / Documents
        ↓
bel-intake
parse / interpret / normalize / propose
        ↓
BEL Intake Contract
        ↓
bel-core
Evidence / Fact / Rule / Decision / Task
        ↓
Tool Contract
        ↓
Operator / Pi / other Runtime
```

`bel-intake` 可以拥有 OCR、Parser、LLM Provider、Prompt、Evaluation 和归一化逻辑，
但不拥有权威业务状态。真正需要长期稳定的是 **BEL Intake Contract**，而不是某个
模型调用接口。

当前因此维持：

```text
新 Excel / PDF
→ Codex-assisted preprocessing
→ 人工检查关键歧义
→ 可审阅的结构化中间结果
→ BEL existing import / backfill
```

这个过程先作为开发/运营流程存在，不进入近期产品 Roadmap。

## 后续顺序

1. 修复 Tool Contract operator-facing read 的 mixed-view consistency。
2. 完成 Executable Delegation Boundary 与 model-free independent client Gate。
3. 接入第一 restricted Operator / Runtime，并验证真实 Agent value。
4. 继续按业务价值扩展 BEL 的 Business State / Tool capability，而不是为了 Agent 增加能力。
5. 第二个真实 Runtime/consumer 出现后验证 substitutability。
6. MCP、外部生态和 downstream adapters 按真实接入需求推进。
7. `bel-intake` 在重复 Intake 需求真实出现后再产品化。

长期责任边界保持一句话：

> **Intake interprets. Core decides. Operator acts.**
