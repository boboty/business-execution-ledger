# First-stage System of Record Declaration

**Status:** EFFECTIVE  
**Effective date:** 2026-09-06  
**Release:** `v0.3.0 — First-stage System of Record`  
**Frozen cutover code SHA:** `c2ad184115a8a6db37b7f7927a7c89d16303733d`

## Declaration

经第一阶段业务事实核验、独立 Cutover Baseline 对账、全新 PostgreSQL 候选库完整切换重放及 Real First-stage Cutover Gate 验收，现正式确认：

> **自本声明生效之日起，BEL（Business Execution Ledger）正式作为第一阶段合同执行事实与确定性业务状态的 System of Record。**

原合同业务台账 Excel 自此不再承担权威业务记录职责，仅作为历史资料、源数据导入/回填材料、导出载体或下游 Data Product 使用。出现差异时，不以旧 Excel 作为 Golden Truth；应回到原始 Evidence、已确认业务事实、冻结业务规则以及 BEL 当前权威状态进行处理。

## First-stage authoritative scope

本次 SoR 声明覆盖第一阶段已经完成切换验收的合同执行业务事实与确定性状态，包括：

- `Contract` 与 `ContractItem` 当前权威事实；
- 采购侧发票与合同的已确认对应关系；
- 对公 OUT Payment 与合同的已确认对应关系；
- 经独立来源支持的 `SalesContract` scope 与 `ProcurementSalesLink`；
- Cutover Fact Pack 允许进入 BEL 的已确认历史/人工事实；
- 基于上述权威事实确定性计算或投影的第一阶段 Workbench 与 Data Products。

采购匹配继续遵守已经冻结并验收的确定性顺序：明确/权威指定优先，其次唯一确定匹配，再次是真实时间顺序；只有在所有合法排列对 V1 权威业务状态完全等价时，才允许使用 equivalent-permutation canonicalization。真正影响业务含义且无法确定的关系继续进入人工确认，而不是由系统猜测。

## What this declaration does not mean

本声明不意味着所有运营任务必须清零，也不把运营中的补充、异常或待确认事项伪装成已完成状态。`TaskException`、需要人工确认的 `MatchCase`、Period Close blocker 以及其他合法的 unresolved work 仍按各自生命周期继续处理。

本声明也不扩大 BEL 的业务边界：

- Agent / LLM 仍不是 System of Record；
- Prompt 不承载权威业务规则或权威业务状态；
- 销售侧自动匹配仍未因本次切换而开放；
- 下游财务、税务、ERP 语义不会进入 Canonical Business Model；
- `Accrual` 等规则输出不会通过 Cutover Fact Pack 被伪装成历史输入事实。

## Cutover evidence chain

本次声明基于已经完成并通过的独立验收链：

1. 从空库创建全新 PostgreSQL candidate，schema 验证为 current migration head；
2. 使用批准的 source Evidence、private backfill plan、payment scope decisions 与 Cutover Fact Pack 完成 canonical cutover preparation；
3. Cutover Fact Pack 以同一 whole-pack identity / EvidenceDocument 分阶段应用，先导入 pre-match facts，再执行采购匹配，最后在 confirmed contract-level edge 就绪后导入 `InvoiceItemAllocation`；
4. 采购 Invoice / Payment matching 通过，真实采购 HCR 为零，equivalent canonical cohorts 按冻结规则稳定重放；
5. 独立 Cutover Baseline 仅从原始来源与冻结语义重建，不从 candidate 反推 expected；
6. canonical reconciliation PASS，`unresolved_count = 0`，无 `MISSING_ACTUAL`、`MISSING_BASELINE_ENTRY` 或 `VALUE_MISMATCH`；
7. Contract Business Ledger、Contract360、Period Close、Invoice Preparation、Exception & Task Center 及相应 Data Products 均通过最终重放验证；
8. Real First-stage Cutover Gate PASS，且 Gate read-only / privacy boundary 验证通过；
9. private source、baseline、reports 与业务值均未进入公共仓库。

Gate 始终只是技术验收的 judge，而不是切换开关。真正使 BEL 成为 System of Record 的，是本文件记录的业务负责人明确接受与声明。

## Release checkpoint

`v0.3.0` 标记第一阶段 SoR 里程碑，其 tag 指向完成最终 cutover 验收的冻结实现：

`c2ad184115a8a6db37b7f7927a7c89d16303733d`

后续代码演进不得改变这一历史事实；如未来业务范围扩大，应通过新的显式阶段、规则冻结、验收与迁移机制推进，而不是回写或重解释本次声明。

## Next stage

SoR 生效后，下一阶段优先建设 **minimal Application Tool Contract**：让 Agent 通过稳定的 Application boundary 读取和操作 BEL，而不是让 Agent 直接接触数据库或成为业务规则的承载者。

Business Cockpit 仍是按实际业务使用需要安排的可选投影，不是 Agent 接入的前置条件。
