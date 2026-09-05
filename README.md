# PC 电脑用电电费计算器

实时监测台式机功耗、按电价计费、生成日报 / 报告（PNG / HTML）的完全离线桌面工具。
技术栈：Python 3.13 + PySide6 + QtCharts；PyInstaller onefile 打包（单 exe，无外部依赖）。

---

## 模块职责

```
hardware.py        硬件探测（subprocess 调 PowerShell / nvidia-smi），隔离 I/O
power_model.py     纯函数 + 数据类：TDP 查表 / 负载插值 / 电源效率曲线 / 校准。零 Qt 依赖
power_core.py      PowerEngine：零 Qt 的核心计量引擎（估算 / 计费 / 累计 / 模拟）。
                   【绞杀者模式目标落点】后续把 MainWindow 的计费逻辑逐步迁到这里
main.py            MainWindow：UI + 控制器，负责界面、事件与调用引擎/模型。当前仍内含多数计费逻辑
kill_instances.py  单实例互斥体（互斥体 + 心跳 + PID 存活三重判定）
test_headless.py   需 Qt 的端到端冒烟测试（无界面拉起 MainWindow 跑一遍关键路径）
tests/test_core.py 纯逻辑单测（不依赖 Qt），覆盖 _find_key 与 PowerEngine
build_exe.bat      一键打包（Windows，GBK+CRLF 保存）
build_v1811.sh     等价打包脚本（Linux/WSL 验证用）
```

数据流向：`hardware.detect_hardware() → power_model.build_model() → (estimate / PowerEngine.tick) → MainWindow 累计 / 计费`

---

## 打包（产出单 exe 到桌面）

```bat
build_exe.bat        :: 自动关旧进程→暂存旧 exe→挪 build→PyInstaller→改名「项目名+版本号」→部署桌面
```

- 产物命名：`PC用电电费计算器_vX.YZ.exe`
- 构建用 Python：`C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe`
- 已 `--exclude-module` 剔除 QtWebEngine / 3D / Multimedia 等 40+ 模块，避免 exe 膨胀到 200MB+

---

## 跑测试

```bash
# 纯逻辑（无需 Qt，最快）—— 推荐日常回归
python tests/test_core.py
# 或 pytest
python -m pytest tests/test_core.py -q

# 端到端冒烟（需 Qt，离屏也可跑）
QT_QPA_PLATFORM=offscreen python test_headless.py
```

测试要点：
- `_find_key` 准确性：Ti / SUPER / XT 后缀不被短前缀截短；`Radeon Graphics` 核显不被误套到独显。
- `PowerEngine`：tick 累计电量、峰谷分段完备、阶梯电费单调、节能模拟给出正节省。

---

## 已知技术债（按 v18.26 评估报告）

| 项 | 说明 | 状态 |
|----|------|------|
| W2 上帝类 | MainWindow 仍含多数计费逻辑 | 已落 `power_core.PowerEngine`，待逐步委托 |
| W4 设置序列化 | 40 字段在两处手写，新增需改两处 | 待 `Settings` dataclass 统一 |
| 报告高度 | PNG 已收紧约 20%，仍偏长 | 可继续压（砍说明 / 峰值时段） |

---

## 主要能力

实时监测 / 校准 / 峰谷·阶梯计费 / 待机识别 / 迷你悬浮窗 / 托盘 / 开机自启 /
历史趋势 / 日报 / 方案对比 / 节能模拟 / 时段分布 / 软件耗电 / CSV·PNG·HTML 导出。
