# -*- coding: utf-8 -*-
"""独立小工具：从「PC 用电电费计算器」exe 中提取内嵌的源码，输出到文件夹。

设计：
- 不依赖 PySide6 / tkinter / 完整 PyInstaller，仅用标准库（struct / zlib / ctypes），
  因此打出来的 exe 体积极小（几 MB），与 49MB 的主程序完全独立。
- 自带一个极简 CArchive(PKG) 读取器，与 PyInstaller 的归档格式一致（见
  PyInstaller/archive/readers.py）：cookie 在文件尾部，TOC 记录每个条目的偏移/长度/
  压缩标志，数据段按需 zlib 解压。只解析并提取名为 src/... 的条目。

用法：
    extract_source.exe                   # 自动在同目录查找主程序 exe 并提取到 <主名>_源码
    extract_source.exe <主程序exe路径> [输出目录]   # 命令行指定
支持把主程序 exe 拖到本工具上（拖放会作为第一个参数传入）。
"""
import os
import sys
import struct
import zlib
import ctypes

# ---------------- 极简 PyInstaller CArchive(PKG) 读取器 ----------------
_COOKIE_MAGIC = b'MEI\x0c\x0b\x0a\x0b\x0e'
_COOKIE_FORMAT = '!8sIIII64s'
_COOKIE_LEN = struct.calcsize(_COOKIE_FORMAT)
_TOC_ENTRY_FORMAT = '!IIIIBc'
_TOC_ENTRY_LEN = struct.calcsize(_TOC_ENTRY_FORMAT)


def _find_magic(fp, magic):
    """从文件尾部向前扫描定位 cookie 魔数（与 PyInstaller 行为一致）。"""
    fp.seek(0, 2)
    end = fp.tell()
    chunk = 8192
    while end >= len(magic):
        start = max(end - chunk, 0)
        fp.seek(start, 0)
        buf = fp.read(end - start)
        p = buf.rfind(magic)
        if p != -1:
            return start + p
        end = start + len(magic) - 1
    return -1


class MiniCArchive:
    def __init__(self, filename):
        self.filename = filename
        with open(filename, "rb") as fp:
            cstart = _find_magic(fp, _COOKIE_MAGIC)
            if cstart == -1:
                raise RuntimeError("未找到 PyInstaller 归档（cookie 魔数缺失）")
            fp.seek(cstart, 0)
            cookie = fp.read(_COOKIE_LEN)
            _magic, pkg_len, toc_offset, toc_len, _pyvers, _pylib = struct.unpack(
                _COOKIE_FORMAT, cookie)
            end_off = cstart + _COOKIE_LEN
            start_off = end_off - pkg_len
            fp.seek(start_off + toc_offset, 0)
            toc = fp.read(toc_len)
        self.start_off = start_off
        self.toc = {}
        self._parse(toc)

    def _parse(self, data):
        pos = 0
        n = len(data)
        while pos < n:
            entry_len, off, length, _u_len, cflag, tcode = struct.unpack(
                _TOC_ENTRY_FORMAT, data[pos:pos + _TOC_ENTRY_LEN])
            pos += _TOC_ENTRY_LEN
            name_len = entry_len - _TOC_ENTRY_LEN
            name = data[pos:pos + name_len].rstrip(b'\0').decode('utf-8')
            pos += name_len
            self.toc[name] = (off, length, cflag, tcode.decode('ascii'))

    def names(self):
        return list(self.toc.keys())

    def extract(self, name):
        off, length, cflag, _tcode = self.toc[name]
        with open(self.filename, "rb") as fp:
            fp.seek(self.start_off + off, 0)
            data = fp.read(length)
        if cflag:
            data = zlib.decompress(data)
        return data


# ---------------- 提取逻辑 ----------------
MB_OK = 0x0
ICON_INFO = 0x40
ICON_ERR = 0x10

# 默认弹窗；命令行带 --silent 时改为打印（便于无界面/自动化测试）
USE_GUI = True


def _msg(title, text, icon=ICON_INFO):
    if not USE_GUI:
        print(f"[{title}] {text}")
        return
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, icon | MB_OK)
    except Exception:
        # 控制台 / 无窗口站环境兜底
        print(f"[{title}] {text}")


def _find_main_exe():
    here = os.path.dirname(os.path.abspath(
        sys.executable if getattr(sys, "frozen", False) else __file__))
    base = os.path.abspath(sys.executable)
    for fn in os.listdir(here):
        if fn.startswith("PC用电电费计算器") and fn.lower().endswith(".exe"):
            full = os.path.join(here, fn)
            if os.path.abspath(full) != base:
                return full
    return None


def _split_rel(name):
    """src/foo/bar.py 或 src\\foo\\bar.py -> ('foo', 'bar.py') 这样的相对段列表。"""
    key = name.lower()
    if key.startswith("src/"):
        rel = name[4:]
    elif key.startswith("src\\"):
        rel = name[4:]
    else:
        return None
    sep = '/' if '/' in rel else '\\'
    return [p for p in rel.split(sep) if p]


def do_extract(exe_path, out_dir):
    try:
        arch = MiniCArchive(exe_path)
    except Exception as e:
        _msg("错误", f"无法读取目标 exe：\n{e}", ICON_ERR)
        return False
    src_names = [n for n in arch.names()
                 if n.lower().startswith("src/") or n.lower().startswith("src\\")]
    if not src_names:
        _msg("提示", "该 exe 中未找到内嵌源码（src/）。\n请确认这是 v18.29 及以上版本。",
             ICON_ERR)
        return False
    os.makedirs(out_dir, exist_ok=True)
    ok = 0
    for n in src_names:
        parts = _split_rel(n)
        if not parts:
            continue
        dst = os.path.join(out_dir, *parts)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            data = arch.extract(n)
        except Exception as e:
            _msg("错误", f"提取 {n} 失败：\n{e}", ICON_ERR)
            return False
        with open(dst, "wb") as f:
            f.write(data)
        ok += 1
    _msg("提取完成",
         f"已从 {os.path.basename(exe_path)} 提取 {ok} 个源文件到：\n{out_dir}")
    return True


def main():
    global USE_GUI
    args = sys.argv[1:]
    if "--silent" in args:
        USE_GUI = False
        args = [a for a in args if a != "--silent"]
    exe = args[0] if (args and os.path.isfile(args[0])) else _find_main_exe()
    if not exe:
        _msg("未找到主程序",
             "请把本工具放在「PC 用电电费计算器」exe 同目录，\n"
             "或在命令行 / 拖放指定主程序 exe 路径。", ICON_ERR)
        return
    if len(args) >= 2:
        out = args[1]
    else:
        base = os.path.splitext(os.path.basename(exe))[0]
        out = os.path.join(os.path.dirname(os.path.abspath(exe)), base + "_源码")
    do_extract(exe, out)


if __name__ == "__main__":
    main()
