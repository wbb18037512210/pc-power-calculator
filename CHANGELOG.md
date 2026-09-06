# PC 电脑用电电费计算器 — 变更日志

PySide6 + QtCharts，完全离线。PyInstaller onefile 打包，产物部署到桌面。
数据文件（session.json / history.json / 用电日报）与 exe 同目录 —— 冻结版 `BASE_DIR = dirname(sys.executable)`。

---

## v18.39 — 内存温度「无探头」明示（不再显示像坏掉的「—」）

### 内存温度为什么还是不显示（结论先行）
- **这是硬件事实，不是 bug**：本机（B450M-PLUS + Micron/兴嘉辰 DDR4）经 LibreHardwareMonitor
  可读的全部 16 路温度里，**没有任何一条 DIMM/内存温度**。SuperIO NCT6793D 的 #1–#6 是
  主板/CPU 插座/VRM 区，三条无名 37°C 是 SATA 机械盘，都不是内存。
- 内存条自带 SPD 温度探头只有 **DDR5 / 部分高端 DDR4** 才有；本机这两条没有。
  HWiNFO、AIDA64 同样读不到（已与 LHM 面板逐条核对）。

### 本次改进（v18.38 只放在 tooltip，不悬停看不到）
- 构成表 / 悬浮窗的「内存」行：LHM 已连但本机无探头时，单元格直接显示
  **「无探头」**（灰色，与详情窗口坏通道同色系），不再留一个像故障的「—」。
- 仍是三级兜底：LHM 未连 → 仍显示「—」（tooltip 提示如何开启 LHM）；
  将来换上带探头的 DDR5 → 自动显示真实温度数字。
- tooltip 已有完整原因说明，本次未改动。

### 其他
- 单测 75 项全过；`test_headless.py` 全过。

---

## v18.38 — GPU 使用率改实测占用 + 内存温度无探头的说明

### GPU 使用率不再用「功耗 ÷ TDP」估算（用户反馈与任务管理器差距大）
- **根因**：此前使用率列对 GPU 用 `功耗 ÷ TDP` 推算，但那是**功率利用率**，
  不是 SM 占用率。功耗里含风扇、显存、供电损耗等固定开销，且硬件解码走的是
  Video Engine 专用引擎 —— 功耗上去了、SM 占用几乎不动。
  本机实测样本：**功耗 47.1W（估算 26%）时真实占用只有 3%，偏差 +23 个百分点**。
- 改为真实占用，三级取值：
  1. `nvidia-smi utilization.gpu`（常驻流加查该字段，与任务管理器同口径）
  2. LHM 的 `GPU Core` Load（A 卡 / 无 nvidia-smi 时的真实来源）
  3. 功耗 ÷ TDP（仅在两者都拿不到时兜底）
- N 卡常驻流查询字段：`power.draw,temperature.gpu,utilization.gpu`。
- 使用率列新增 tooltip 标注来源（实测 / 估算），避免"这数哪来的"的疑问。
- 实测校验：连续 6 次采样与 nvidia-smi 直查完全一致（8~9% vs 8~9%）。

### 内存温度：说明清楚为什么读不到
- 补充 LHM 硬件映射：DIMM 的 HardwareId 是 `/memory/dimm/N`，此前只映射 `/ram`，
  带 SPD 探头的内存条会被漏掉（DDR5 / 部分高端 DDR4）—— 已修正，有探头即可显示。
- **本机两条内存（Micron DDR4 + 兴嘉辰 DDR4）没有 SPD 温度探头**，LHM 只报容量
  与 SPD 时序、无 Temperature，属硬件限制（HWiNFO、AIDA64 同样读不到）。
  温度列 tooltip 现在明确说明这一点，而不是给一个让人以为是软件故障的 —。
- 传感器详情窗口新增「显示全部传感器」勾选：可查看内存的容量与 SPD 时序
  （默认视图只显示温度/风扇/控制/功耗/频率/占用，130 行 → 全部 254 行）。

### 其他
- 单测 `tests/test_hw_detect.py` +3 项（LHM GPU 占用解析 / DIMM 映射 /
  功耗≠占用的反例回归），共 75 项全通过；`test_headless.py` 全通过。

---

## v18.37 — 温度/转速以 LibreHardwareMonitor 为参考显示 + 修 ensure_lhm 崩溃

### 以 LHM 为参考显示温度/转速
- 新增**传感器详情**窗口（控制条「传感器」按钮 / 双击构成表「温度·转速」列）：
  按 LHM 的硬件分组列出全部温度、风扇、风扇控制、功耗、频率、占用读数，
  列结构与 LHM 面板一致 —— 传感器 | 最小 | 当前 | 最大；分组标题保留层级
  （如 `B450M-PLUS › Nuvoton NCT6793D`），带「刷新」按钮。
- 构成表与悬浮窗的温度/转速列新增 **LHM 参考读数 tooltip**：逐项列出原始
  传感器名与读数（例：`AMD Ryzen 5 5600X · Core (Tctl/Tdie)：72.3 °C`），
  可直接和 LHM 面板对表；风扇行列出每一路在转的风扇（含显卡风扇）。
- 语义温度新增 **GPU**：与 LHM 面板一致取 `GPU Core` 优先于 `GPU Hot Spot`；
  nvidia-smi 取不到时回退 LHM。磁盘参考读数过滤 NVMe 的告警/临界阈值
  （那是阈值不是实测温度，此前会混进列表造成误读）。

### 修复（v18.36 遗留真实 bug）
- **`hardware.py` 用了 `os.path.exists` 却从未 `import os`**：LHM Web Server
  一断（进程被关/开机未自启），`ensure_lhm()` 立即抛 NameError，
  且它位于 `_query_sensors` 的 try 之外 → 整个采样链路抛异常。
  这正是"温度/转速时好时坏"的隐藏原因。已补 `import os` 并加回归断言。

### 其他
- 控制条按钮 9 → 10（新增「传感器」），仍一行平铺均分。
- 悬浮窗温度列宽 40 → 48px，避免 `1942 RPM` 被截断。
- 单测 `tests/test_hw_detect.py` +13 项（LHM 树/父层级/probe 各部件/阈值过滤
  /GPU 语义温度/os 回归），本机 72 项全通过；`test_headless.py` 全通过。
- 本机实测对照：CPU 72.3°（Tctl/Tdie）、主板 27.0°（SuperIO #1）、
  GPU 41.0°（Core）、风扇 1951 RPM（机箱）+ 1209 RPM（显卡）。

---

## v18.36 — 真实温度/风扇数据源(LHM Web Server) + 悬浮窗构成四列 + 迷你进度条

### 数据源：接入 LibreHardwareMonitor Web Server
- **穷举实测确认**：本机（管理员权限）`Win32_Fan`、`Win32_TemperatureProbe`、
  `MSAcpi_ThermalZoneTemperature`、`ThermalZoneInformation` 计数器、
  厂商命名空间（ASUS/Gigabyte/MSI/ASRock/HP/Dell）全部为空 —— Windows 免驱
  没有 CPU/内存/主板温度与风扇转速接口，此前这些只能显示 —。
