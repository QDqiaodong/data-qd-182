# 织机多工单连续织造计划 v1（loom-batch）

使用者为样布技术员。多个工单共用同一根经轴：经轴从开机到结束不断开、不重穿，所有工单与接缝段共用 `common` 描述的穿综、绑综与格距，任何工单不得自行换经轴。本接口在全部可行计划中选出全局最优的一个，输出压缩的、可逐段展开核对的批次报告与备料数据。每个工单是一整块不可拆分的布：选中时必须完整生产一次（一个相位周期连续织 `repeat_y` 遍），不生产则为零。

本接口与 v1（loom-draft）共用同一份组织编译、踏板并集、无限重复纬向审查与千分比取整实现；v1 的输入输出、无限重复浮线口径与命令行为保持不变。

## 输入

严格 JSON 对象，顶层字段恰为 `common`、`jobs`、`bridges`、`stock`、`max_length_mm`。所有声明先完整验证再计算，包括最终不会选中的工单与接缝；未知字段、未知踏板、重复标识、布尔冒充整数均为输入错误，不得当作可跳过候选。

### common

恰含 `shafts`、`tie_up`、`warp`、`repeat_x`、`warp_spacing_mm`、`pick_spacing_mm`、`takeup_permille`、`warp_waste_mm`、`float_limit` 九个字段，含义、范围与 v1 完全一致（不允许 `picks`、`repeat_y`，它们属于各工单）。横向经端按 `warp` 连续重复 `repeat_x` 次。

### jobs

1..6 个对象，每个恰含：

* `id`：匹配 `[A-Za-z0-9_-]{1,40}`，在工单集合内唯一。
* `value`：整数 1..1000000，交付价值。
* `picks`：1..128 纬，结构与校验同 v1（每纬 `treadles` 为无重复已知踏板、`yarn` 为纱线标签）。
* `repeat_y`：整数 1..1000，同 v1 范围。
* `phases`：1..2 个互异整数，每项在 0..len(picks)-1，表示允许的起织相位。

相位 p 的一周期依次是 `picks[p:]`、`picks[:p]`；该完整周期连续织 `repeat_y` 遍，中途不得换相位。

### bridges

0..2 个对象，每个恰含 `id`（规则同工单，在接缝集合内唯一，与工单 id 命名空间相互独立）与 `picks`（1..8 纬，语义同 v1 纬记录）。一条接缝段只把这串纬织一遍；同一配方允许在不同间隙重复使用。接缝段是真实消耗材料的布。

### stock

纱线库存对象数组，每个恰含 `yarn`（v1 纱线标签规则，数组内唯一）与 `length_mm`（整数 0..10^12）。未列出的纱线库存为零；允许列出未使用的纱线。

### max_length_mm

整数 1..10^12，全批次布长上限。

## 可行计划

可行计划可以不接单，也可以选择任意工单子集、任意排列，并为每个选中工单指定一个允许相位。每两个相邻工单之间恰好选择“不加接缝”或一条 bridge 配方；首尾不得加接缝，不得连续两条接缝，不得在工单内部插接缝。

所有实际纬的升综仍是该纬踏板对应综框的并集，组织格 1 表示该经端在纬线上方。同一计划串同时满足以下两种质量口径：

* **有限经向**：对每个基础经端，其在整条有限生产串中连续 1 的最长段不得超过 `float_limit`。连续段必须跨相位周期、工单与接缝边界连接计算；串首尾不相接。没有 1 时最长为 0；全程为 1 时最长为有限的总纬数 P，不得报告 unbounded。相同基础经端在横向各次重复结果相同，按基础经端汇报。
* **无限纬向**：对每种实际生产纬（每个段的每个周期行），保留 v1 的无限重复结构审查：沿基础经端串循环检测连续 0，全 0 为 unbounded，其余最长段超过 `float_limit` 不合格。此规则不因 `repeat_x` 有限而放宽。v1 对某工单独立无限重复的经向审查结论，与该工单在有限批次中是否合格互不蕴含。

## 长度、材料与库存

设总生产纬数为 P，横向总经端 E=len(warp)×repeat_x。非空计划宽 W=E×warp_spacing_mm、长 L=P×pick_spacing_mm，且必须 L≤max_length_mm。

