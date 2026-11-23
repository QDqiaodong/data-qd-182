# 拆纬重织后的耗纱交班单 v1（loom-handoff）

使用者为织造工段调度员。批次计划（loom-batch）能算出正常织完一卷所需的用纱，但实织遇到跳花时，操作员会拆掉布尾的几纬再织：拆下的纬纱报废、不回到纱筒，上一班没签封的布尾也可能由下一班拆掉。本接口按纱筒对上实物余量，把废耗记在拆纬发生的班次并注明原织入班次，同时给下一位操作员可执行的续织边界。一次只处理一个工位的一卷布，离线运行，不通信真实织机。

本接口复用 loom-batch 的批次选择、相位、接缝、纬纱型号与材料口径；实织耗纱不改变已选计划。`batch.stock` 只是原计划的预算，实物从 `spools` 单独扣。本接口不做原料批次质量判断，也不涉及经纱回收。既有六个命令与 Python 接口的契约保持不变。

## 输入

严格 JSON 对象，顶层字段恰为 `batch`、`spools`、`checkpoint`、`shift`。未知字段、布尔冒充整数、坏引用均为输入错误。

### batch

即 loom-batch 的批次输入，按 `loom_draft.batch.validate_batch` 完整验证（含永不织到的工单与接缝）。其唯一所选方案定义计划纬序 `0..P-1`：每个段（工单按所选相位旋转后的周期 × `repeat_y`，接缝原样一遍）依次展开，每条纬的型号就是该实际生产段对应行的 `yarn`。每次成功织入一纬按整幅纬纱长度扣料，沿用批次材料取整：每纬耗 `ceil(实际宽度毫米 × (1000+takeup_permille)/1000)`，其中实际宽度 = 基础经端数 × repeat_x × warp_spacing_mm。全卷每纬耗相同，记为 `unit`。

### spools

开卷时的完整纱筒清单，1..32 项，每项恰含：

* `id`：匹配 `[A-Za-z0-9_-]{1,40}`，清单内互异；
* `yarn`：批次纱线标签规则（非空、不超过 200 字符）；
* `length_mm`：整数 0..10^12，开卷起始量。

允许列出永不使用的纱筒。开卷后不能增补纱筒、换 id 或改起始量；任何时刻一种型号最多一只在用筒。

### checkpoint

`null` 表示首班：未开始、坐标为零、无纱筒在用。否则必须是上一班交班单输出的 `checkpoint` 对象（见“交班 checkpoint”），本接口完整重新验证之。

### shift

恰含 `no` 与 `events`：

* `no`：整数 1..2147483647。首班（checkpoint 为 null）必须为 1；否则必须恰是 checkpoint 记录班号加 1。
* `events`：0..120000 个事件对象，按给定顺序处理。每个事件恰含 `op` 与另一个字段：
  * `{"op":"mount","spool_id"}`：该纱筒成为其型号的在用筒；同型号原在用筒下机保留余量，其他型号不变。允许重装原筒、装空筒。`spool_id` 必须存在于 spools。
  * `{"op":"weave","count"}`：从 `next_pick` 起按计划连续织 `count` 纬。每纬用当时该型号的在用筒全额扣一次 `unit`，并逐纬记住（纱筒, 本班号）归属。`count` 为正整数。越出 P 或 2048 纬尾料窗口、缺少在用筒或某纬扣料不足，都说明这份实织记录不自洽：整班拒绝，不发交班单。
  * `{"op":"unpick","count"}`：从当前尾部逆序拆 `count` 纬，`next_pick` 回退 `count`；不得拆已签封的前缀。被拆纬从尾料改记报废，归属仍是当时实际织入的（纱筒, 班号）；不退料、不改已扣米数。回到同一纬位重新织时建立新的归属。
  * `{"op":"seal","count"}`：从待签封尾料最前端签封 `count` 纬，`sealed` 前进、`next_pick` 不动；不能签封未织部分。被签封纬的纱耗由尾料移到成布，筒余量不变。

拆纬和签封都可能只取上班一段连续织入的一部分。所有 `count` 拒绝布尔与非整数。

## 坐标与尾窗

用计划纬左闭右开坐标记两个指针：`sealed` 是已签封卷走的前缀长度，`next_pick` 是布尾下一纬，初始都为 0。恒有 `0 ≤ sealed ≤ next_pick ≤ P`。两者之间是待签封尾料，设备上最多能预留 2048 纬可拆，即 `next_pick - sealed ≤ 2048`；`weave` 不得使该窗口超限。

## 输出交班单

`schema_version=1`，CLI 与 `loom_draft.handoff.run_handoff(data)` 返回结构一致，字段如下。

