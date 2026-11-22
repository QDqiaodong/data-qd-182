# 织机纱线批次追溯与历史放行复核 v1（loom-trace）

使用者为质检员。织造已经结束，本接口回答三件事：

1. 每块裁片实际用了哪些原料批次，按真实生产串归账；
2. 发货申请当时（`requested_at`）系统能够知道的放行结论；
3. 以当前知识时刻 `as_of` 复核，哪些已放行裁片需要召回。

本接口只增加追溯语义，**不改变** `loom-batch` 的选单目标、可行性与材料契约。批次计划由现有 `run_plan` 规则唯一确定（价值最大、总用纱最小、身份键最小），追溯坐标全部绑定该真实计划压缩段，包括接缝。本任务不新增任何选单优化目标。

时间是一等约束：`known_at` 是系统获知证据的时刻，不是织机生产时刻；晚到的新版本可以描述早先生产位置，但绝不泄漏到历史决定。

## 输入

严格 JSON 对象，顶层字段恰为 `batch`、`lots`、`allocations`、`certificates`、`shipments`、`as_of`。所有数组均可乱序；不得以数组顺序代替版本、位置或平局规则。所有整数拒绝布尔值；未知字段、无效引用、非法版本、越界位置、冲突均为输入错误。

### batch

现有合法批次输入（校验同 `docs/contract-batch.md`）。其选定计划共 P 纬、E = `len(common.warp) × common.repeat_x` 根实际经端，基础穿综长度 M = `len(common.warp)`。横向经端 x（0 起）的基础位置为 `x % M`，横向重复编号为 `x // M`。P、E 为零的空计划合法，此时 `allocations`、`certificates`、`shipments` 必须为空，未用 `lots` 仍合法。

### lots

0..256 个对象，每项恰含 `id`、`yarn`、`capacity_mm`：

* `id`：匹配 `[A-Za-z0-9_-]{1,40}`，数组内唯一；
* `yarn`：沿用 v1 纱线标签规则；
* `capacity_mm`：0..10^12 的整数。容量是同一批次经、纬两方向共用的毫米池。

同一 `yarn` 可有多个批次，同一 `lot` 可以同时用于经纱和纬纱。`batch.stock` 仍约束原生产计划，不得用 `lots` 重排计划或替换库存语义；`lots` 只是对已生产用料的批次归账。

### allocations / certificates 的版本规则

两个数组各 0..1024 项版本记录。每项公共字段恰为 `id`、`revision`、`known_at`、`payload`：

* `id`：匹配上述标识规则，在各自数组命名空间内识别一条逻辑记录；
* `revision`：1..1024 的整数；
* `known_at`：0..10^12 的整数；
* `payload`：`null` 表示该版本撤回整条记录；非 null 表示完整替换（不是字段补丁）。允许撤回后恢复，但版本 1 不能为 `null`。

同 `id` 的 `revision` 必须从 1 连续无缺号、无重复，且 `known_at` 随 `revision` 严格递增。**所有版本**（包括 `known_at > as_of` 的未来版本）都要完整验证，不能只验证最终存活版本。

对任意知识时刻 T，一条逻辑记录只取 `known_at <= T` 中 `revision` 最大的一版；若没有，或该版为 `null`，则该记录在 T 不存在。

### allocations 的非 null payload

两种之一，且同 `id` 的非 null 版本必须保持 `orientation` 相同（允许订正位置与 `lot_id`）：

* `warp`：恰含 `orientation="warp"`、`base_end`、`repeat_start`、`repeat_stop`、`pick_start`、`pick_stop`、`lot_id`。`base_end` 为 0..M-1，`0 <= repeat_start < repeat_stop <= repeat_x`，`0 <= pick_start < pick_stop <= P`。它为该基础经端在所列横向重复与纬区间内的纱段归账；`lot_id` 必须存在且 `lot.yarn` 必须等于该基础经端的 yarn。
* `weft`：恰含 `orientation="weft"`、`pick_start`、`pick_stop`、`lot_id`，`0 <= pick_start < pick_stop <= P`。它只为区间内**实际纬纱**等于 `lot.yarn` 的那些纬归账，不占用其他 yarn 的纬；区间内至少须有一条匹配纬。实际纬纱来自已选相位、周期重复与接缝，不能按工单输入第一纬推测。