- 安装 LibreHardwareMonitor v0.9.6（`D:\tools\LibreHardwareMonitor`）+ PawnIO 驱动；
  新版 LHM 已移除 WMI Provider，采集改走其 Web Server（`127.0.0.1:8085/data.json`），
  代理被绕过（ProxyHandler({})）；OpenHardwareMonitor WMI 保留为回退。
- 本机实测取到：CPU Tctl/Tdie（AMD 5600X）、主板 SuperIO NCT6793D 温度、
  机箱风扇 + GPU 风扇转速；SuperIO 坏通道（110/106/103°）用 5–100°C 过滤。
- 构成表温度列接入：CPU/内存/主板取 LHM 温度；风扇行温度列改显最高转速 RPM。
- 硬件信息块：处理器温度兜底 LHM；主板行新增实时温度。
- LHM 已设注册表 Run 开机自启（未装 LHM 时应用一切照旧，显示 — 不报错）。

### 悬浮窗功耗构成四列化
- 构成行从「部件+功耗」扩为「部件 | 使用率 | 功耗 W | 温度/转速」（320px 宽），
  数据随主界面同源刷新；合计行保持金色两列。

### 使用率进度条改迷你样式
- 之前 16px 实心条填满单元格，视觉过硬；改为 22px 单元格内 10px 细条垂直居中
  （「填充一半、留下一半」），百分比移到条右侧小标签。

### 其他
- 硬件信息里处理器/物理内存的 ASCII 使用率条移除（与构成表进度条职责重复）。

---

## v18.35 — 实时卡片布局修复 + 构成表四列 + 控制条平铺 + 风扇转速

### 布局根因修复：读数网格塌缩（用户截图"空白"的真凶）
- **根因**：v18.17 的"全标签 Ignored 防顶宽"在网格场景下把 6 个读数列中 4 列的
  sizeHint 记 0，QGridLayout 按 sizeHint 分配后其余列全被压成 0 宽——所有读数
  叠成一列、卡片右侧 ~400px 全空。Ignored 适合横排互斥场景，不适合网格。
- 修法：改 `setMinimumWidth(1)`（显式覆盖 minimumSizeHint），读数 **2 行 × 3 列**
  均分铺开（旧 4+2 布局第二行右侧两格本来也是空的）。
- 硬件信息块改**双列 table**（左：主板/处理器/内存/网络；右：显卡/磁盘/风扇），
  内容全高 283→168px，视口 202px，**不再滚动**；`setMinimumHeight(220)` + 窗口
  高度 880→920 保证完整显示。
- 「启动模式」行按用户要求删除（UEFI/SecureBoot 低频信息）。

### 功耗构成表四列化（用户指定）
- 列顺序：**部件 | 使用率 | 功耗 W | 温度**；行顺序固定 CPU → GPU → 内存 →
  风扇 → SSD → HDD → 主板/芯片组 → 显示器 → 外设（未列出的排末尾）。
- 使用率列是 **QProgressBar**（CPU=真实负载、GPU=估算功率/TDP、内存=内存占用；
  其余显示 —），<70 绿 / <90 橙 / ≥90 红三档，跨档才重设样式（避免每 2s 全表
  setStyleSheet）。差分更新路径同步刷新进度条与温度。
- 温度列：CPU(75/85)、GPU(65/78)、SSD/HDD(45/55) 三档配色，无传感器显示 —；
  磁盘温度按 media 类型从 `disk_temps` 匹配。

### 控制条平铺（用户反馈）
- 9 个按钮改**一行均分铺满**卡片（旧 5+4 两行第二行右侧空白）；实测按钮宽
  103–124px 正常。状态标签「监测中…」挪到实时卡片详解行行尾。

### 新增：风扇转速
- `hardware.fan_rpms_cached()`：读 LibreHardwareMonitor / OpenHardwareMonitor 的
  WMI Sensor(SensorType='Fan')（Windows 无标准风扇接口，装了才显示，否则 —）。
  10s 缓存节流、双命名空间皆无时 120s 才重试；在 worker 线程执行不卡 UI。
- 显示在硬件信息块磁盘下方；`_parse_fans_json` 纯解析函数有 6 项单测。

### 测试
- 6 个测试文件 + test_headless 全部通过；修复 test_headless set_interval 用例的
  时序脆弱点（worker 间隔 3.5s 时 sleep 3.2s 偶发不够 → 4.2s）。

---

## v18.34 — 硬件信息并入实时卡片（删除最左侧栏）

### 布局重构（用户反馈：截图红框标注）
- 最左侧 288px 系统信息侧栏整体删除，硬件信息块（重新检测按钮 + 检测状态 +
  AIDA64 风格富文本）并入**实时卡片右半区**（此前 v18.5 删除本机配置卡片后
  一直空着的区域），横排比例 读数 3 : 硬件信息 2。
- `_sysinfo_panel()` 从固定宽侧栏改为内嵌块，成员 `sysinfo_view` /
  `_hw_detect_lbl` 名字不变，`_update_sysinfo` / `_redetect_hardware` 零改动；
  信息区改浅灰圆角底（`#f7f9fc`）与白色卡片区分。
- `_live_card` 末尾的"全标签 Ignored 防顶宽"需排除 `_hw_detect_lbl`
  （Ignored 在横向 stretch 竞争下会被压成 0 宽——v18.33 副标题同款坑）。
- `_refresh_header_static()` 增加 `_sys_static` 未初始化防御（_build_ui 里
  标题行先于 _live_card 构建，调用顺序与旧版相反）。
- 删除侧栏后主界面不再被 288px 顶宽，minimumSizeHint 592px（旧 ~1080）。

### 测试
- 全部 6 个测试文件 + test_headless 通过；视觉探针截图确认新布局。

---

## v18.33 — 运行时间/操作系统上移标题行 + 详情独占屏幕

### 运行时间、操作系统移到主界面标题后（用户反馈）
- 左侧系统信息栏的「运行时间」「操作系统」两行上移到顶部标题 `⚡ PC 用电电费计算器`
  后面：`运行 3时26分 · 09-06 [日] 13:24`（灰字 12px，随采样每拍刷新，QLabel 开销极小）
  ＋ `Windows 10 家庭版 · 64 位`（静态，启动采集/重新检测硬件后回填）。
- 左侧栏自「启动模式」开始，不再重复这两项；原副标题「实时监测 · 24 小时汇总」精简为
  版本号 `v18.33`（完整文案移入 tooltip）。
- **布局坑（重要）**：QLabel 的 `minimumSizeHint`=整段文本宽度，会顶宽主界面；修复不能用
  `Ignored` 策略——布局把 Ignored 项宽度按 0 分配、文字直接消失（v18.32 的副标题其实
  就是这么被压没了）。正确做法：`setMinimumWidth(1)` 显式覆盖 minimumSizeHint
  （`setMinimumWidth(0)` 等于未设置，无效）。

### 双击曲线详情 → 主界面自动隐藏/恢复
- 双击打开曲线详情时主界面自动隐藏，详情独占屏幕；关闭详情后 `_show_window()`
  恢复主界面（try/finally 保证异常路径也恢复）。
- 动机：主窗口是 Tool 型（不占任务栏），模态弹窗遮挡 + 关闭后主界面沉到其它窗口后面时
  用户没有任务栏按钮可切回。独占式交互同时消除遮挡与"找不回"两类问题。
