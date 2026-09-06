# Application Tool Contract v1

本阶段建立可独立评审的最小边界，不改变 v0.3.0 SoR 宣告和冻结业务规则。

## 选择与范围

`Operator → bel.tools.ToolContract → ApplicationPort → ToolOperations
→ existing Application services → Domain / persistence`。

`ToolContract` 只处理 JSON 值、输入校验、能力授权和响应封装；
`ToolOperations` 是现有 Application 的小型组合入口，负责会话生命周期和显式投影。
采购匹配仍唯一委托给 `match_invoices`，人工待办仍来自 Exception & Task Center。
没有 Agent Runtime、MCP、HTTP 服务、第二套规则或 schema 变更。

第一条纵向流程是：发现采购发票及已有匹配状态 → 查询事实/Evidence →
运行既有确定性采购发票匹配 → 查询分配 Decision 和人工待办。
它完成允许自动执行的业务部分；歧义终点是持久化 MatchCase 和既有人工作台，
不会把“调用成功”当作“所有业务工作已解决”。

## 接入与信任边界

可信 BEL 宿主使用 `bel.infrastructure.tool_host.create_tool_contract(database_url,
allow_matching=False)` 组装入口，仅把 `catalog()` 和 `call(request)` 暴露给操作端。
生产工厂严格要求 `postgresql+psycopg://`，检查 Alembic head，不创建或修补 schema，
不读取 .env。测试可直接注入 ApplicationPort 或 SQLite 测试会话。

可信宿主的最小调用示例（`database_url` 由宿主配置，不能来自 Agent 参数）：

```python
from bel.infrastructure.tool_host import create_tool_contract

tools = create_tool_contract(database_url)  # 默认只读
response = tools.call({
    "version": "1", "request_id": "inspect-work",
    "capability": "procurement.invoice_work", "arguments": {},
})
```

`allow_matching=True` 是宿主对**所有当前采购发票的匹配批次**的明确授权，
不是逐笔人工确认。它不来自请求、提示词或模型输出。默认只读。
同进程 Python 对象不是恶意代码沙箱；未来远程宿主必须持有数据库凭据、
认证调用方并控制授权，只传输 JSON，不能把 Session/Engine 或后端对象交给 Agent。
当前未开放网络端点，不声称具备多租户、远程认证或任意插件隔离能力。
运行结果可能含业务数据，宿主不能把内容写入仓库或公共日志。

## 请求与响应

请求必须且只能有四个键：

```json
{"version":"1","request_id":"example-call","capability":"procurement.invoice_work","arguments":{}}
```

`version` 必须为字符串 `1`；`request_id` 是 1–100 字符的相关性标记，
不是幂等键或 Evidence。`arguments` 必须为对象；未知字段一律拒绝，不做类型猜测。
响应固定为 `version, request_id, status, data` 四个键。
非法 request_id 回传 null；非 OK 的 data 为 null，不回传内部异常内容。
消费者必须按字段名读取，不依赖对象键顺序。破坏性字段/语义变化必须升级版本；
同版本可增加响应字段，消费者应忽略不认识的响应字段。

| capability | arguments | OK data |
| --- | --- | --- |
| `procurement.invoice_work` | `{}` | `invoices[]`, `human_work[]` |
| `procurement.inspect_invoice` | `{"invoice_id":"UUID"}` | `invoice_id, direction, seller, gross_amount, currency, evidence, allocations[]` |
| `procurement.match_invoices` | `{}` | `auto_confirmed, human_confirmation_required, unmatched, capacity_exceeded, already_matched_skipped, out_of_scope, subject_ids[]` |

`catalog()` 返回版本、能力名称、参数形状、READ/WRITE 副作用；写能力额外声明
当前 enabled、全量采购发票 scope 和 RECONCILE_CURRENT_STATE 重试语义。