同一知识时刻，多个有效 allocation 不得争用同一真实纱段（即使 `lot_id` 相同也不允许）：经纱要求基础位置相同、横向重复区间相交、纬区间相交；纬纱要求 yarn 相同且区间交集中实际存在该 yarn 的纬。不同 yarn 的纬记录数值区间重叠不冲突。必须检查从 0 到所有 `known_at` 之间每个实际可见版本组合，包括截止时刻以后的组合；同一 `known_at` 的所有记录版本同时生效。两个事实只在旧版/新版各自出现而从未同时有效，不算争用。出现争用是非法输入。

覆盖不完整合法，报告未知来源并阻止涉及裁片放行；缺失纱段不擅自分给其他 lot。

### certificates 的非 null payload

恰含 `lot_id`、`pick_start`、`pick_stop`、`verdict`：`lot_id` 必须存在，`0 <= pick_start < pick_stop <= P`，`verdict` 为 `"pass"` 或 `"fail"`。证书表示该批次用于这些生产纬位置的原料质量，不同证书是独立证据，不是同批次最后一份覆盖全部旧证书。

在 T，对某 lot 的某使用位置：任意有效 `fail` 覆盖即不合格；无 `fail` 且至少一份有效 `pass` 覆盖才合格；两者皆无为证据不足。证书只覆盖该裁片未使用的纬，不能影响它；订正或撤回只能替换同 `id` 的旧证书，不能删除其他 `id` 的 `fail`。允许对当前没有归账使用的批次开证。

### shipments

0..128 个对象，每项恰含 `id`、`requested_at`、`end_start`、`end_stop`、`pick_start`、`pick_stop`：

* `id` 唯一；
* `requested_at` 为 0..`as_of` 的整数；
* `0 <= end_start < end_stop <= E`，`0 <= pick_start < pick_stop <= P`。

每项是一块矩形裁片，端点均为半开区间、使用整经端/整纬边界；裁片可跨周期、工单、接缝或横向重复边界。两块裁片不得在成布上有面积重叠；共享边界合法。

### as_of

0..10^12 的整数知识时刻，即复核所依据的最新知识。

## 精确材料

记 s=`pick_spacing_mm`，d=`warp_spacing_mm`，k=1000+`takeup_permille`，w=`warp_waste_mm`，F(t)=ceil(t·s·k/1000)，H(x)=ceil(x·d·k/1000)，F(0)=H(0)=0。全部用整数精确计算，不对裁片或归账记录重新向上取整。

* 某根经端在纬区间 `[a,b)` 的成布纱长为 `F(b)-F(a)`；其开轴损耗 w 只归给覆盖该根经端第 0 纬的 allocation。整批 lot 用量计损耗；裁片原料贡献只计成布纱长，不把开轴损耗算成所含原料。
* 每条纬在经端区间 `[u,v)` 内的纱长为 `H(v)-H(u)`；该条整纬用量 `H(E)` 由该纬的 lot 承担，横向切片不重复取整，也不按裁片自身宽度重算纬长。
* 在知识时刻 T，lot 的 `attributed_mm` 为该时刻所有有效 allocation 在整批生产计划中归到它名下的用量（含经轴损耗、未申请发货的布及接缝，经纬合并）。大于 `capacity_mm` 即 `overdrawn`。
* 完整覆盖时所有 lot 的 `attributed_mm` 合计等于原批次 `material_requirements` 总量；有缺口时 `whole_unattributed_mm` 使“已归账 + 未归账 = 原计划总用量”成立，并含尚未归账的开轴损耗。

## 每块裁片的两次独立决定

对每块裁片分别以 `requested_at`（`historical`）与 `as_of`（`current`）两个知识快照独立决定，互不覆盖；补录只改变 `current`，不改写 `historical`。把 `as_of` 缩小到旧时刻并相应去掉超时申请时，过去已存在申请的 `historical` 不变。

`allowed=true` 需同时满足：整块裁片的经纬来源均完整；所有涉及 lot 均未 `overdrawn`；每个实际使用 lot 的每个相关生产纬位置均有足够合格证据。来源缺口、超容量、`fail`、证据不足可以同时存在，不得只报告第一项；已知且可判断的用料不能因其他位置未知而免审。容量看整批归账，不是只看该裁片纱量。

处置：