- 自动化验证（QTest 真实双击 + 全屏截图）：详情打开=主界面隐藏 ✓，关闭后恢复 ✓。

### 测试
- test_headless 侧栏断言更新：侧栏不再含「运行时间/操作系统」，新增标题标签内容断言；
  全部 6 个测试文件 + test_headless 通过；双击闭环探针 ALL PASS。

---

## v18.32 — 报告横屏版式 + 双击曲线详情 + 刷新率 1s + 主界面精简

### 报告 PNG 横屏排版（根因修复）
- **根因**：报告 HTML 用 `display:flex` 分栏，但 **QTextDocument 不支持 flex**（实测 4 个
  flex 子项 x 全为 4、y 逐行递增，退化成块级竖排）→ 导出 PNG 是 960×1476 的竖长条（0.65:1）。
- 版式改用 table（QTextDocument 唯一可靠的横排方案）：标题行 → KPI 四格一行 → 主体三栏
  （配置/构成/峰谷 ｜ 预估/待机/软件Top10 ｜ 逐小时柱图+悬浮窗快照）。
- 逐小时柱图改双列（24 行→12 行）；**柱条必须用 HTML 属性 `width+bgcolor` 的嵌套 table**——
  实测嵌套 table 里 CSS `width/background` 不生效（条形不渲染）、空 div 高度塌缩，三层写法
  只有 HTML 属性层可靠。
- `_render_png` 自适应：逻辑宽度 1100–2400 迭代搜索到 宽/高≈1.70，输出恒为 ~3000px 宽。
  实测：满内容 2334×1320（1.77:1 正好 16:9），旧版 0.65:1。
- mini 悬浮窗快照块从 9 行两列表格改为 6 行短句（窄栏内不再错行）。

### 双击功耗曲线 → 大图详情
- 主界面曲线标题加「双击查看详情」；新增 `ChartView`（QChartView 子类）：主界面双击开详情，
  详情内双击复位缩放、框选放大（rubber band）。
- 详情含当前/平均/峰值统计，**曲线实时跟进新采样点**（弹窗内 1s 定时同步 `_chart_pts`）。
- 修复：`QXYSeries.append` 不接受 `(QDateTime, float)` 重载，x 需传毫秒浮点（与主图一致）。

### 刷新率默认 1 秒
- `SAMPLE_MS` 2000→1000；曲线详情弹窗内置刷新率切换（1/2/3/5/10 秒），经
  `worker.set_interval` 线程安全生效并随会话落盘。

### 主界面精简（用户反馈）
- **移除「月度预算」卡片**：预算功能保留（设置面板可设、达阈值仍弹预警、报告有进度）。
- **左侧硬件信息简化**：一行一主题、子行缩进 12px；砍 TPM/域/BIOS 版本/Cache/插槽数/MAC/
  网关/DPI；CPU/GPU/盘/网卡型号统一 `_short_model()` 精简（去 '6-Core Processor'、
  'Family Controller'、'-' 后料号等）。288px 窄栏不再折行错乱。
- **详解行精简**：`直流 X W + 电源损耗 Y W = 插座 Z W（效率 E（动态））· 显示器开（计 30W）`
  → `插座 X W ＝ 直流 Y + 损耗 Z · 效率 E 动态 · 显示器 +30W`（嵌套括号消除、长度减半）。

### 测试
- test_headless 同步新文案断言（动态徽标、损耗解析、显示器 +/关 标注、侧栏关键块）；
  新增嵌套 table 柱条回归保护（headless 渲染 PNG）。

---

## v18.31 — 硬件识别增强（移植 TubaTools 三项能力）

> 参考 `TubaTools`（图吧工具箱）的 `HardwareInfoService` 做逐项对比后，挑出对**功耗估算精度**
> 真正有影响的三项落地。纯展示向能力（JEDEC 厂商解码、三字母厂商码表、CPU-Z 覆盖）不移植——
> 后者需外部 exe，与本项目零依赖离线定位不符。

### P0 虚拟/伪显卡过滤（消除灾难性误判）
- 原过滤只有 `Virtual / Basic / Microsoft` 三个词，**实测漏掉**：
  `GameViewer Display Adapter`（远控/串流虚拟显卡，本机就装了）、`Idd Desk Adapter`、`DDA Wrapper`。
- 一旦被当成主显卡，`identify_gpu` 返回 **75W / conf=low**，而本机真实 GTX 1080 是 **180W（−58%）**。
- 黑名单扩到 9 个关键词，抽为模块级常量 `VIRTUAL_GPU_KEYWORDS` 便于单测。

### P1a 便携机判定改以机箱类型为准
- 原实现只看 `Win32_Battery` 计数 —— **台式机接 UPS 会误判成笔记本**（UPS 经 USB HID 暴露电池）。
- 改为 `Win32_SystemEnclosure.ChassisTypes`（8/9/10/11/14/30/31/32）为准，电池仅兜底，
  并排除 `Virtual/VMware/HVM/KVM/QEMU/XEN/Hyper-V` 等虚拟机型号。新增 `HardwareInfo.is_laptop`。

### P1b 显示器功耗改按 EDID 物理尺寸建模
- 原逻辑按显卡分辨率三档粗估（1080p 22W / 1440p 30W / 4K 45W），超宽屏、带鱼屏会落到错误档。
- 新增 `root\WMI WmiMonitorBasicDisplayParams` 采集 EDID 物理宽高(cm)，按 PnP 码与
  `Win32_PnPEntity(PNPClass=Monitor)` 关联，换算英寸数（本机实测 54×31cm → **24.5 英寸**）。
- 功耗模型改为「面积 × 0.0181 W/cm² × 分辨率系数」连续估算。单位功耗**标定自
  24.5" 1440p = 30.0W，与旧常量 `monitor_1440p` 完全一致**，既有估算结果不跳变
  （本机实测 30.0W → 30.3W，差 +0.3W）。
- 台数取 `monitor_count` 与 `monitors` 的并集：测到尺寸的按面积精算，测不到的按分辨率档补齐；
  **取不到 EDID 时完整回退旧逻辑**，行为与 v18.30 及之前完全一致。

### 测试
- 新增 `tests/test_hw_detect.py`（49 项）：虚拟卡过滤 / 笔记本判定 / 显示器建模 / `build_model` 回归保护。
- 修复 `test_degraded`、`test_ui_slots` 退出码为 127 的问题：产品行为是"关闭=最小化到托盘、
  监测继续"，采样线程本就不停，测试进程需显式 `_force_quit` 收尾，否则 CI 会误判失败。

---

## v18.30 — 修复「历史趋势」点击无反应 + 全局异常可见化

> 现象：点击「历史趋势」按钮毫无反应（无弹窗、无报错）。

### 根因
`open_history()` 中对 `QBarSeries` 调用了 `setColor()`，而 **PySide6 里 `setColor` 只存在于 `QBarSet`，
`QBarSeries` 没有该方法** → 抛 `AttributeError`。程序以 `--windowed` 打包、**没有控制台**，
异常被静默吞掉，用户侧只剩「点了没反应」。