* `invoices[]`: `invoice_id, match_case_id, match_status`。
  后两项可为 null，仅表示没有已记录的 MatchCase，**不等于**符合匹配范围、
  待匹配、无风险或规则判定的 UNMATCHED。销售和 UNKNOWN 方向不进入此投影。
* `human_work[]`: `source_type, source_id, code, status, invoice_id,
  resolution_route, scopes[]`；每个 scope 有 `scope_type, scope_id`。
  保留现有 Center 的来源身份、全部候选 scope 和人工路径，只筛选关联采购发票的工作。
  此能力不是全局异常中心，不覆盖未映射发票的任务或月结 blocker。
* `evidence`: `fragment_id, document_id`，只提供追溯引用，不暴露文件路径或原始文档内容。
* `allocations[]`: `allocation_id, contract_id, match_case_id, gross_amount,
  match_method, confirmation_type`，只投影已持久化采购分配，绝不合成候选分配。
* UUID 为字符串；金额为十进制字符串；未知 currency/seller 为 null；计数为整数；
  列表为空时返回 `[]`。匹配/人工状态沿用 Application 的权威代码。

| status | 含义与后续动作 |
| --- | --- |
| `OK` | 调用成功；业务不确定性仍须读取 data/持久化工作状态 |
| `INVALID_REQUEST` | 修正版本、类型、字段或 UUID 后重试；无业务调用 |
| `UNKNOWN_CAPABILITY` | 不支持的能力；无业务调用 |
| `FORBIDDEN` | 宿主未启用写能力；无业务调用 |
| `NOT_FOUND` | 发票不存在或不属于采购方向 |
| `INTERNAL_ERROR` | 读取失败，不泄漏内部诊断 |
| `OUTCOME_UNKNOWN` | 写调用未能交付结果；可能已提交，先读取状态再决定重试 |

HUMAN_CONFIRMATION_REQUIRED 是持久化业务状态，不是传输错误。
已有规则产生 UNMATCHED 时原样保留，不增加冻结规则之外的任务生产者。
本版没有 Agent 提案入口、人工确认入口或通用 RESOLVE；因此也不伪造 PROPOSED 状态。
未来提案需要独立的持久化和人工授权设计。

## 重试、事务与读取

已有匹配服务取得共享 PostgreSQL advisory lock 后检查 MatchCase，再在同一事务内
写入 MatchCase、候选、分配和事件并提交。每次调用使用新会话，异常退出关闭会话并
回滚尚未提交的修改；工具层不叠加第二个事务，不在提交后额外读取以拼装成功响应。

同一已处理 subject 不被重分配；相同状态上重试不新增匹配/分配/事件。
但新到发票会参与下一次批次，返回计数也可能变化。这里没有请求日志、精确响应重放或
exactly-once 承诺。若要求固定输入集的审批和重放，应先扩展 Application 命令，
不能在工具 handler 里自行筛选匹配算法输入。

读取是当前状态投影，不是冻结快照；多次查询之间可能发生并发变化。
本版列表全量返回，无截断和分页；大数据量优化延后，不默默漏掉工作。

## 验证与后续

`tests/integration/test_tool_contract.py` 用合成事实验证 JSON 往返的完整操作流程、
Evidence/Decision 追溯、人工歧义保留、销售隔离、未知范围、只读、权限和重试。
`tests/postgres/test_tool_contract_postgres.py` 在迁移创建的隔离 PostgreSQL 上重复流程，
测试并发重复调用和中途写失败回滚。测试遵守显式 disposable 数据库契约。
`tests/unit/test_tool_architecture.py` 限制 tools 的依赖、禁止 Core 导入运行时 SDK，
并为 `bel.agent / agent_runtime / runtimes` 的未来适配器目录设置存储/Core 导入护栏。
这是静态 import 护栏，不是动态导入或任意新目录的安全沙箱。

下一步只做独立架构/契约评审，确认批次授权和重试语义，再决定首个 Runtime 接入。
