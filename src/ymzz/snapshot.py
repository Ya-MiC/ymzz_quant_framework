"""把一次回測收成不依賴伺服器的 HTML。"""

from __future__ import annotations

import json
from pathlib import Path


def write_snapshot(path: Path, result: dict, root: Path) -> None:
    css = (root / "web" / "style.css").read_text(encoding="utf-8")
    script = (root / "web" / "app.js").read_text(encoding="utf-8")
    payload = json.dumps(result, ensure_ascii=False)
    html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8" />
  <title>晏銘湛箴回測快照</title>
  <style>{css}</style>
</head>
<body>
  <header class="top"><div><p class="brand">晏銘湛箴</p><h1>回測快照</h1></div></header>
  <main>
    <section class="panel results" style="grid-column: 1 / -1">
      <h2>結果</h2>
      <p id="status" class="status"></p>
      <div id="metrics" class="metrics"></div>
      <canvas id="price" width="920" height="320"></canvas>
      <canvas id="equity" width="920" height="180"></canvas>
      <div class="table-wrap"><table id="ledger"><thead><tr><th>時間</th><th>動作</th><th>價格</th><th>數量</th><th>費用</th><th>原因</th><th>權益</th></tr></thead><tbody></tbody></table></div>
      <p id="prov" class="note"></p>
    </section>
  </main>
  <script>window.YMZZ_SNAPSHOT = {payload};</script>
  <script>{script}</script>
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")