### 修复
1. **柱状图配色**改为 `bar_set.setColor(QColor("#2f6bff"))`（作用在 QBarSet 上），图表双轴（柱=用电量 kWh / 折线=电费 ¥）恢复正常。
2. **新增全局异常钩子 `_install_excepthook()`**（`main()` 首行安装）：
   未捕获异常 → 写 `_log` + 打 stderr + 有 QApplication 时弹一次错误框（15 秒自动关闭）。
   根治「窗口化运行下槽函数异常 = 静默无反应」这一整类问题。
3. **新增 `tests/test_ui_slots.py`**：逐个冒烟 9 个弹窗/操作类槽函数
   （`open_history / open_hourly / open_apps / open_compare / open_sim / open_settings /
   export_csv / export_report / _save_settings_panel`），任何异常即 Fail，防止同类回归。
   - 支持单槽运行：`python tests/test_ui_slots.py open_history`（便于外部 timeout 隔离定位）
   - `--all` 追加 `_redetect_hardware`（依赖 WMI，较慢，默认跳过）
   - 坑记录：`QMessageBox.information/critical` 是 **C++ 静态方法**，走 C++ 的 `exec()`，
     仅 patch `QMessageBox.exec` 拦不住，必须连静态方法一起替换，否则测试会真起模态循环卡死。

### 验证
- `tests/test_ui_slots.py` → `UI_SLOT_SMOKE_OK (9 slots)`
- `test_core`(12) / `test_parity` / `test_monitor_hook` / `test_degraded` / `test_headless` 全绿

---

## v18.29 — 代码审查报告 §1.2 主要不足(W) 全部修复

> 用户要求：依据 `代码审查评估报告_v18.29.md` 的「1.2 主要不足(W)」，**全部解决** 7 项弱点。
> 版本号仍为 v18.29（本轮为代码质量修复，未改功能版本）。

### W1 · 孤儿引擎模块 `PowerEngine` 漂移
- `power_core.PowerEngine` 此前被 `MainWindow` 零引用，且三处已与 `MainWindow` 漂移：
  - D1 基准效率 `0.89` vs `0.85` → 统一为「实时效率优先，缺省 0.85」；
  - D2 冷启动 `avg_w` `239.4` vs `0.0` → 冷启动（累计电量为 0）回退到插座功率兜底；
  - D3 缺 `wall_month` / `idle_streak_ms` / `disp_saved_wh` / `hourly` 状态 → 补齐字段并随 simulate 返回 `wall_month`。
- `MainWindow._simulate` 改为**委托** `PowerEngine.simulate`（单一真相源），删除本地重复实现。
- 新增 `tests/test_parity.py`：把旧 `_simulate` 复刻与引擎对照（热态 + 冷启动），任一方改坏口径立即报错。

### W2 · 硬件识别静默大误差（RTX3050 +69% / Arc A770 +33% / Ultra9 −48%）
- 新增 `hardware_id_v2.py`：`GPU_TDP_EXTRA`（+27 型号）、`CPU_TDP_EXTRA`（+31 型号）增量表、`find_key_v2`（预编译索引 + 后缀剥离）、`identify_gpu/identify_cpu` 返回 `(watts, confidence, method)`，`confidence ∈ {high, medium, low}`。
- `power_model.build_model` 改为**置信度感知**：低置信度不再静默翻车，模型带 `cpu_conf`/`gpu_conf` 标记。
- 验证：RTX 3050→130W、Arc A770→225W、Core Ultra 9 285K→125W、i9-14900KF→125W 均命中；GPU_TDP 增至 75 项、CPU_TDP 增至 70 项。

### W3 · 105 个 `except Exception`（53 个完全静默）
- 引入 `self._degraded` 降级账本 + `_mark_degraded/_clear_degraded/_degraded_summary/_refresh_degraded_status`，状态栏标签与报告内均展示降级项。
- 三段式约定（写入模块注释）：① 硬件探测类——记日志 + 状态栏标注「部分功能降级」；② UI 刷新类——保留静默但加「已知：UI 刷新失败可忽略」注释；③ 落盘类——必须留痕并给用户可见信号。
- 崩溃防护：`__init__` 硬件检测失败不再拖垮整个程序，降级到空模板并登记账本；`_load_session_maybe` / `_save_session` / `collect_system_info` 失败均可见。
- 新增 `tests/test_degraded.py` 守护账本行为。

### W4 · `MainWindow` 膨胀（3478 行 / 81 方法）
- 抽出 `_init_defaults()`（~80 行属性默认值），`__init__` 由 ~155 行降至 ~75 行；
- 抽出 `_build_advanced_settings_rows(fl)`（告警/预算/待机/自启/显示器分组），`_build_settings_panel` 拆分；
- 抽出 `_report_hourly_bars()`，`_build_report_html` 拆分。行为逐字节等价，headless 全量冒烟通过。

### W5 · 43 个设置字段双份维护
- 新增模块级 `_SETTINGS_SPEC` 单一真源（29 个配置字段 + 默认值）；
- `_save_session` / `_load_session_maybe` 改为经 spec 循环读写，新增设置只改 spec 一处；`_sync_settings_to_model` 统一把配置同步进功耗模型。

### W6 · 识别结果无「未识别」信号
- 系统信息面板 CPU/GPU 行在低置信度时显示琥珀色「⚠ 型号未识别·估算可能偏差，建议校准」徽标；
- 报告卡片同步展示该提示，引导用户校准。

### W7 · `_find_key` 每次调用全表正则重归一化（182µs/次）
- `power_model._find_key` 原地替换为 `find_key_v2`（预编译索引 + 按需后缀剥离），热路径不再每调用全表归一化；
- 准确性测试不变（RTX 3060 Ti 不误判为 3060、RX 6800 XT 不误判为 6800 等）。

---

## v18.29 — 源码内嵌 EXE（单一文件自包含源码）

> 用户要求：把源码打包进 EXE 文件内部，而非单独附带一个源码 zip。

### 实现
- 新增 `package_source.py`：白名单收集可重建工程所需的源文件（main/hardware/power_model/power_core、
  build_exe.bat/kill_instances.py/README/CHANGELOG/tests/），排除 `_probe_*`、`_diag_*` 等调试脚本与
  构建产物；支持 `--bundle-dir`（复制到目录供嵌入）与 `--out/--ver`（生成独立 zip 两种模式）。
- `build_exe.bat` 在 PyInstaller 前调用 `package_source.py --bundle-dir _src_bundle`，
  并以 `--add-data "_src_bundle;src"` 把源码整体打进 onefile exe；打包结束后清理临时目录。
- 主窗口新增「导出源码（内嵌于 EXE）」托盘菜单项（`_export_source`）：运行时从
  `sys._MEIPASS/src` 读取内嵌源码并原样导出到用户所选目录；开发 / onedir 下回退到 `BASE_DIR` 真实源码。
- 进程内源码以 `src/` 子目录形式随 exe 自包含，单文件即可携带全部源码，无需额外附件。

### 注意
- 内嵌源码体积约 110 KB，对 49 MB 的 exe 体积影响可忽略。
- 后续仍可用 `python package_source.py` 单独生成 `release/<名>_源码_<ver>.zip` 以手动分发。

