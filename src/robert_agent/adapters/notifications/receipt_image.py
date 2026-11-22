"""Render separate PNGs with the same paper and canvas as the Web receipt."""

import base64
from html import escape
from importlib.resources import files
import json
from pathlib import Path
import re
import subprocess


EVENT_LABELS = {
    "pr_opened": "首次交付 PR",
    "report_delivered": "独立报告完成",
    "pr_updated": "PR 更新",
    "pr_reviewed": "PR 评审交付",
    "pr_merged": "PR 已合并",
}


def _receipt(receipt, icon):
    label = EVENT_LABELS[receipt["event"]]
    title = escape(receipt.get("title") or label)
    repository = escape(receipt["repository"])
    timestamp = escape(receipt["occurred_at"])
    summary = escape(receipt.get("summary", ""))
    url = escape(receipt.get("url") or "")
    return (
        '<section class="receipt-container"><article class="receipt-paper">'
        '<header class="receipt-header"><div class="receipt-brand">'
        f'<img src="data:image/svg+xml;base64,{icon}" width="28" height="28" alt="">Robert</div></header>'
        '<p class="receipt-kicker">Agent 收据</p>'
        f'<h2 class="receipt-title">{title}</h2>'
        f"<p>{label} · {repository}<br>{timestamp}</p>"
        + (f'<section class="receipt-section"><p class="receipt-summary">{summary}</p></section>' if summary else "")
        + (f'<section class="receipt-section"><a href="{url}">{url}</a></section>' if url else "")
        + "</article></section>"
    )


def render_pngs(receipts, directory, executable, *, runner=subprocess.run):
    directory = Path(directory)
    assets = files("robert_agent").joinpath("web_assets")
    icon = base64.b64encode(assets.joinpath("robert-favicon.svg").read_bytes()).decode("ascii")
    css = "\n".join(assets.joinpath(name).read_text(encoding="utf-8") for name in ("github-shell.css", "timeline.css"))
    image_script = assets.joinpath("receipt-image.js").read_text(encoding="utf-8")
    sections = "".join(_receipt(item, icon) for item in receipts)
    source = directory / "receipt.html"
    source.write_text(
        f"""<!doctype html><html lang="zh"><meta charset="utf-8">
<style>{css}
.receipt-container {{ width: 470px; max-height: none; }}
.receipt-paper {{ max-height: none; overflow: visible; }}
</style>
{sections}
<pre id="receipt-images"></pre>
<script>{image_script}
window.addEventListener("load", () => {{
  const images = Array.from(document.querySelectorAll(".receipt-container"), container =>
    renderReceiptCanvas(container).toDataURL("image/png").split(",")[1]
  );
  document.getElementById("receipt-images").textContent = JSON.stringify(images);
}});
</script>
</html>""",
        encoding="utf-8",
    )
    result = runner(
        [
            executable,
            "--headless",
            "--disable-background-networking",
            "--no-first-run",
            "--dump-dom",
            "--window-size=500,800",
            f"--user-data-dir={directory / 'profile'}",
            source.resolve().as_uri(),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    match = re.search(r'<pre id="receipt-images">(.*?)</pre>', result.stdout)
    if result.returncode or match is None or not match[1]:
        raise ValueError("Chromium did not render receipt images")
    encoded = json.loads(match[1])
    if not isinstance(encoded, list) or len(encoded) != len(receipts):
        raise ValueError("Receipt image count differs from the delivery count")
    paths = []
    for index, image in enumerate(encoded, start=1):
        png = base64.b64decode(image, validate=True)
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Chromium produced a non-PNG receipt")
        path = directory / f"robert-receipt-{index:02}.png"
        path.write_bytes(png)
        paths.append(path)
    return paths
