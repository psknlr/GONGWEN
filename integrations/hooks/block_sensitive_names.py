#!/usr/bin/env python3
"""PreToolUse 钩子示例：文件名含密级或“内部”字样时，阻止经工具添加为材料（纵深防御）。

配置（.gongwen/config.toml）：
    [[hooks.PreToolUse]]
    matcher = "material_add"
    command = "python3 integrations/hooks/block_sensitive_names.py"

约定（与 grok-cli / Claude Code hooks 一致）：从标准输入读取 JSON 事件；
退出码 0 放行，2 阻断（标准错误作为理由）。
"""

import json
import re
import sys

event = json.load(sys.stdin)
path = str((event.get("args") or {}).get("path", ""))
if re.search(r"(绝密|机密|秘密|内部|工作秘密)", path):
    print(f"文件名“{path}”含密级或内部字样：须由人工经命令行申报属性后添加，不经模型通道添加", file=sys.stderr)
    sys.exit(2)
sys.exit(0)
