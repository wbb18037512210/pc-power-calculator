# -*- coding: utf-8 -*-
"""v18.37 补丁修正：
1) 上一版把 `cands` 加 "gpu" 打到了 _parse_sensors（WMI 回退）而非 _parse_lhm_json。
2) hardware.py 用了 os.path.exists 但从未 import os —— ensure_lhm 一触发即
   NameError（v18.36 遗留真实 bug，会让整个采样链路抛异常）。
"""
import io, os

ROOT = os.path.dirname(os.path.abspath(__file__))
p = os.path.join(ROOT, "hardware.py")
s = io.open(p, "r", encoding="utf-8").read()

# --- 1) 还原 _parse_sensors 的 cands（不该有 gpu） -------------------------
i_ps = s.index("def _parse_sensors(")
i_lhm = s.index("def _parse_lhm_json(")
seg = s[i_ps:i_lhm]
if '"gpu"' in seg:
    seg = seg.replace('cands = {"cpu": [], "memory": [], "motherboard": [], "gpu": []}',
                      'cands = {"cpu": [], "memory": [], "motherboard": []}', 1)
    s = s[:i_ps] + seg + s[i_lhm:]

# --- 2) 给 _parse_lhm_json 的 cands 加 gpu --------------------------------
i_ps = s.index("def _parse_sensors(")
i_lhm = s.index("def _parse_lhm_json(")
head, tail = s[:i_lhm], s[i_lhm:]
old = '    cands = {"cpu": [], "memory": [], "motherboard": []}'
new = '    cands = {"cpu": [], "memory": [], "motherboard": [], "gpu": []}'
assert old in tail, "lhm cands 未找到"
tail = tail.replace(old, new, 1)
s = head + tail

# --- 3) 补 import os -----------------------------------------------------
if "\nimport os\n" not in s:
    old_imp = "import json\nimport subprocess\n"
    new_imp = "import json\nimport os\nimport subprocess\n"
    assert old_imp in s, "import 段未找到"
    s = s.replace(old_imp, new_imp, 1)

io.open(p, "w", encoding="utf-8").write(s)

# --- 校验 ----------------------------------------------------------------
s2 = io.open(p, "r", encoding="utf-8").read()
i_ps = s2.index("def _parse_sensors(")
i_lhm = s2.index("def _parse_lhm_json(")
assert '"gpu"' not in s2[i_ps:i_lhm], "_parse_sensors 仍含 gpu"
assert 'cands = {"cpu": [], "memory": [], "motherboard": [], "gpu": []}' in s2[i_lhm:]
assert "\nimport os\n" in s2
print("fix OK")