* `historical allowed=true` 视为当时已放行；`current=false` 时 `disposition="recall"`；
* 两次均 true 为 `"released"`；
* 历史 false、当前 true 为 `"ready"`（现在可重新申请，不伪称过去已发货）；
* 两次均 false 为 `"hold"`。

## 输出

`schema_version=1`，CLI 与 `loom_draft.trace.run_trace(data)` 返回结构一致；后者返回普通字典，非法输入抛 `ValueError`。

* `production`：原 batch 的 `plan`、`segments`、`material_requirements`，保持原结构，与真实所选生产串一致。
* `lot_balances`：在 `as_of` 按 `lot_id` 升序列出每个声明批次的 `lot_id`、`yarn`、`capacity_mm`、`warp_mm`、`weft_mm`、`attributed_mm`、`remaining_mm`、`overdrawn`；`remaining_mm` 可为负，未用批次也列出。
* `whole_unattributed_mm`：全批次未归账总长（含尚未归账的开轴损耗）。
* `shipments`：按 `id` 升序，每项含 `id`、`disposition`、`historical`、`current`。每个决定含：
  * `knowledge_at`、`allowed`、`lots`、`missing`、`problems`；
  * `lots` 按 `lot_id` 升序，只列实际与裁片相交的批次，每项含 `lot_id`、`yarn`、`warp_mm`、`weft_mm`、`total_mm`、`whole_attributed_mm`、`overdrawn`、`allocation_versions`、`certificate_versions`。版本引用统一为 `{id,revision}`，按 `(id,revision)` 升序去重；`allocation_versions` 只列为本裁片提供了真实纱段的有效版本，`certificate_versions` 只列覆盖该 lot 在本裁片实际使用纬位置的有效证书（pass/fail 均列），未来版、被替换版、撤回版和无关范围不列。
  * `missing` 含 `warp_points`、`weft_picks`、`material_mm`、`first`。`warp_points` 是缺少来源的 `(实际经端,实际纬)` 位置数量；`weft_picks` 是裁片内缺少来源的实际纬条数；`material_mm` 是裁片内未知来源纱长（不含开轴损耗）；`first` 无缺口为 `null`，否则为 `{orientation,end_index,pick_index}`，先 warp 后 weft、各方向先 `pick_index` 后 `end_index` 取最早，未知纬的 `end_index` 取裁片 `end_start`。
  * `problems` 为按 `(kind,lot_id)` 升序去重的对象数组，`kind` 只用 `source_gap`、`overdrawn`、`failed`、`uncertified`；`source_gap` 的 `lot_id=null`，其余为准确批次 id。一条 lot 可同时 `failed` 和 `uncertified`；`fail` 覆盖位置不再另外算 `uncertified`。`source_gap` 仅指未知来源；unknown 位置不伪造 lot 问题。每项还给出 `first_pick`：该原因在裁片中的最早实际纬；`overdrawn` 为该 lot 在裁片内最早使用纬，`source_gap` 为所有缺口最早纬。空 `problems` 对应 `allowed=true`。
* `summary`：至少含 `shipments`、`released`、`recall`、`ready`、`hold`、`whole_unattributed_mm`，与报告一致。

## 资源与实现约束

既有合法重复量可能产生数百亿个交织位置。追溯阶段（生产计划确定后）必须在 256MiB 进程内存内处理既有 `repeat_x`/`repeat_y` 上限：不展开成布网格、不为每个交织点建立字典、不输出逐纬/逐经端台账、不擅自压低合法重复量。报告保持压缩 `segments`、批次汇总、版本引用与最早见证。归账区间可切在周期中间，多个不同 yarn 的纬区间可以重叠，不因矩形未按基础重复对齐而拒绝。仅依赖标准库，支持现有 Python 版本；不要求联网、真实机台、持久数据库或跨进程服务。

## CLI 与失败约定

`loom-trace --source INPUT --output OUTPUT`。沿用严格载入（JSON 最多 8MB、拒绝重复键与非有限常量）、先完整验证与计算、再同目录临时文件原子硬链接新建一个 JSON 报告的约定。

* 合法但不允许发货、需要召回或存在超容量的业务报告仍成功退出 0 并向标准输出打印 `summary`；
* 输入错误、已有目标、I/O 错误退出 2，已有目标逐字节不变，失败不残留部分报告；
* 错误不能靠 traceback 充当报告。
