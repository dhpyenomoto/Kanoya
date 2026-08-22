"""社内閲覧用の静的サイトを生成する.

    pip install markdown
    python3 scripts/build_site.py

生成物は site/ に出力し、そのままリポジトリへコミットする。
**ホスティング側でビルドさせない**のが方針。ビルド工程が無ければ、
Vercel / GitHub Pages / Netlify / 社内ファイルサーバ / USBメモリ、
どこに置いても同じように開ける。ビルドが無いものは壊れない。

（Vercel が `_config.yml` を見て Jekyll と誤検出し、Ruby環境が無いまま
`jekyll build` を実行して失敗した、というのが本スクリプトを用意した経緯。）

日本語ファイル名はURLでパーセントエンコードされて読みにくいため、
出力側ではASCIIのスラッグへ変換し、文書間のリンクも書き換える。
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

try:
    import markdown
except ModuleNotFoundError:
    print("markdown が必要です:  pip install markdown", file=sys.stderr)
    raise SystemExit(1)

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "site"

# 元ファイル → (出力名, ナビ表示名, ナビに載せるか)
PAGES: list[tuple[str, str, str, bool]] = [
    ("README.md",                        "index.html",          "概要",                 True),
    ("docs/01_企画提案書.md",              "01-proposal.html",    "1. 企画提案書",         True),
    ("docs/02_データ収集設計.md",          "02-data-collection.html", "2. データ収集設計", True),
    ("docs/03_システムアーキテクチャ.md",   "03-architecture.html", "3. システム設計",      True),
    ("docs/04_導入ロードマップとROI.md",    "04-roadmap-roi.html", "4. ロードマップとROI",  True),
    ("docs/05_収集パイプライン運用手順.md", "05-operations.html",  "5. 運用手順",           True),
    ("docs/sample-adr-matrix.md",        "adr-matrix-table.html", "ADRマトリクス（表）",  True),
    ("engine/README.md",                 "engine.html",         "参照実装の使い方",       True),
]

# そのままコピーするファイル（すでにHTML）
COPIES: list[tuple[str, str, str]] = [
    ("docs/sample-adr-matrix.html", "adr-matrix.html", "ADRマトリクス（ヒートマップ）"),
]

CSS = """
:root{--ground:#F5F6F2;--surface:#FFFFFF;--alt:#EDEFE9;--ink:#1A211C;--ink2:#414B44;
 --muted:#6B746D;--line:#D8DCD3;--accent:#14584A;--accent-soft:#E2EBE6;--warn:#B8452B}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
 --ground:#141715;--surface:#1C201D;--alt:#232823;--ink:#E8EBE6;--ink2:#C0C7BF;
 --muted:#949C94;--line:#2E342E;--accent:#4FB49A;--accent-soft:#1E2A26;--warn:#DD6A46}}
:root[data-theme="dark"]{--ground:#141715;--surface:#1C201D;--alt:#232823;--ink:#E8EBE6;
 --ink2:#C0C7BF;--muted:#949C94;--line:#2E342E;--accent:#4FB49A;--accent-soft:#1E2A26;--warn:#DD6A46}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font-size:16px;line-height:1.85;
 font-family:"Hiragino Sans","Yu Gothic","Noto Sans JP",system-ui,sans-serif}
.wrap{display:flex;align-items:flex-start;max-width:1180px;margin:0 auto;gap:34px;
 padding:0 22px 80px}
nav{position:sticky;top:0;flex:0 0 232px;padding:30px 0;max-height:100vh;overflow:auto}
nav .brand{font-family:"Hiragino Mincho ProN","Yu Mincho","Noto Serif JP",serif;
 font-size:17px;font-weight:600;line-height:1.5;margin-bottom:18px;display:block;
 color:var(--ink);text-decoration:none}
nav a.item{display:block;padding:7px 11px;border-radius:3px;color:var(--ink2);
 text-decoration:none;font-size:13.5px;border-left:2px solid transparent}
nav a.item:hover{background:var(--alt);color:var(--ink)}
nav a.item.on{background:var(--accent-soft);color:var(--accent);font-weight:700;
 border-left-color:var(--accent)}
main{flex:1;min-width:0;padding:30px 0}
h1,h2,h3,h4{font-family:"Hiragino Mincho ProN","Yu Mincho","Noto Serif JP",serif;
 font-weight:600;line-height:1.45;text-wrap:balance}
h1{font-size:29px;margin:0 0 22px;padding-bottom:14px;border-bottom:2px solid var(--accent)}
h2{font-size:22px;margin:44px 0 14px;padding-top:10px;border-top:1px solid var(--line)}
h3{font-size:17px;margin:30px 0 10px;color:var(--accent)}
h4{font-size:15px;margin:22px 0 8px}
p,li{max-width:74ch}
a{color:var(--accent);text-underline-offset:3px}
a:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:2px}
code{background:var(--alt);padding:2px 5px;border-radius:3px;font-size:.88em;
 font-family:ui-monospace,"SF Mono",Menlo,monospace}
