"""
PyInstaller 冻结入口（network_test 自动化 CLI）。

说明与意图
-----------
- 正常开发时使用：`python -m network_test.automation.runner ...`
- 打包后由 PyInstaller 把本文件作为「分析起点」，从而拉取 `network_test`、`libs` 等依赖。
- 不把 `main()` 直接写在 spec 的 `Analysis([runner.py])` 里，是为了保持与 `-m` 运行
  时完全相同的导入图与行为，仅增加一层极薄的启动封装。

运行前提
--------
- `--config` 仍指向用户提供的 JSON 路径（可与 exe 同目录分发）。
- 默认配置里的 `report_dir` 等相对路径相对于**当前工作目录**，现场请在配置中写
  绝对路径或先 `cd` 到期望目录再运行 exe。
"""

from __future__ import annotations

from network_test.automation.runner import main

if __name__ == "__main__":
    raise SystemExit(main())
