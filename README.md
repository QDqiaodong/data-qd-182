# data-qd-182 · 织机组织工单

把穿综、踏板绑综和踏序编译成经纬组织，同时检查重复边界长浮线并汇总纱线用量。

## 独立安装

Python >=3.11，实际验证使用本地CPython3.12.14。运行与unittest仅用标准库，不依赖外部服务。每仓建立独立虚拟环境；构建工具锁定版本，公共包缓存可复用。

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
```

离线安装需要已安装的兼容Python及缓存中的setuptools==80.9.0、wheel==0.45.1。若缓存缺失，应先准备公共构建工具，不能把以上命令当空缓存可用承诺。修改源码后须重新安装本仓。

## 演示

在仓库根目录执行，目标路径须不存在；重复演示使用新文件名。

```sh
.venv/bin/loom-draft --source fixtures/input.json --output run/report.json
```

预期：4经×2纬组织repeat，0条浮线违规，总纱线380mm。

多工单共用经轴连续织造计划（全局最优选单、排序、相位与接缝）：

```sh
.venv/bin/loom-batch --source fixtures/batch_input.json --output run/batch_report.json
```

预期：交付 A、S、B 共 9 纬，交付价值 2，总纱线 49mm（经 22mm、纬 27mm），库存恰好用尽。

纱线批次追溯与历史放行复核（织造结束后按裁片核对原料批次与放行结论）：

```sh
.venv/bin/loom-trace --source fixtures/trace_input.json --output run/trace_report.json
```

预期（`as_of=8`，含 known_at=7 的归账订正）：三块裁片 S、R、U 均为 `released`，未归账 0mm。同目录 `fixtures/trace_recall.json` 把 `as_of` 收窄到 6：S、R 因 known_at=5 的独立不合格证书变为 `recall`（历史仍为当时已放行），U 仍 `released`。

成功退出0；业务质量提示或诊断不等于输入失败，见summary。非法输入或已有目标退出2，保留旧目标。JSON最多8MB，拒绝重复键、未知字段及非有限常量。先完整验证与计算，再同目录临时文件原子硬链接新建一个JSON报告。run/已忽略。

实际裁片发货台账与召回交接（本地持久、多进程安全，不连外部系统）：

```sh
rm -rf run/dispatch_store
for f in fixtures/dispatch/0*.json; do
  .venv/bin/loom-dispatch --store run/dispatch_store --source "$f" >/dev/null
done
.venv/bin/loom-dispatch --store run/dispatch_store --view
```

预期：台账 generation=3，S、R 经历 recall 后恢复 normal，4 个事件（recall×2、clear×2）全部确认，重发同面积裁片被 `cloth_overlap` 永久拒绝。也可运行 `PYTHONPATH=src python3 examples/dispatch_walkthrough.py` 查看含回执的完整生命周期。业务拒绝退出0；输入错误、request_id 冲突、存储/I/O 错误退出2。字段、提交原子性、请求重放与召回交接规则详见[发货台账契约](docs/contract-dispatch.md)。

验布机读数对齐与复验清单（编码器回跳/掉帧下，在全部完整对齐路线上保守判定裁区 clear/recheck/damaged）：
```sh
.venv/bin/loom-inspect --source fixtures/inspect_input.json --output run/inspect_report.json
```

预期：5 块候选裁区中 L13 为 `damaged`，L2、L3 为 `recheck`，L4、R2 为 `clear`。大场景验收（120000 帧、128000 纬 × 2000 经端，192 MiB 地址空间内完成）：

```sh
python3 examples/inspection_acceptance.py run/big_input.json
.venv/bin/loom-inspect --source run/big_input.json --output run/big_report.json
```

预期 summary：`{"clear": 1, "cuts": 2, "damaged": 0, "recheck": 1}`。对齐语义、裁区判定与资源约束详见[验布契约](docs/contract-inspect.md)。

共用绑综的连续织造工艺卡（一套绑综覆盖多款，附带各款常态踩法与单拍故障应急踩法）：

```sh
.venv/bin/loom-card --source fixtures/card_input.json --output run/card_report.json
```

预期：`status=ready`，`changed_pedals=0`；款 P 常态 `[["A"],["B"],["C"]]`，B 在 1 纬失灵时应急 `[["C","E"],["F"]]`、`resume_row=0`。`rejoin_limit` 改为 0 时常态变为 `[["A"],["B"],["F"]]`、应急收为 `[["C","E"]]`；删去踏板 F 与含 F 的禁配对则该款 infeasible，但在无故障要求时同一缩减机台仍可正常织。选卡规则、输入限制与报告结构详见[工艺卡契约](docs/contract-card.md)。

拆纬重织后的耗纱交班单（按纱筒对实物余量，废耗记在拆纬班次并注明原织入班次，给出续织边界）：

```sh
.venv/bin/loom-handoff --source fixtures/handoff_shift1.json --output run/handoff_1.json
```

预期：首班挂 F1、G1 织 3 纬签封 1 纬，`sealed=1`、`next_pick=3`，可续织 1 纬到卷尾。把报告中的 `checkpoint` 对象和固定 batch/spools 交给下一班即可续接：`PYTHONPATH=src python3 examples/handoff_walkthrough.py` 演示三班链（二班拆 2 纬，废耗记在原织入班次 1 的 F1、G1 各 3mm，续织在纬 2 因 F1 余量为零停下；三班改挂 F2 织完签封），以及纬 2 改由 F2 织入的归属变体。合法交班单（含续织耗尽或停在尾窗的提示）退出 0；输入、不自洽实织记录、坏 checkpoint 或 I/O 错误退出 2。坐标、事件、checkpoint 格式与尺寸上界详见[交班契约](docs/contract-handoff.md)。

## 数据契约和边界

采用明确升综/格距模型；无限平铺风险审查与有限生产用量分别列明，不通信真实织机。 字段、单位、顺序及失败约定详见[契约](docs/contract.md)；多工单批次计划的输入、可行性与最优性规则、压缩报告结构详见[批次契约](docs/contract-batch.md)；纱线批次追溯、版本化时间快照与历史放行/召回规则详见[追溯契约](docs/contract-trace.md)；实际裁片发货、占用防重、召回事件与请求重放详见[发货台账契约](docs/contract-dispatch.md)；验布机帧对齐、全部完整路线上的覆盖/缺陷判定与复验清单详见[验布契约](docs/contract-inspect.md)；共用绑综的整体绑综选择、各款常态表与单拍故障应急踩法详见[工艺卡契约](docs/contract-card.md)；实织拆纬的纱筒实物余量、废耗归属、续织边界与多班交接详见[交班契约](docs/contract-handoff.md)。

样例全部为原创合成数据，见[来源](fixtures/SOURCES.md)。代码和样例MIT许可；业务命令和测试不访问网络、不启动容器。功能边界以实际源码和契约为准。