### 独立提取小程序（SourceExtractor）
- 新增 `extract_source.py`：仅用标准库（struct / zlib / ctypes）自带一个极简 PyInstaller
  CArchive(PKG) 读取器，从主 exe 的归档中解析并提取 `src/...` 条目；不依赖 PySide6 / 完整
  PyInstaller，因此可单独打包成体积极小的 exe（约 7.5 MB）。
- 配套 `build_extractor.bat`：`--onefile --windowed`，剔除 PySide6 / PyQt / tkinter / numpy / PIL
  等重型依赖，产物 `SourceExtractor.exe` 部署到桌面。
- 用法：双击自动在同目录查找「PC用电电费计算器*.exe」并提取到 `<主名>_源码`；也支持命令行
  `SourceExtractor.exe <主程序exe> [输出目录] --silent`，或把主 exe 拖到本工具上。
- 验证：用官方 `CArchiveReader` 做逐字节比对，v18.29.exe 内嵌的 15 个源码条目（含修正后的主 spec）
  与仓库源文件 **15/15 逐字节一致**。

### 构建脚本修复
- `build_exe.bat` 版本解析由 `tokens=2`（默认分隔符含 `=`，会把 `APP_VERSION = "v18.29" # 注释`
  的第 2 个 token 误取为 `=`）改为 `tokens=2 delims==` 并增加 `delims=#` 截断行尾注释，
  使产物稳定命名为 `PC用电电费计算器_v18.29.exe`（此前曾误命名为 `_=.exe` / 含注释乱码）。
- `package_source.py` 在打包快照时把主 spec 的 `datas` 规范化为含 `('_src_bundle','src')`，
  保证从提取出的源码重新 `pyinstaller PC用电电费计算器.spec` 仍可复现内嵌构建。

---

## v18.28 — 真实显示器电源状态识别（修「显示器关闭后仍无法识别」）

> 用户反馈：手动按显示器电源键关屏后，程序仍记成「显示器开启」、照常计 30W。
> 根因：`display_auto_off()`（hardware.py:646）只靠「空闲时长 ≥ 系统熄屏超时」启发式推断，
> 只能识别**系统自动熄屏**；手动按显示器电源键关屏时系统层面无感知，永远推断不到。

### 修复方案
- **接入 Windows 真实电源事件**：向主窗口注册 `GUID_MONITOR_POWER_ON`
  （`{0273105A-6A1B-4244-AD7A-3A0B30C60E5D}`）通知，拦截
  `WM_POWERBROADCAST`(0x0218) / `PBT_POWERSETTINGCHANGE`(0x8013) 事件，
  从 `POWERBROADCAST_SETTING.Data` 直接读出显示器开关真值（0=关屏 / 1=开屏）。
- 新增模块级 ctypes 结构 `_GUID` / `_POWERBROADCAST_SETTING` 与辅助
  `_parse_guid_str` / `_guid_to_str`（字符串 ↔ `_GUID` 互转，大小写/花括号容错）。
- 主窗口新增 `_install_monitor_power_hook()`（窗口原生句柄就绪后调用，
  `winId()` 强制创建；注册失败 / 非 Windows 静默退回空闲推断）与
  `nativeEvent()` 事件分发：命中显示器电源事件即更新
  `self._monitor_phys_off` 并实时重算 `display_on`、刷新读数、落盘、同步托盘菜单勾选。
- `_display_on()` 自动检测分支前插入**最高优先级短路**：`_monitor_phys_off` 为真直接返回 False，
  不再走空闲推断 —— 手动关屏也能即时扣掉显示器 30W。
- `main()` 在 `w.show()`（tray 模式 `w.hide()` 同理，隐藏窗口仍会创建原生句柄）之后统一调用
  `_install_monitor_power_hook()`，确保钩子只在句柄就绪后挂载。
- 手动「记为关闭 / 开启」三态标记仍优先于本自动检测（与既有行为一致）。

### 交付验证
- `main.py` 通过 `py_compile`（已修正一处误置在 `return` 之后的死代码
  `self._sync_disp_menu()`，移入事件处理块内正常执行）。
- 后续计划（未本次完成）：补一个不拉起完整 UI、仅验证
  `RegisterPowerSettingNotificationW` 注册 + nativeEvent 解析的单元/集成测试；
  以及把「显示器开关」状态也写入 `session.json` 持久化（重启后保留最后一次真实电源读数）。

---

## v18.27 — 代码审查评估后的质量整改（按 v18.26 评估报告）

> 依据 `代码审查评估报告_v18.26.md` 的 P0/P1 项整改；高风险的「上帝类大重构 / 40 字段
> Settings dataclass」按报告自身的「绞杀者模式」建议**留作后续增量**，本次只落实安全收益项。

### P0 — 正确性 / 可测性
- **W3 硬件识别准确性 bug 修复**：`_find_key` 由「子串包含、先命中短前缀」改为
  **最长匹配优先 + 词边界正则 + 归一化**。修复 `RTX 3060 Ti`(200) 被错认成 `RTX 3060`(170)、
  `GTX 1660 Super`(125) 被错认成 `GTX 1660`(120) 等；`RX 6800 XT` 不再被 `RX 6800` 截短。
  - **回归发现**：报告自带补丁的 `_norm` 无差别剥离 `graphics`，会把键 `Radeon Graphics`
    变成 `radeon`，导致它误匹配所有 Radeon 独显（如 `RX 5800 XT` 错认成 25W 核显）。
    已修正为「仅在 Intel 家族词后剥离 graphics」，并补 `test_find_key_radeon_graphics_only_matches_integrated` 防回归。
- **W1 测试套件对齐 v18.26**：`test_headless.py` 不再断言已删除的 `_render_pdf`/`%PDF-`，
  改为断言 `_render_png` 落盘 PNG（校验存在性 / 字节数 / PNG 签名）。修前该测试在第 832 行必 `AttributeError` 失败。

### P1 — 可维护性 / 性能
- **W2 绞杀者第一步**：新增零 Qt 依赖的 `power_core.PowerEngine`（瞬时估算 / 单一·峰谷·阶梯计费 /
  峰谷分段累计 / idle 识别 / 节能模拟），并配套 `tests/test_core.py`（纯逻辑、不拉起 Qt）。
  MainWindow 计费逻辑暂未重写委托（避免大改引入回归），下一步再逐步迁移。
- **W7 删除死代码**：移除 `_save_settings_panel` 内 `if True:` 空壳分支，赋值体缩进一并收平。
- **W6 构建脚本可移植**：`build_exe.bat` 去掉硬编码绝对路径，改用 `cd /d "%~dp0"`。
- **W5 异常不再静默吞**：`_save_session` 写盘失败改为 `print("[session] 保存失败: …")` 留痕。
- **2.3 构成表差异刷新**：`_refresh_breakdown` 在部件集合不变时只更新数值单元格，
  不再每 2s 全表 `setRowCount(0)` 重建。

### P2 — 工程化
- 新增 `README.md`：模块职责图 + 打包流程 + 如何跑测试。

### 交付验证
- `tests/test_core.py` 12 个纯逻辑用例全部通过（含 `_find_key` 准确性 + `PowerEngine` 计量）。
- `main.py`/`power_model.py`/`power_core.py`/`test_headless.py` 均 `py_compile` 通过。

---

