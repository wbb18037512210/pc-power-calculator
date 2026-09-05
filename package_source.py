# -*- coding: utf-8 -*-
"""源码打包工具。

两种用法：
  1) 嵌入 EXE（构建用）：
       python package_source.py --bundle-dir _src_bundle
     把可重建工程所需的源文件按相对路径复制到 _src_bundle/，随后由
     PyInstaller --add-data "_src_bundle;src" 一起打进 onefile exe。
  2) 单独出源码压缩包（手动用）：
       python package_source.py                # 自动从 main.py 读版本，生成 release/<名>_源码_<ver>.zip
       python package_source.py --out DIR      # 指定输出目录
       python package_source.py --ver vX.YZ   # 手动指定版本

设计要点：
    - 顶层文件用白名单精确纳入，避免把 _probe_*.py / _diag_*.py 等调试脚本打包进去。
    - tests/ 目录整体递归纳入（剔除 __pycache__ / *.pyc）。
    - 顶层 *.spec / requirements*.txt 若存在也一并纳入。
    - release/ 等构建产物目录整体排除（里面是 exe 与历史归档，不进源码包）。
"""
import argparse
import os
import re
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
NAME = "PC用电电费计算器"
MAIN_SPEC = NAME + ".spec"
# 内嵌进 exe 的源码如果要「自包含复现构建」，主 spec 必须声明把 _src_bundle 映射成 src。
EMBED_DATAS = [("_src_bundle", "src")]

# 顶层文件白名单（精确）
INCLUDE_FILES = [
    "main.py", "hardware.py", "power_model.py", "power_core.py",
    "hardware_id_v2.py",
    "build_exe.bat", "kill_instances.py", "package_source.py",
    "README.md", "CHANGELOG.md", "test_headless.py", ".gitignore",
]
# 顶层目录白名单（递归纳入）
INCLUDE_DIRS = ["tests"]

EXCLUDE_DIRS = {
    "build", "dist", "release", "__pycache__", ".git",
    ".pytest_cache", ".workbuddy", "venv", "envs", "node_modules",
}
EXCLUDE_EXT = {".exe", ".pyc", ".pyo", ".log", ".obj", ".pdb", ".lock", ".bak"}


def parse_version():
    try:
        with open(os.path.join(ROOT, "main.py"), "r", encoding="utf-8") as f:
            for line in f:
                m = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return "unknown"


def excluded(rel_path):
    parts = rel_path.split(os.sep)
    if set(parts) & EXCLUDE_DIRS:
        return True
    base = parts[-1]
    ext = os.path.splitext(base)[1].lower()
    if ext in EXCLUDE_EXT:
        return True
    # 调试 / 诊断脚本（以下划线开头）一律排除
    if base.startswith(("_probe_", "_diag_", "_staging", "_MEI")):
        return True
    if base in ("session.json", "history.json", "app.lock", "build_pyinstaller.log"):
        return True
    return False


def collect_files():
    out = []
    for f in INCLUDE_FILES:
        fp = os.path.join(ROOT, f)
        if os.path.isfile(fp) and not excluded(f):
            out.append(f)
    for d in INCLUDE_DIRS:
        dp = os.path.join(ROOT, d)
        if not os.path.isdir(dp):
            continue
        for cur, dirs, files in os.walk(dp):
            dirs[:] = [x for x in dirs if x not in EXCLUDE_DIRS]
            for fn in files:
                rel = os.path.relpath(os.path.join(cur, fn), ROOT)
                if not excluded(rel):
                    out.append(rel.replace(os.sep, "/"))
    # 顶层可选构建文件
    for fn in os.listdir(ROOT):
        low = fn.lower()
        if (low.endswith(".spec") or (low.startswith("requirements") and low.endswith(".txt"))):
            if os.path.isfile(os.path.join(ROOT, fn)) and not excluded(fn):
                out.append(fn)
    return sorted(set(out))


def _ensure_spec_embed_datas(spec_path):
    """确保主 spec 的 datas 包含 ('_src_bundle','src')，使提取出的源码可直接复现内嵌构建。

    这样即便构建脚本改用 `pyinstaller PC用电电费计算器.spec`，也能保证源码被内嵌，
    不会因依赖命令行 --add-data 而丢失。
    """
    try:
        with open(spec_path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return
    if "('_src_bundle', 'src')" in text or '("_src_bundle", "src")' in text:
        return
    m = re.search(r"datas=\[(.*?)\]", text, re.DOTALL)
    if not m:
        return
    inner = m.group(1).strip()
    entry = "('_src_bundle', 'src')"
    new_inner = (inner + ", " + entry) if inner else entry
    text = text[:m.start()] + "datas=[%s]" % new_inner + text[m.end():]
    with open(spec_path, "w", encoding="utf-8") as f:
        f.write(text)


def bundle_into(dest_dir):
    """把收集到的源文件按相对路径复制到 dest_dir，供 PyInstaller --add-data 使用。"""
    if os.path.exists(dest_dir):
        shutil.rmtree(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)
    n = 0
    for rel in collect_files():
        sp = os.path.join(ROOT, rel)
        dp = os.path.join(dest_dir, rel)
        os.makedirs(os.path.dirname(dp), exist_ok=True)
        shutil.copy2(sp, dp)
        if rel == MAIN_SPEC:
            _ensure_spec_embed_datas(dp)
        n += 1
    return n


def make_zip(zip_path):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in collect_files():
            z.write(os.path.join(ROOT, rel), arcname=rel)
    return os.path.getsize(zip_path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Package source code")
    ap.add_argument("--ver", default=None, help="版本号，默认从 main.py 读取")
    ap.add_argument("--out", default=None, help="zip 输出目录，默认 release/")
    ap.add_argument("--bundle-dir", default=None,
                    help="把源码复制到该目录（用于 PyInstaller --add-data 嵌入 exe）")
    args = ap.parse_args(argv)

    if args.bundle_dir:
        n = bundle_into(args.bundle_dir)
        print(f"源码已打包到目录: {args.bundle_dir} ({n} 个文件)")
        return 0

    ver = args.ver or parse_version()
    files = collect_files()
    if not files:
        print("没有可打包的源文件", file=sys.stderr)
        return 1

    out_dir = args.out or os.path.join(ROOT, "release")
    os.makedirs(out_dir, exist_ok=True)
    zip_name = f"{NAME}_源码_{ver}.zip"
    zip_path = os.path.join(out_dir, zip_name)
    size = make_zip(zip_path)
    print(f"源码归档: {zip_path}")
    print(f"          大小 {size/1024:.1f} KB，{len(files)} 个文件")

    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    if os.path.isdir(desktop):
        try:
            shutil.copy2(zip_path, desktop)
            print(f"已复制到桌面: {desktop}/{zip_name}")
        except Exception as e:
            print("复制到桌面失败:", e, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
