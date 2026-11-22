# 实际裁片发货台账与召回交接 v1（loom-dispatch）

loom-trace 的 `historical.allowed` 只回答“在发货申请当时的知识快照下，这块裁片是否具备
放行条件”，它**不记录仓库是否真的发过货**。本模块补上仓库侧的真实事实台账：

* 同一块成布面积只能被实际交付一次，发货占用是矩形事实；召回、质量恢复、通知确认都
  **不释放**已发面积，也不提供删除发货记录或复用其面积的操作；
* 每次成功发货把当次 trace 决定永久冻结为原始凭据（`admitted_decision`），后续导入的新
  证据只能改写 `current_decision`，并在 `allowed` 发生 true↔false 翻转时产生召回/恢复
  事件，绝不回头改写旧凭据；
* 通知只供下游读取与确认，不连接、不发送到任何外部系统。

本模块完整复用 `loom-trace` 的字段验证、版本规则、跨时刻争用检查、压缩几何、材料归属、
容量与证书判定，**不复制另一套浮线、取整、版本选取或放行判断**。仅依赖 Python 标准库，
不使用网络、后台服务或真实织机，支持 macOS/Linux 多进程并发与进程中断恢复。

## Python 与 CLI 接口

* `loom_draft.dispatch.apply(store_path, command)` -> 普通字典（一份回执）。
* `loom_draft.dispatch.read_store(store_path)` -> 普通字典（完整台账视图）。
* `loom-dispatch --store DIR --source COMMAND.json`：向标准输出写一个完整 JSON 回执。
* `loom-dispatch --store DIR --view`：向标准输出写完整台账视图。

`--source` 与 `--view` 互斥；没有 `--output` 参数，台账也不是一次性报告文件，而是
`store_path` 目录内被独占管理的持久状态。命令文件沿用 8MB 严格读取上限、拒绝重复 JSON
键与非有限常量。业务拒绝仍退出 0；输入错误、`request_id` 冲突、存储/I/O 错误退出 2 并给
简明错误，不把 traceback 当业务报告。

## 台账目录与原子提交

`store_path` 是此台账独占的本地目录，内部由实现管理：

* `ledger.json`：唯一可变台账文件，包含当前 source、全部发货、事件、事件序号、请求日志；
* `.lock`：`fcntl.flock` 进程互斥锁（读用共享锁、写用排他锁），进程退出后由内核释放，
  无需人工清理；
* `.commit-*.tmp`：提交临时文件，写入并 `fsync` 后用 `os.replace` 原子替换 `ledger.json`，
  再 `fsync` 目录；异常残留的临时文件在下次打写锁时清理。

数据导入与复核结果、转换事件、永久回执作为**一次提交**一起生效；批量发货的所有占用与凭据、
批量确认与回执同样一起生效。进程在持久化中途或“提交完成但回执未返回”时被强制终止，下次
打开只能看到完整提交前或完整提交后的状态；已提交未返回的请求靠请求日志原样重放，未提交的
请求可正常执行一次。外部手工破坏有效台账文件会明确报错（`StoreError`），不会悄悄重建空账
并释放已发面积。

## source 契约

source 是完整 loom-trace 输入对象，字段仍为 `batch`、`lots`、`allocations`、
`certificates`、`shipments`、`as_of`，并有两条额外约束：

1. `shipments` 必须为空数组（真实发货只由本台账的 `dispatch` 操作产生）；
2. 所有 allocation/certificate 版本的 `known_at` 都不得大于 `as_of`（台账不持有未来版本）。

其余验证与 loom-trace 完全一致，且**对所有版本**完整验证。允许合法的来源缺口与不合格证据。
初始化后 `batch` 与 `lots` 固定，后续 source 的这两项必须与初始化值 JSON 结构相等：对象键
顺序忽略，**数组顺序保留**。不能通过重排生产、换批次容量或改纱线身份让已发货物变成另一卷布。

### 知识积累（ingest）

导入是知识积累，不是替换：

* 新 `as_of` 必须**严格大于**当前 `as_of`；
* 既有 allocations/certificates 中的每条 `(id,revision)` 都必须原样保留（结构相等），
  数组本身可重排，不能删除或改写旧版本；