## v18.26 — 报告导出从 PDF 改为 PNG 图片

### 导出格式切换为 PNG
- 用户需求：把「PC用电汇总」导出从 PDF 改成 PNG 图片，便于直接贴图/分享。
- 把原有 `QTextDocument + QPrinter(PdfFormat)` 渲染链路替换为 `QTextDocument + QImage`：
  - 同一套 HTML 解析引擎，版式与旧 PDF 完全一致；
  - 渲染宽度从 860 提到 **960px**、2× 超采样（1920px 逻辑宽），图片更宽但不会纵向过长；
  - 收紧报告 CSS 间距（body/card/box padding、margin、字号、表格行高）并压扁 24 小时条形图，
    在保持可读性的前提下让整图高度明显下降（实测同一内容逻辑高度 1500px → 1203px，约 -20%）；
  - 移除 `QtPrintSupport`/`QPrinter`/`QPageLayout`/`QPageSize` 依赖，进一步降低打包负担。
- 导出按钮与汇总弹窗主按钮文案统一改为「保存报告(PNG)」，默认文件名 `PC用电汇总.png`。
- HTML 导出继续保留作为「可二次排版」的备用格式。
- 自动归档报告同样改为 PNG：`用电报告_{时间戳}.png`。
- `.gitignore` 同步增加 PNG 报告忽略规则，并清理已废弃的 PDF 导入。

## v18.25 — 悬浮窗盘温度占位符顺序修复

- 实机截图发现第二块盘显示「盘2°36」（`%s` 参数顺序写反）→ 改为「盘2 37°」。
- 交付：桌面 `PC用电电费计算器_v18.25.exe`（清理 v18.23/24 旧版）。

## v18.24 — 悬浮窗新增实时温度行

- 电费行下方右对齐小字（9px #9fb4d8）：`CPU 45° GPU 63° 盘 41° 盘2 37°`，
  数据取 `self._sys_dyn`（cpu_temp / gpu_temp / disk_temps，盘最多 2 块）；
  缺哪个隐藏哪个，全缺整行收起。
- `_apply_size` 计入温度行高度。
- 备注：本机 CPU 温度取不到（Ryzen 传感器链路无值）属设计内自动隐藏；GPU/盘温度正常。

## v18.23 — 历史拖动位置钳制回屏

- 功耗表格比旧文本高，会话恢复的旧位置可能把窗口底部顶出屏幕 →
  新增 `_clamp_into_screen()`（夹回 `availableGeometry`，不压任务栏），在 `_apply_size` 尾部调用；
  尺寸变化 / 会话恢复后一律钳制。

## v18.22 — 功耗结构升级为票据式两列小表

- `lbl_bd` 单标签长文本 → `bd_widget` + 行容器：每行 `名称(右对齐) + 瓦数(右对齐固定 52px 列)`，
  金色粗体「合计」行；行由 `set_breakdown()` 动态重建（`_bd_row_widgets` 管理、`deleteLater` 销毁）。
- `_apply_size` 按行控件 sizeHint 累加高度。

## v18.21 — 悬浮窗电费行金额被裁修复

- 症状：顶行左侧电费「本轮 1.911 kWh · ¥」后的金额被右侧配置列挤压裁掉。
- 修复组合：电费行移出顶行独占整行；字号 12→11px；kWh 3→2 位；
  去「本轮 」前缀；窗宽 224→240；≥100kWh 自适应降精度 `.1f/.0f`。
- 顺带清理孤儿 `lbl_cost`（未加入布局的残留控件）。

## v18.20 — 右键「恢复默认位置（屏幕右侧）」

- 默认定位只在无历史位置时生效，用户拖走后无法回位 → 右键菜单加 `act_home` → `place_right()`。

## v18.19 — 悬浮窗配置信息右上角 + 整体屏幕右侧 15% + 功耗结构右下角

- `MiniOverlay` 顶部改为左右两栏：左=实时功率/状态，右=配置信息
  （`lbl_cfg` 多行小字右对齐，空内容收起并重算尺寸）。
- 新增 `_cfg_compact(hw)`：硬件配置压缩成短型号——`i5-12400` / `GPU RTX 3060` / `内存 32G` / `2 屏`。
- 新增 `place_right()`：整个悬浮窗水平居中于屏幕右侧 15% 区带、垂直居中；
  仅无历史位置时使用（用户拖动后由会话记忆接管）。
- 功耗结构（右下角，全部项）随配置一并初始注入（`_mini_cfg_set` 一次性标志）；
  `_toggle_mini()` 与启动恢复路径在 `self._mini_pos` 为空时调用 `place_right()`。

---

## v18.18 — 产物命名「项目名+版本号」+ 悬浮窗构成改为全量显示

### 产物命名规范（用户要求）
- 打包产物自动改名为 **项目名+版本号**：`PC用电电费计算器_v18.18.exe`。
- `build_v1811.sh` / `build_exe.bat` 打包成功后从 `main.py` 提取
  `APP_VERSION` 自动改名；bat 还会在部署桌面时先清掉旧版本 exe。
- `kill_instances.py` 升级为**前缀匹配**（tasklist 枚举 + taskkill /PID 也可，
  此处用前缀枚举镜像名），带版本号的 exe 也能被关进程脚本杀到。

### 悬浮窗功耗构成：全面显示（用户要求）
- v18.17 只取前 4 项、其余归「其他」；v18.18 起 **所有构成项全部列出**，
  按功耗降序，自动换行，例如：
  `GPU 83 · CPU 45 · 显示器 30 · 主板 18 · HDD 8 · 内存 6 · SSD 3 W`。
- 悬浮窗宽度固定 224，**高度随构成内容自动伸缩**：用
  `QFontMetrics.boundingRect(…, TextWordWrap, …)` 精确计算换行后的行高
  （`QLabel.heightForWidth` 在部分环境返回 -1/失真，不可靠）。
- 右键菜单文案改为「显示功耗构成（全部项）」，开关仍然持久化。

---

## v18.17 — 悬浮窗新增功耗构成 + 主界面收窄（1983px → 944px）

### 悬浮窗第四行：功耗构成
- 在「本轮 kWh · ¥」下方新增一行小字，按功耗降序取前 4 项、其余归并「其他」：
  例 `GPU 83 · CPU 45 · 显示器 30 · 主板 18 · 其他 15 W`。
- 右键菜单新增「显示功耗构成」开关（可勾选），选择写入会话持久。
- 显示时窗口 224×128，隐藏时 224×104，控件随开关收放。
- 导出 PDF 报告的「迷你悬浮窗快照」同步记录第四行内容。

### 主界面收窄（用户反馈"主界面太宽"）
- 探针实测：`resize(1080)` 形同虚设，窗口被内容最小宽度顶到 **1983px**。
- 逐层定位并修复：
  1. **实时读数卡片 1572px**：① 详解行（"直流 X + 损耗 Y = 插座 Z（效率…）"）夹在
     第一列里撑宽整卡 —— 挪出独占一整行；② 7 个指标一行排布 —— 改 **4 列 × 2 行网格**；
     ③ 大号数字 34px → 28px；④ 全部标签改可压缩（`Ignored` 水平策略 + `setMinimumWidth(0)`，
     真实数据比占位文本宽，不改这条填数后又会被顶回 1232px）。
  2. **控制条 1020px**：9 个按钮挤一行 → 两行网格（每行 4 个）。
  3. **图表 860px**：`QChartView` 自带较大最小尺寸 → `setMinimumWidth(0)` + 高度 120。
  4. **功耗构成表格卡片**：表格与提示文字同样改可压缩。
  5. **标题行 662px**：副标题缩短 + 允许被压缩。
  6. 系统信息面板 375 → 288px。
