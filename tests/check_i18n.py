"""Check that every i18n key used in code exists in both language files.

Run with:  py\\Scripts\\python.exe tests\\check_i18n.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    used: set[str] = set()
    for rel in ("app/ui/main_window.py", "app/kvm.py", "app/poller.py", "main.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        used.update(re.findall(r'"#([A-Z0-9_]+)"', text))

    zh = json.loads((ROOT / "langs/zh_CN.json").read_text(encoding="utf-8"))
    en = json.loads((ROOT / "langs/en_US.json").read_text(encoding="utf-8"))
    zh_keys = {k.lstrip("#") for k in zh}
    en_keys = {k.lstrip("#") for k in en}

    missing_zh = sorted(used - zh_keys)
    missing_en = sorted(used - en_keys)
    mismatch = sorted(zh_keys ^ en_keys)

    print("used:", len(used), "zh:", len(zh_keys), "en:", len(en_keys))
    print("missing zh:", missing_zh)
    print("missing en:", missing_en)
    print("zh/en mismatch:", mismatch)

    ok = not missing_zh and not missing_en and not mismatch
    print("I18N CHECK:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
