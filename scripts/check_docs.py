#!/usr/bin/env python3
"""文档/代码一致性检查：抽取手册里嵌入的代码块并做语法校验。

《02-从零开始操作手册.md》用 heredoc 内嵌了 policy_plugin.py / control_plane.py /
launch_project.sh / watchdog.sh 的可执行代码。这些块一旦与仓库实现漂移、或被改出
语法错误，照手册抄的人就会踩坑。本脚本在 CI 与本地把它们抽出来：

  * Python 块 → py_compile
  * Bash 块   → bash -n

任一失败即非零退出。
"""
from __future__ import annotations

import py_compile
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANUAL = REPO / "02-从零开始操作手册.md"

# (起始 heredoc 标记, 结束标记, 语言)
BLOCKS = [
    ("<<'PLUGIN'\n", "\nPLUGIN", "py"),
    ("<<'CP'\n", "\nCP", "py"),
    ("<<'LAUNCHER'\n", "\nLAUNCHER", "sh"),
    ("<<'WD'\n", "\nWD", "sh"),
]


def extract(text: str, start: str, end: str) -> str:
    i = text.index(start) + len(start)
    j = text.index(end, i)
    return text[i:j]


def check_python(code: str) -> str | None:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(code)
        path = f.name
    try:
        py_compile.compile(path, doraise=True)
        return None
    except py_compile.PyCompileError as exc:
        return str(exc)


def check_bash(code: str) -> str | None:
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, encoding="utf-8") as f:
        f.write(code)
        path = f.name
    result = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
    return None if result.returncode == 0 else result.stderr.strip()


def main() -> int:
    text = MANUAL.read_text(encoding="utf-8")
    failures = []
    for start, end, lang in BLOCKS:
        label = start.strip().strip("<'")
        try:
            code = extract(text, start, end)
        except ValueError:
            failures.append(f"{label}: 未在手册中找到该代码块标记")
            continue
        err = check_python(code) if lang == "py" else check_bash(code)
        if err:
            failures.append(f"{label} ({lang}): {err}")
        else:
            print(f"OK  {label} ({lang})")

    if failures:
        print("\n文档代码块校验失败：", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("\n所有嵌入代码块校验通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