* 每根经纱长度 = ceil(L×(1000+takeup_permille)/1000)+warp_waste_mm。只在整根经轴上加一次损耗、做一次取整，再按基础经端 yarn 与 repeat_x 汇总；不得按工单分别取整或另加经轴损耗。
* 每条实际纬线长度 = ceil(W×(1000+takeup_permille)/1000)，按其真实 yarn 汇总，接缝纬也计入。
* 库存按 yarn 共享池检查：同一 yarn 的经、纬用量合并后不得超过库存，不得把两个方向各自与同一库存比较。
* 空计划不启动经轴：宽、长、经端数、纬数及所有用量均为零，也不消耗损耗。

## 优化与平局

只从全部可行计划中选一个，依次比较：

1. 交付工单 value 之和最大；
2. 总经纬用纱毫米数之和最小；
3. 计划身份键字典序最小。

身份键是按生产次序排列的 `(job_id, phase, bridge_before)` 三元组列表；第一项的 `bridge_before` 必为 `""`，以后 `""` 代表无接缝，否则为 bridge id。字符串按区分大小写的 ASCII 顺序、phase 按整数、列表按通常逐项字典序（真前缀较小）比较。输入数组顺序不参与平局判断。不允许只取输入顺序、贪心接单、局部最优、启发式近似，或仅比较总纱长而忽略逐 yarn 余额。

## 输出报告

`schema_version=1`，稳定字段如下；CLI 与 `loom_draft.batch.run_plan(data)` 返回结构一致。

* `plan`：身份键的对象表示数组，每项含 `job_id`、`phase`、`bridge_before`。
* `unselected_job_ids`：所有未选工单 id 的升序数组。
* `segments`：实际生产串的压缩、按序可展开描述。每段含：
  * `kind`：`"job"` 或 `"bridge"`；`id`：工单或接缝 id；
  * `phase`：工单为所选相位，接缝恒为 0；
  * `repeat_count`：工单为 `repeat_y`，接缝恒为 1；
  * `start_pick`：全批次绝对纬起点（0 起）；`pick_count`：含重复后的段长（周期行数×repeat_count）；
  * `repeat_drawdown_warp_above`：一个周期的组织矩阵，只列基础经端列，一周期一行；工单周期已旋转到所选相位；
  * `repeat_pick_lifts`：每行升起综框的升序数组（踏板并集）；
  * `repeat_yarns`：每行对应的真实纬纱标签。
  * 三者行数一致，且与 plan、踏板并集和实际纱线身份一致；按 `repeat_count` 展开即得完整生产串。
* `float_audit`：
  * `warp_floats`：按基础经端顺序，每项 `repeat_index`、`max_crossings`、`first_start_pick`（全批次最早达到该最长段的绝对纬起点；最长为零时为 null）；
  * `weft_floats`：按 `segment_index`、`repeat_pick_index` 排列，每项含该周期行的 `unbounded`、`max_crossings`，含义同 v1；
  * `passes_float_limit`：所选计划恒为 true。空计划两种数组均为空。
* `material_requirements`：`width_mm`、`length_mm`、`total_ends`、`total_picks`；`yarn` 数组按 `(orientation, yarn)` 升序，每项 `orientation`（warp/weft）、`yarn`、`length_mm`，只列正用量；`stock_balance` 为输入库存与实际使用标签的并集按 yarn 升序，每项 `yarn`、`available_mm`、`used_mm`（经纬合并）、`remaining_mm`。
* `summary`：至少含 `delivered_value`（交付价值合计）、`delivered_jobs`（交付工单数）、`bridge_picks`（接缝总纬数）、`total_yarn_mm`（经纬用纱合计）。

报告只使用上述压缩段表示，不输出完整成布网格或逐实际纬清单；repeat_x/repeat_y 达到上限时（单方向可有 128000 个实际重复位置）程序仍按压缩结构计算与输出，不因虚拟格数大而拒绝输入或擅自缩小重复量。所有数值用整数精确计算。

## CLI 与失败约定

`loom-batch --source INPUT --output OUTPUT`。沿用严格载入（JSON 最多 8MB、拒绝重复键与非有限常量）、先完整验证与计算、再同目录临时文件原子硬链接新建一个 JSON 报告的约定。成功（包括空交付）退出 0 并向标准输出打印 summary；非法输入、已有输出或 I/O 失败退出 2，已有目标逐字节不变，失败不留下部分报告。一次候选不合格只排除该候选；合法输入始终可交付空计划。Python 接口 `loom_draft.batch.run_plan(data)` 返回普通字典，非法输入抛 `ValueError`。