- 结果：最小宽度 **1983 → 944px（-52%）**，默认窗口 1080×880，`resize()` 真正生效。
- 防回归断言：最小宽度 ≤ 1000px、实际宽度 ≤ 1100px，超限测试直接失败。

---

## v18.16 — 报告直接导出 PDF + 修「悬浮窗不显示」+ 修「设置显示不全」

### 用电报告直接输出 PDF
- 「导出报告」按钮改为直接导出 **PDF**（不再只给 HTML）：
  `QTextDocument` + `QPrinter(PdfFormat)` 渲染已有 HTML，纯 Qt 实现，
  不需要 reportlab / wkhtmltopdf，也不像 QWebEngineView 那样让 exe 膨胀 100MB+。
- 汇总弹窗底部改为 **保存报告(PDF) / 保存报告(HTML) / 关闭** 三个按钮。
- 24 小时周期到点自动归档的报告也从 HTML 改为 **PDF**。
- 踩坑：Qt6 已移除 `QPrinter.PageSize`，页面尺寸要用
  `QPageSize(QPageSize.PageSizeId.A4)`，否则抛 AttributeError、PDF 生成不出来。

### 报告里新增「迷你悬浮窗快照」卡片
- 导出瞬间把悬浮窗显示的三行原样写进报告：瞬时功率 / 监测状态 / 本轮累计，
  另附插座与直流功耗、电源效率、显示器功耗与累计节省、窗口位置与显示方式。

### 修「最新版悬浮窗在桌面上不显示」（v18.13 引入的严重回归）
- **现象**：改完之后悬浮窗彻底看不见。
- **根因**：v18.13 用 `SetParent` 把窗口挂到桌面（`Progman → SHELLDLL_DefView`），
  窗口身份就从「顶层窗口」变成「**子窗口**」；而背景透明依赖 `WS_EX_LAYERED`，
  **分层子窗口在 Windows 上不渲染** —— 于是整个悬浮窗被吞掉。
- **修法**：不再动窗口父子关系。改为保持顶层窗口，用
  `WindowStaysOnBottomHint` 实现「贴在桌面上、不遮挡任何窗口」。
- **新增右键菜单**（选择写入会话，重启保持）：
  - 嵌入桌面（默认，置底，不遮挡任何程序，Win+D 也看得到）
  - 始终置顶（任何窗口之上都可见）
  - 隐藏悬浮窗
- 实测确认：`可见=True`、243×129、`WS_CHILD=False`、`WS_EX_LAYERED=True`、`TOPMOST=False`。

### 修「设置面板内容显示不全」（第二次修，这次量到了确切数字）
- 探针实测：设置内容最小宽度 **525px**，而抽屉可视区只有 **470px** —— 右侧被裁。
- 三处成因 + 对应修法：
  1. **小标题占着标签列**（`addRow(QLabel(标题), QLabel(""))`）：标题文本
     「阶梯电价（居民月用量分档）」等把标签列撑宽。改为 `addRow(QLabel(标题))`
     单参数形式 —— **横跨两列**，不再参与标签列宽度计算（改了 11 处）。
  2. **长标签**：「月度电量预算(kWh, 0=不设)」单项就有 228px。缩短 12 处，
     单位挪到字段的 suffix（电量预算 kWh / 电费预算 ¥ / CPU 负载≤ / 告警阈值 …）。
  3. **字段增长策略**：`AllNonFixedFieldsGrow` 会把每个字段都撑到 sizeHint，
     直接抬高表单最小宽度。改为 `ExpandingFieldsGrow`。
- 抽屉宽度 480 → 520。结果：**内容最小宽度 525px → 417px，可视区 510px，无溢出**。
- 已加防回归断言（`settings dock no-clip OK`），以后再撑破会直接测试失败。

### 打包脚本：关进程 + 删旧版本（用户要求固化）
- 新增 `kill_instances.py`：打包前 `taskkill /F` 掉所有实例
  （否则 exe 被占用，覆盖失败却不报错，最后启动的还是旧版）。
  注意 cmd 输出是 **GBK**，`subprocess` 按 utf-8 解会崩，需 `encoding="gbk"`。
- `build_v1811.sh` / `build_exe.bat` 改为：
  关进程 → 暂存旧 exe 为 `_staging.exe` → 打包 →
  **成功则删光所有旧版本备份**（`_old_*` / `_locked_archive` / `dist` / `debug` / `_old_build_*`），
  **失败则还原旧 exe**，不留残局。
- 磁盘：`release/` 从 519MB 降到 **47.4MB**（只剩最新一个 exe）。

### 其他
- 部署时把电源额定功率 **550W** 写入会话，动态效率曲线正式启用。
- 排查中发现机器上有 **11 个实例在跑（多为 0 线程的僵尸进程）**，已全部清理。

---

## v18.13 — 全屏豁免 + 一键填额定功率 + 修「重启被自己拦在门外」

### 电源额定功率「按推荐填入」
- 动态效率曲线的开关是「电源额定功率」，但多数人说不清电源铭牌，
  留空（0）就一直走固定 0.85，新功能等于没启用。
- 设置面板额定功率行增加 **「按推荐填入」** 按钮：按「主机峰值 ÷ 0.88 + 30% 余量」
  估算后取最接近的常见档位（`PSU_COMMON`，300/350/…/1000），
  本机（5600X + GTX 1080，主机峰值 373W）算得 **550W**。
- 估算值只用于取档位，**不参与任何功耗计算**，用户随时可改成真实铭牌值。

### 修「45 秒内重启无法启动」（v18.11 心跳接管的副作用，实测复现）
- **现象**：停掉旧实例后立刻启动新版 → 进程起来了（21 线程 / 67MB），
  但心跳一直不更新、不采样、不落盘。原因是旧实例 28 秒前刚写过心跳，
  新实例见「心跳新鲜（< 45s）」就认定还有活实例在跑，弹出「已在运行」后卡住。
- **根因**：**心跳新鲜 ≠ 持有者还活着**。时间戳只能说明「最近有人写过」，
  说明不了「写它的那个进程还在」。
- **修法（两道）**：
  1. 心跳内容从 `时间戳` 改为 `时间戳 PID`（`_beat()` / `_beat_pid()`），
     接管判定升级为「心跳新鲜 **且** 该 PID 仍存活」（新增 `_pid_alive()`，
     `OpenProcess` + `GetExitCodeProcess`，259 = STILL_ACTIVE）。
     旧格式（无 PID）兼容放行。
  2. 「已在运行」提示框改为 **8 秒后自动关闭并退出**（原为静态模态框，
     熄屏时无人点确定就会永远挂着，表现为「进程在但不工作」）。
- 回归新增 `restart-within-45s OK`（持有者存活→拒绝 / 已死→接管 / 旧格式→放行 / PID 存活判定）。