* 新增版本仍满足连续 `revision`（1..N 无缺号、`known_at` 随修订严格递增、rev1 不能为
  null）等原规则，且其 `known_at` 必须大于旧 `as_of`、不大于新 `as_of`；
* 新增证据可以涉及早先生产位置；撤回通过新增 `payload=null` 版本表达。

初始化为 `generation=1`，每次成功导入恰好加 1；发货与确认通知不改变 generation。

## command 严格对象

command 是四种严格对象之一。未知字段、缺字段、重复 JSON 键、非有限数、布尔充整数一律作为
输入错误拒绝（抛 `ValueError` / CLI 退出 2）。`request_id` 与 `cut_id` 沿用
`[A-Za-z0-9_-]{1,40}` 标识规则；`request_id` 在整份台账中跨操作唯一。

### init —— `{op:"init", request_id, source}`

在尚未初始化的目录创建台账。对已有台账再发 init 是业务拒绝 `already_initialized`。

### ingest —— `{op:"ingest", request_id, expected_generation, source}`

`expected_generation` 为正整数，必须等于当前 generation，否则业务拒绝 `stale_generation`。
通过来源积累契约后更新知识，并复核全部真实已发裁片。

### dispatch —— `{op:"dispatch", request_id, expected_generation, cuts}`

* `cuts` 为 1..16 项，每项恰含 `cut_id`、`end_start`、`end_stop`、`pick_start`、
  `pick_stop`；坐标是 trace 的真实经端/纬半开矩形，必须在本卷实际成布内且面积为正；
* `cut_id` 在本次内互异，且不能与任何已成功发出的 `cut_id` 相同；
* 台账累计至多 128 块成功发货裁片；
* 每个提议裁片都以**当前 source 的 `as_of`** 作为 `requested_at`，用当前 source 的完整
  归账和证据得出与 trace 一致的决定；
* 全部允许，且与本次其他裁片、所有历史成功发货裁片都无面积重叠时，整批办理实际发货；否则
  整批业务拒绝、不占用任何新裁片。边界相接合法。

业务检查严格按以下**优先级**给出 code（前面类别成立时不用后面类别替代）：

1. `stale_generation`（`expected_generation` 不匹配，优先于一切依赖当前台账的检查）；
2. `cut_limit`（累计上限，按新增块数计算）；
3. `cut_id_used`（`cut_id` 已被历史成功发货占用）；
4. `cloth_overlap`（本次内或与历史裁片矩形面积冲突）；
5. `trace_blocked`（当前 trace 决定不允许）。

一次拒绝中，同类别的所有相关 `cut_id` 或既有冲突 `cut_id` 按 ASCII 升序列出，不只取输入
第一项。本次 cuts 的重复 `cut_id`、非法坐标、非法 source/版本结构及来源积累契约违反属于
**输入错误**，不是业务拒绝。已有台账上的错误输入不会损坏或改写业务状态，也不占用
`request_id`。

### ack —— `{op:"ack", request_id, event_ids}`

`event_ids` 为 1..128 个互异正整数。只有全部事件存在才成功；只要有不存在的编号，整批业务
拒绝 `unknown_event`，不做部分确认。已确认事件再次确认成功且不产生新事件。确认只记录下游
已收取通知，不表示实物已退回、质量已恢复或旧发货作废。

## 发货凭据与现在结论

台账区分“本次发货时的依据”与“现在的结论”。成功发货为每块裁片永久保存：坐标、所属
`request_id`、`admitted_generation`、`admitted_at`，以及 `admitted_decision`——它恰为
当次 trace 对该裁片的 **current** 决定，含 `knowledge_at`、`allowed`、`lots`、`missing`、
`problems`，材料与版本引用保持原语义。此凭据永久不变。

另有 `current_decision`，在初始化后的当前 source 上按同一矩形复核；它随每次成功导入更新，
包括 `allowed` 未变化但材料归属、问题或版本引用发生变化的情况。`current.allowed=false` 的
实际已发裁片记为 `state="recall"`，true 记为 `"normal"`。不能仅凭 trace 的
`historical.allowed` 自动增加真实发货记录；尚未发货的合法但不允许裁片只出现在拒绝回执中。

## 召回 / 恢复事件

每次成功 ingest，对每块已发裁片比较导入前后的 `current_decision.allowed`：