pre{background:var(--alt);border:1px solid var(--line);padding:15px 17px;overflow-x:auto;
 border-radius:3px;line-height:1.7}
pre code{background:none;padding:0;font-size:12.5px}
blockquote{margin:18px 0;padding:14px 18px;background:var(--accent-soft);
 border-left:3px solid var(--accent);color:var(--ink2)}
blockquote p{margin:6px 0}
.tablewrap{overflow-x:auto;border:1px solid var(--line);background:var(--surface);margin:18px 0}
table{border-collapse:collapse;width:100%;min-width:460px;font-size:14.5px}
th,td{text-align:left;padding:10px 14px;border-bottom:1px solid var(--line);vertical-align:top}
thead th{background:var(--alt);font-size:12px;letter-spacing:.06em;color:var(--ink2);
 font-weight:600;white-space:nowrap}
tbody tr:last-child td{border-bottom:none}
hr{border:none;border-top:1px solid var(--line);margin:34px 0}
footer{margin-top:56px;padding-top:22px;border-top:1px solid var(--line);
 font-size:13px;color:var(--muted)}
@media (max-width:860px){.wrap{flex-direction:column;gap:0}
 nav{position:static;flex:none;width:100%;max-height:none;
  border-bottom:1px solid var(--line);padding-bottom:14px}
 nav .items{display:flex;flex-wrap:wrap;gap:4px}
 nav a.item{border-left:none;font-size:12.5px;padding:5px 9px}}
"""


def build_link_map() -> dict[str, str]:
    """元のリンク文字列 → 出力ファイル名 の対応表."""
    mapping: dict[str, str] = {}
    for src, out, _label, _nav in PAGES:
        mapping[src] = out
        mapping[Path(src).name] = out          # docs内からの相対リンク
        mapping[f"../{src}"] = out
    for src, out, _label in COPIES:
        mapping[src] = out
        mapping[Path(src).name] = out
        mapping[f"../{src}"] = out
    return mapping


def rewrite_links(text: str, links: dict[str, str]) -> str:
    """Markdown内のリポジトリ内リンクを、出力側のファイル名へ差し替える."""
    def repl(m: re.Match) -> str:
        label, target = m.group(1), m.group(2)
        anchor = ""
        if "#" in target:
            target, anchor = target.split("#", 1)
            anchor = "#" + anchor
        hit = links.get(target)
        return f"[{label}]({hit}{anchor})" if hit else m.group(0)

    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", repl, text)


def nav_html(current: str) -> str:
    items = []
    for _src, out, label, in_nav in PAGES:
        if not in_nav:
            continue
        cls = "item on" if out == current else "item"
        items.append(f'<a class="{cls}" href="{out}">{label}</a>')
    for _src, out, label in COPIES:
        cls = "item on" if out == current else "item"
        items.append(f'<a class="{cls}" href="{out}">{label}</a>')
    return (f'<nav><a class="brand" href="index.html">奈良春日 鹿のや<br>'
            f'レベニューマネジメント基盤</a>'
            f'<div class="items">{"".join(items)}</div></nav>')


def page_html(title: str, body: str, current: str) -> str:
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{title} — 奈良春日 鹿のや レベニューマネジメント基盤</title>
<style>{CSS}</style></head>
<body><div class="wrap">{nav_html(current)}<main>{body}
<footer>社内資料。競合施設の実名・価格ポジション・収益前提を含みます。
取り扱いにご注意ください。</footer>
</main></div></body></html>
"""


def main() -> int:
    links = build_link_map()
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    md = markdown.Markdown(extensions=["tables", "fenced_code", "sane_lists", "attr_list"])

    written = 0
    for src, out, label, _nav in PAGES:
        path = ROOT / src
        if not path.exists():
            print(f"  スキップ（見つからない）: {src}", file=sys.stderr)
            continue
        text = rewrite_links(path.read_text(encoding="utf-8"), links)
        md.reset()
        body = md.convert(text)
        # 横に広い表がページ全体を横スクロールさせないよう、個別に包む
        body = body.replace("<table>", '<div class="tablewrap"><table>')
        body = body.replace("</table>", "</table></div>")
        (OUT / out).write_text(page_html(label, body, out), encoding="utf-8")
        written += 1

    for src, out, _label in COPIES:
        path = ROOT / src
        if not path.exists():
            print(f"  スキップ（見つからない）: {src}", file=sys.stderr)
            continue
        shutil.copyfile(path, OUT / out)
        written += 1

    print(f"site/ に {written} ページを生成しました")
    print(f"  出力先: {OUT}")
    print("  ホスティング側でのビルドは不要です（そのまま配信できます）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