- **问题**：v18.11 起靠「空闲时长 ≥ 系统息屏超时」推断熄屏。但看电影 / 视频会议 / PPT 放映时，
  鼠标键盘长时间无输入，屏幕其实亮着 —— 会被误判为熄屏，整场电影都少算显示器那一档（30W）。
- **修法**：`hardware.foreground_fullscreen()` 探测**真全屏**窗口（ctypes）：
  - 判定为真全屏 = 窗口铺满所在显示器 **且**（无标题栏 **或** 置顶）。
  - 仅最大化（仍带标题栏）**不算** —— 人可能只是开着窗口走开了。
  - 多屏环境按 `MonitorFromWindow` + `GetMonitorInfo` 取窗口所在那块屏，不是主屏。
  - 探测失败一律返回 False（宁可退回空闲判断，不改变既有行为）。
- `display_auto_off()` 增加豁免：有真全屏窗口时直接返回 False（屏幕亮着）。
- 手动标记仍然优先于全屏推断（`_display_manual` 三态 > 自动）。
- 实机验证：造一个 2560×1440 置顶无边框窗口 → `foreground_fullscreen()=True`、
  `display_auto_off()=False`；销毁后 → `False` / `True`。
- 回归新增 `fullscreen exemption OK`（全屏→亮屏、久空闲→熄屏、手动优先）。

## v18.12 — 动态电源效率曲线 + 硬件重检测

起因：用户提出「每增加或者减少驱动应该会影响效率」+「显示器关闭时驱动会有提示」。

### 显示器关闭时驱动能否提示 —— 结论：不能
- `nvidia-smi --query-gpu=display_attached,display_active`：息屏期间仍是 `Yes / Enabled`。
  驱动只知道线缆是否接上、显示是否初始化，**不掌握 DPMS 息屏状态**。
- `GetDevicePowerState("\\.\DISPLAY1" / "\\.\LCD")`：本机 CreateFile 阶段即失败，不可用。
- Windows 无公开的显示器开关查询 API → 只能靠空闲推断 + 手动覆盖。

### 动态电源效率曲线
- `power_model.PSU_EFF_CURVE`（80 PLUS 金牌典型曲线）+ `psu_eff_at(load_w, rating_w)`：
  5%→0.70 / 10%→0.82 / 20%→0.87 / 30%→0.895 / 50%→0.90 / 75%→0.88 / 100%→0.85，线性插值，超额定截断。
- `estimate()` 用 `psu_eff_at(calib_host, psu_rating_w)`；`rating <= 0` 返回 None → 回落固定效率，
  **未设置时行为与旧版完全一致**。按校准后的主机 DC 负载取点，物理上更准。
- 返回值新增 `psu_eff`；`on_sample()` 的 `self.cur` 同步带上该字段（初版漏了，UI 会一直读到 None）。
- UI：设置面板新增「电源额定功率」（0–2000W，0 = 不启用）；读数区显示实时效率 + 「（动态）」角标。
- 电源建议改为按**主机峰值**计算（剔除显示器，因其走市电）+ 峰值点效率。

### 硬件重检测（对应「增减驱动」的另一半）
- 模型原先只在启动时 `build_model(detect_hardware())` 构建一次，运行中插拔硬盘 / 显示器估算值不变。
- 系统信息面板顶部新增「⟳ 重新检测硬件」按钮 → `_redetect_hardware()`：
  重跑检测 + 重建模型，**继承** psu_efficiency / psu_rating_w / calib_*（不会冲掉校准），
  刷新面板并弹窗列出变化部件与静态 / 峰值功耗差值；无变化则提示未检测到变化。

### 测试隔离（重要修复）
- `test_headless.py` 原先开头直接 `os.remove(session.json / history.json)` 再写回测试假值，
  会毁掉所在目录的真实累计数据。现改为把 `BASE_DIR / SESSION_FILE / HISTORY_FILE / LOCK_FILE`
  全部重定向到 `tempfile.mkdtemp()`，atexit 时 rmtree。已验证跑测试前后真实文件 mtime 不变。

## v18.11 — 显示器开关状态接入功耗模型 + 悬浮窗半透明

修「关屏后累计电量还在涨」：
1. 正常部分 —— 关屏只切断显示器，主机照常耗电，累计电量本就该涨。
2. Bug 部分 —— 显示器 30W 被写成恒定静态项，关屏后估算一点没降（按 226W 计费，实为 190W）。

- 显示器状态检测：无可靠公开 API（`GetDevicePowerState` 实测失败）→
  `display_auto_off()` 用空闲时长 ≥ `powercfg VIDEOIDLE` 超时（本机 300s）+ 3s 余量推断；
  托盘菜单三态手动覆盖（`_disp_manual`：自动 / 记为开启 / 记为关闭）。
- 模型改造：`PowerModel.monitor_w`；`calibrated()` 只接收主机功耗；显示器**既不参与校准映射，
  也不参与电源效率折算**。三种校准模式下开关差值恒等于 30.0W（初版曾因显示器被算两次得到 65.3W / 39W）。
- 口径变化：开屏插墙 225.8 → **220.5W**，关屏 **190.5W**。
- UI：读数区尾部追加「显示器已关（省 30W，累计省 x.xxx 度）」，`_disp_saved_wh` 持久化。
- 悬浮窗半透明：设置抽屉背景 `rgba(255,255,255,216)`；**必须同步把 `QWidget#settingsPanel`
  改为 transparent**，否则子控件实心盖掉效果；MiniOverlay alpha 228 → 196。

### v18.11 修订 — 熄屏标记自动恢复 + 设置面板下拉
- 手动标记「关闭」后若忘了切回会长期低估 → `DISP_WAKE_IDLE_SEC = 20s`：
  手动标记 False 时若 `user_idle_sec() < 20` 说明人已回来，自动恢复按开屏计。
- 设置面板新增「显示器状态」三态 QComboBox。
- 部署坑：桌面 exe 被零线程僵尸持有文件映射，`Copy-Item -Force` 失败却**不终止脚本**，
  导致启动的仍是旧版 → 改为**先 `Rename-Item` 隔离再 Copy**。

### v18.11 修订2 — 单实例心跳接管 + 周期落盘
- 严重故障：程序能启动但完全不采样、不写 session、无报错。
  真根因是早期 `timeout 25` 只杀了 bootloader 父进程，**子进程成孤儿并持续持有单实例互斥体**，
  之后变僵尸（杀不掉）却仍占着锁 → 每次启动都命中 `ERROR_ALREADY_EXISTS` 并弹「已在运行」阻塞。
- 解法：互斥体 + **心跳文件**双重判定（`app.lock`，20s 一跳，新鲜阈值 45s），
  僵尸持锁时新实例直接接管，`_beat()` 抢占所有权。
- 采样每 30 拍（约 105s）自动落盘一次，避免强杀 / 崩溃丢掉全部累计量。

## v18.10（未单独交付，随 v18.11 一并发布）

- 设置浮层加标题栏 ✕ 按钮 + Esc 收起。
- 设置表单新增「迷你悬浮窗」复选框，保存时联动 `_toggle_mini`。
- 托盘菜单新增「生成昨日日报」。