* `shift_no`：本班班号。
* `sealed`、`next_pick`、`total_picks`：本班结束时的坐标与计划总纬数 P。
* `spools`：按 `spool_id` 升序，每项含 `spool_id`、`yarn`、`mounted`（本班结束时是否在用）、`remaining_mm`、`sealed_mm`、`tail_mm`、`scrap_mm`。后四项是截至本班的量：成布（已签封）、待签封尾料、永久废耗、实物余量；每筒四项之和始终等于其开卷长度，尾料和废料都不混进已签封成布量。
* `scrap_this_shift`：按 `(woven_shift, spool_id)` 升序的 `{woven_shift, spool_id, mm}` 数组，只列本班实际拆下的耗纱，同一（原织入班次, 纱筒）合并成一行；不改写以前的交班单。
* `resume`：从当前 `next_pick` 出发，在不换筒、不签封、只按现有筒余量的前提下还能连续织多少纬。字段：
  * `count`：可续织纬数；
  * `stop_pick`：形成结论的第一条不能续织的纬（到卷尾时为 P）；
  * `reason`：`plan_end`、`tail_full`、`no_spool`、`insufficient` 之一。对每条候选纬按优先级检查：先卷尾（坐标到 P），再尾窗（尾料已满 2048 纬），再筒（无在用筒、再余量不足）；
  * `yarn`：`no_spool` 与 `insufficient` 时给出当纬型号，其余为 null；
  * `spool_id`：`insufficient` 时给出实际在用筒，其余为 null。
  * 余量恰好够扣至零的纬可以织。试算不扣实际余量、不改变 checkpoint。
* `checkpoint`：下一班必须承接的状态对象（见下节）。
* `summary`：`shift_no`、`sealed`、`next_pick`、`scrap_this_shift_mm`、`resume_count`、`resume_reason`，供 CLI 打印。

## 交班 checkpoint

调度员只把这个对象和固定的 batch/spools 交给下一班；旧班的事件文件和输出报告都不在新工位。checkpoint 为 JSON 对象，字段恰为：

* `v`：恒为整数 1；
* `base`：原 batch 与 spools 的绑定指纹。算法：batch 按“对象键排序、数组保序”的紧凑 JSON 序列化，spools 先按 id 升序再同样序列化，两者以换行拼接后取 SHA-256 的十六进制小写。因此对象键顺序不影响比较，spools 可重排，batch 的数组保持原顺序；换任何基线（错发批次、改动纱筒起始量）都拒绝；
* `shift`：最近完成班的班号；
* `sealed`、`next`：两个坐标；
* `mounted`：在用纱筒 id 的升序数组（每种型号至多一只）；
* `amounts`：与按 id 升序的纱筒清单对齐的三元组数组 `[sealed_mm, tail_mm, scrap_mm]`；
* `tail_spool`、`tail_shift`：等长数组，逐纬记录尾料 `[sealed, next)` 的纱筒索引（按 id 升序清单的 0 起下标）与织入班号。

checkpoint 用 UTF-8 紧凑 JSON 序列化（无缩进，分隔符为逗号和冒号，无额外空格，非 ASCII 按原字符编码）最多 65536 字节。历史只保留按筒累计量与尾窗内逐纬归属，尾窗最多 2048 纬，因此连续多班反复织入、拆掉同一位置也不会撑爆交接（本格式最坏约 28 KB）；缩减旧信息后仍准确承接早先班次留下的可拆尾料、已签封量和永久废耗。不能把旧日志或报告藏进其他另读文件。

接收方完整重新验证 checkpoint，拒绝：不同基线；坏格式（缺字段、多字段、类型错误、布尔冒充整数）；超长；以及自相矛盾——坐标越界或尾窗超限、尾料归属与坐标/计划型号/筒余量不符、在用筒未知或同型号两只、筒量不平衡（任一项为负、不是整纬倍数、四项之和超过开卷长度、已签封总量与坐标不符）。不要求鉴权。用同一 checkpoint 和班次包重算，结果逐字节一致。

## CLI 与失败约定

`loom-handoff --source INPUT --output OUTPUT`。沿用仓库约定：严格载入（JSON 最多 8 MB、拒绝重复键与非有限常量）、先完整验证与计算、再同目录临时文件原子硬链接新建一个 JSON 报告。合法交班单——包括续织提示为耗尽（`insufficient`/`no_spool`）或停在尾窗（`tail_full`）——退出 0 并向标准输出打印 summary；非法输入、不自洽的实织记录、坏 checkpoint 或 I/O 失败退出 2，已有目标逐字节不变，失败不留下部分交班单。Python 接口 `loom_draft.handoff.run_handoff(data)` 返回普通字典，非法输入抛 `ValueError`。
