"""v18.48 按需 LHM 验证（无需 GUI 交互）：

1. sample_dynamic() 在 LHM 未运行时**不应**自动拉起 LHM（idle 不常驻）。
2. ensure_lhm() 能拉起 LHM；is_lhm_running() 变 True。
3. stop_lhm() 能真正终止 LHM 进程；is_lhm_running() 回到 False。
4. 顺带报告 App 自身内存若以此 python 运行（仅参考，真实以 EXE 为准）。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hardware as H


def main():
    print("=== [0] 清理可能存在的残留 LHM（每次构建/测试前确保干净）===")
    H.stop_lhm()
    time.sleep(1.5)
    print("  清理后 is_lhm_running =", H.is_lhm_running())
    assert H.is_lhm_running() is False, "FAIL: 清理后 LHM 仍在运行"
    print("  OK: 已从干净状态开始")

    print("=== [1] idle 不自动拉起 LHM ===")
    H.sample_dynamic(None)
    print("  sample_dynamic 后 is_lhm_running =", H.is_lhm_running())
    assert H.is_lhm_running() is False, "FAIL: sample_dynamic 不应拉起 LHM"
    print("  OK: 空闲不拉起 LHM")

    print("=== [1b] 风扇读取路径（gpu_fan_rpms / fan_kinds_cached）不得拉起 LHM ===")
    # v18.48 回归点：构成表温度列 _gpu_fan → gpu_fan_rpms → sensor_snapshot_cached，
    # 旧实现会经 sensor_snapshot_cached 自动拉起 LHM，导致空闲常驻突破 100MB。
    H.gpu_fan_rpms()
    H.fan_kinds_cached()
    print("  gpu_fan_rpms/fan_kinds_cached 后 is_lhm_running =", H.is_lhm_running())
    assert H.is_lhm_running() is False, "FAIL: 风扇读取路径不应拉起 LHM"
    print("  OK: 风扇读取路径空闲不拉起 LHM")

    print("=== [1c] lhm_sensors(force=False) 不应拉起 LHM（面板关闭后空闲）===")
    H.lhm_sensors(force=False)
    print("  lhm_sensors(force=False) 后 is_lhm_running =", H.is_lhm_running())
    assert H.is_lhm_running() is False, "FAIL: lhm_sensors(force=False) 不应拉起 LHM"
    print("  OK: 非强制取数不拉起 LHM")

    print("=== [2] ensure_lhm 拉起 ===")
    ok = H.ensure_lhm(wait=20.0)
    print("  ensure_lhm 返回 =", ok, " is_lhm_running =", H.is_lhm_running())
    assert ok is True, "FAIL: ensure_lhm 未能拉起 LHM"
    assert H.is_lhm_running() is True, "FAIL: 拉起后 is_lhm_running 仍 False"
    print("  OK: LHM 已拉起且 Web Server 就绪")

    print("=== [3] stop_lhm 终止 ===")
    killed = H.stop_lhm()
    time.sleep(1.5)
    print("  stop_lhm 返回 =", killed, " is_lhm_running =", H.is_lhm_running())
    assert H.is_lhm_running() is False, "FAIL: stop_lhm 未能终止 LHM"
    print("  OK: LHM 已终止")

    print("ALL_ON_DEMAND_CHECKS_PASSED")


if __name__ == "__main__":
    main()