* true→false：新建 `kind="recall"` 事件；
* false→true：新建 `kind="clear"` 事件；
* 其他变化（含 allowed 不变但归属/引用/问题变化）：不新增事件。

事件含 `event_id`、`kind`、`cut_id`、`generation`、`knowledge_at`、`decision`、
`acknowledged`；`decision` 冻结为触发该转换时的新决定。事件按台账提交顺序使用从 1 开始的
连续整数，同一导入内按 `cut_id` ASCII 顺序编号；拒绝、重试、ack 均不消耗编号。召回后恢复
产生 clear，但旧 recall 事件仍保留，尚未确认时仍待交接；再次失效须产生新 recall，不能复用
已确认的旧事件。`acknowledged` 初始 false，确认后 true，其余事件内容不变。

## 请求身份与重放

`request_id` 是请求身份，不是“重新运行”的提示。

* 对同一 `request_id` 与同一 command 的重试，原样返回首次保存的整个 JSON 回执，不重新
  计算、不改 generation、不改成当前质量结论，也不额外占用布料、增加通知或确认其他事件；
* 请求相等按 JSON 结构比较：对象键顺序忽略、数组顺序保留；不增加 replay 字段；
* 相同 `request_id` 携带不同 command 抛 `ValueError`，不能覆盖旧凭据；
* 纯格式检查之后，重放命中优先于当前代次、当前质量、已有 cut_id 与来源时间检查。因此先前
  成功发货即使现在召回，旧请求仍取回成功回执；先前 `trace_blocked` 的请求即使后来恢复也仍
  取回旧拒绝，重新办理必须使用新 `request_id`；
* 输入错误不占用 `request_id`，修正后可复用原 ID。

## 回执

所有格式合法并得出业务结论的变更请求（包括业务拒绝）都保存永久回执。回执恰含
`schema_version=1`、`request_id`、`op`、`status`、`generation`、`result`。`status` 为
`accepted` 或 `rejected`；`generation` 为该请求第一次得出结论时的知识代次。

* 成功 init/ingest 的 `result` 含 `as_of`、`transition_event_ids`（init 为空数组）；
* 成功 dispatch 的 `result` 含按 `cut_id` 升序的 `deliveries`，每项为保存的原始发货凭据；
* 成功 ack 的 `result` 含按编号升序的 `acknowledged_event_ids`；
* 拒绝的 `result` 至少含 `code`；dispatch 拒绝还含 `rejected_cut_ids`、
  `conflicting_cut_ids`（均为排序去重数组，无既有冲突时后者为空）；`trace_blocked` 另外
  给出按 `cut_id` 排列的 `decisions`，每项含 `cut_id`、`decision`，列出本次**全部**提议
  裁片的决定，让调用方看到所有不允许原因，而不是把少数合格项先发走；
* `cut_limit`、`stale_generation`、`already_initialized` 影响本次全体提议裁片；
* ack 拒绝另给 `unknown_event_ids`。

## 视图（read_store / --view）

视图不创建不存在的台账，也不修改回执、通知或质量结果。`schema_version=1`，至少含
`generation`、`as_of`、`production`、`deliveries`、`events`、`pending_event_ids`、
`summary`：

* `production` 沿用 trace 的 `plan`、`segments`、`material_requirements`；
* `deliveries` 按 `cut_id` 排列，每项含永久发货凭据的全部字段，外加 `current_decision`、
  `state`；
* `events` 按编号列出全部事件；`pending_event_ids` 正好是尚未确认的事件，不因质量恢复自动
  消失；
* `summary` 含 `delivered_cuts`、`normal`、`recall`、`events`、`pending_events`，计数与
  视图一致；
* 视图不重复塞入全部历史回执。Python 与 CLI 给出的回执/视图一致。

## 资源与处理范围

继续支持原有最大 `repeat_x`/`repeat_y` 与压缩计划：导入和发货不展开成布交织网格、不逐点
登记占用。占用是已发矩形事实；质量、材料、版本引用来自实际计划与 trace 语义。覆盖至少
128 次成功来源导入、128 块发货裁片、1024 条变更请求及相应事件记录；不人为设置更小的持久
台账总字节上限，也不新增时延 SLA。
