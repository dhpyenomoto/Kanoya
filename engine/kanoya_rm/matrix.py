"""施設 × 宿泊日 の ADR マトリクス — 「どの日に誰がいくらで出しているか」を一覧する.

survey レポートは日ごとの判断を出すが、値付けの現場で実際に見たいのは
「この日、競合各社がいくらで出しているか」を横並びにした表である。
価格が1社だけ突出しているのか、市場全体が上がっているのかは、
中央値だけを見ていても分からない。

出力は4系統:
  render_text      端末での一覧（千円単位・売止と欠測を区別）
  write_csv        Excel等での再加工用
  render_markdown  GitHub上でそのまま表示される表（社内共有向け）
  render_html      ヒートマップ付きの視覚表。**調べたい日程を画面上で変えられる**

HTML版は全期間のデータをページ内に埋め込み、期間の絞り込みをブラウザ側で行う。
日付を変えるたびに Python を再実行したり HTML を書き換えたりする必要がない。

行の並びは類似度スコア順。自社を最上段に固定し、直下に市場中央値を置くことで、
「自社が市場のどこにいるか」が縦方向に読めるようにしている。
"""

from __future__ import annotations

import csv
import html
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .config import Settings

SOLD_OUT = "SOLD_OUT"
MISSING = "MISSING"


@dataclass
class MatrixRow:
    comp_id: str
    name: str
    tier: str
    weight: float
    rooms: int
    distance_km: float
    is_self: bool = False
    is_summary: bool = False
    cells: dict[date, float | str] = field(default_factory=dict)

    def values(self) -> list[float]:
        return [v for v in self.cells.values() if isinstance(v, (int, float)) and v > 0]

    @property
    def average(self) -> float:
        vals = self.values()
        return sum(vals) / len(vals) if vals else 0.0

    @property
    def coverage(self) -> int:
        return len(self.values())

    @property
    def soldout_count(self) -> int:
        return sum(1 for v in self.cells.values() if v == SOLD_OUT)


@dataclass
class SelfPricing:
    """自社価格の1名あたり内訳（税サ込）.

    マトリクスの単位は「1室2名1泊2食・税サ込」の総額（NAR）だが、
    値付けの現場で実際に入力するのはOTAプランの1名単価である。
    夕食・朝食は原価にほぼ固定されるため、レベニューマネジメントで
    動かせるのは宿泊単価だけ。この分解を持っておくと、
    「推奨総額 → 実際に打ち込む宿泊単価」の変換が機械的にできる。
    """

    room: float
    dinner: float
    breakfast: float
    occupancy: int
    floor: float
    ceiling: float

    @property
    def meals(self) -> float:
        return self.dinner + self.breakfast

    @property
    def per_person(self) -> float:
        return self.room + self.meals

    @property
    def total(self) -> float:
        return self.per_person * self.occupancy

    def room_rate_for(self, total: float) -> float | None:
        """1室総額から、食事を除いた1名あたり宿泊単価を逆算する.

        食事代が総額を超える場合は None。値付けとして成立していない。
        """
        per = total / self.occupancy - self.meals
        return per if per > 0 else None


def self_pricing_of(settings: Settings) -> SelfPricing:
    """設定ファイルから自社価格の内訳を読む.

    rate_components が無い設定でも動くよう、基準価格から
    「食事を除いた残り」として宿泊単価を復元する。
    """
    prop = settings.property
    occupancy = int(prop["property"].get("standard_occupancy", 2)) or 2
    comps = prop.get("rate_components") or {}
    guards = prop.get("guardrails", {})
    anchor = float(prop.get("base", {}).get("anchor_room_rate", 0.0))

    dinner = float(comps.get("dinner_per_person", 0.0))
    breakfast = float(comps.get("breakfast_per_person", 0.0))
    room = comps.get("room_per_person")
    if room is None:
        room = max(0.0, anchor / occupancy - dinner - breakfast)
    return SelfPricing(
        room=float(room), dinner=dinner, breakfast=breakfast,
        occupancy=occupancy,
        floor=float(guards.get("floor_room_rate", 0.0)),
        ceiling=float(guards.get("ceiling_room_rate", 0.0)),
    )


@dataclass
class MatrixReport:
    property_name: str
    as_of: date
    dates: list[date]
    rows: list[MatrixRow]
    day_labels: dict[date, str]
    event_labels: dict[date, str]
    fixture: bool
    radius_m: int = 0
    pricing: SelfPricing | None = None

    @property
    def competitor_rows(self) -> list[MatrixRow]:
        return [r for r in self.rows if not r.is_self and not r.is_summary]

    def value_range(self) -> tuple[float, float]:
        vals = [v for r in self.competitor_rows for v in r.values()]
        return (min(vals), max(vals)) if vals else (0.0, 1.0)


def _anon_label(index: int) -> str:
    """0→競合A, 1→競合B, … 26件目からは 競合AA へ繰り上がる."""
    letters = ""
    n = index
    while True:
        letters = chr(ord("A") + n % 26) + letters
        n = n // 26 - 1
        if n < 0:
            break
    return f"競合{letters}"


def build_full(settings: Settings, ctx, **kwargs) -> MatrixReport:
    """収集済みの全宿泊日でマトリクスを組む.

    HTML版は期間の絞り込みをブラウザ側で行うため、データを全期間分持たせる。
    こうすると、日付を変えるたびに Python を再実行する必要がなくなる。
    """
    days = sorted(ctx.recommendations)
    if not days:
        return build(settings, ctx, (date.max, date.min), **kwargs)
    return build(settings, ctx, (days[0], days[-1]), **kwargs)


def build(settings: Settings, ctx, window: tuple[date, date], *,
          fixture: bool = False, radius_m: int = 0,
          anonymize: bool = False) -> MatrixReport:
    """施設×宿泊日のマトリクスを組み立てる.

    anonymize=True で競合名を「競合A」「競合B」…に置き換える。
    外部への共有時に自社のコンペティティブセットを晒さないためと、
    擬似データの見本を公開する際に実在施設へ架空価格を紐づけないための機能。
    """
    dates = [d for d in sorted(ctx.recommendations) if window[0] <= d <= window[1]]

    rows: list[MatrixRow] = []

    # ① 自社（現行と推奨）を最上段に固定する
    own = MatrixRow(comp_id="__self__", name=settings.property["property"]["name"],
                    tier="自社", weight=1.0,
                    rooms=int(settings.property["property"]["rooms"]),
                    distance_km=0.0, is_self=True)
    own_reco = MatrixRow(comp_id="__self_reco__", name="└ エンジン推奨",
                         tier="自社", weight=1.0, rooms=0, distance_km=0.0,
                         is_self=True)
    for day in dates:
        rec = ctx.recommendations[day]
        own.cells[day] = rec.current_rate
        own_reco.cells[day] = rec.recommended_rate
    rows.extend([own, own_reco])

    # ② 市場中央値（自社の直下に置き、縦方向で位置が読めるようにする）
    median_row = MatrixRow(comp_id="__median__", name="市場中央値（NAR）",
                           tier="指標", weight=1.0, rooms=0, distance_km=0.0,
                           is_summary=True)
    for day in dates:
        snap = ctx.comp_snapshots.get(day)
        median_row.cells[day] = (
            snap.weighted_median_nar if snap and snap.weighted_median_nar > 0 else MISSING
        )
    rows.append(median_row)

    # ③ 競合各社。類似度スコア（weight）の降順＝「似ている順」
    order = sorted(settings.competitors.values(),
                   key=lambda c: (-c.weight, c.distance_km))
    for index, comp in enumerate(order):
        label = _anon_label(index) if anonymize else comp.name
        row = MatrixRow(comp_id=comp.id, name=label, tier=comp.tier,
                        weight=comp.weight, rooms=comp.rooms,
                        distance_km=comp.distance_km)
        for day in dates:
            snap = ctx.comp_snapshots.get(day)
            if snap is None:
                row.cells[day] = MISSING
                continue
            found = next((d for d in snap.detail if d[0] == comp.id), None)
            if found is None:
                row.cells[day] = MISSING
            elif not found[2]:
                row.cells[day] = SOLD_OUT
            else:
                row.cells[day] = found[1]
        rows.append(row)

    return MatrixReport(
        property_name=settings.property["property"]["name"],
        as_of=ctx.snapshot,
        dates=dates,
        rows=rows,
        day_labels={d: settings.dow_of(d) for d in dates},
        event_labels={d: settings.event_score_of(d)[1] for d in dates},
        fixture=fixture,
        radius_m=radius_m,
        pricing=self_pricing_of(settings),
    )


# ---- 端末出力 -----------------------------------------------------------

def _cell_text(value: float | str) -> str:
    if value == SOLD_OUT:
        return "満"
    if value == MISSING or not isinstance(value, (int, float)) or value <= 0:
        return "·"
    return f"{value / 1000:.0f}"


def render_text(report: MatrixReport, *, max_cols: int = 21) -> str:
    if not report.dates:
        return "（対象期間にデータがありません）"

    dates = report.dates[:max_cols]
    truncated = len(report.dates) - len(dates)
    name_w = 22

    lines: list[str] = []
    lines.append("=" * (name_w + 6 * len(dates) + 22))
    lines.append(f"  ADRマトリクス — {report.property_name}")
    lines.append(f"  基準日 {report.as_of.isoformat()} ／ "
                 f"単位:千円（1室2名1泊2食・税サ込 換算）／ 満=売止 ·=データなし")
    if report.radius_m:
        lines.append(f"  調査範囲 半径{report.radius_m / 1000:.1f}km ／ 行順=類似度スコア降順")
    lines.append("=" * (name_w + 6 * len(dates) + 22))
    if report.fixture:
        lines.append("  ██ フィクスチャ（擬似）データです。実勢価格ではありません。 ██")

    header = " " * name_w + "".join(f"{d.strftime('%m/%d'):>6}" for d in dates)
    dow = " " * name_w + "".join(f"{report.day_labels[d][:3]:>6}" for d in dates)
    lines.append("")
    lines.append(header + f"{'平均':>8}{'類似度':>7}")
    lines.append(dow + f"{'':>8}{'':>7}")
    lines.append("-" * (name_w + 6 * len(dates) + 15))

    rule = "-" * (name_w + 6 * len(dates) + 15)
    for row in report.rows:
        # 自社ブロックと市場中央値と競合群を罫線で区切る
        if row.comp_id == "__median__":
            lines.append(rule)
        label = row.name[: name_w - 2]
        cells = "".join(f"{_cell_text(row.cells.get(d, MISSING)):>6}" for d in dates)
        avg = f"{row.average / 1000:>7.0f}" if row.average else "      -"
        sim = "" if (row.is_self or row.is_summary) else f"{row.weight:>7.2f}"
        lines.append(f"{label:<{name_w}}{cells}{avg}{sim}")
        if row.comp_id == "__median__":
            lines.append(rule)

    if truncated > 0:
        lines.append(f"\n  ※ 残り{truncated}日は端末では省略。全期間はCSV/HTML出力を参照。")
    return "\n".join(lines)


# ---- CSV ---------------------------------------------------------------

def write_csv(report: MatrixReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["施設", "区分", "類似度", "客室数", "距離km", "平均ADR", "取得日数", "売止日数"]
                   + [d.isoformat() for d in report.dates])
        w.writerow(["", "", "", "", "", "", "", ""]
                   + [report.day_labels[d] for d in report.dates])
        for row in report.rows:
            cells = []
            for d in report.dates:
                v = row.cells.get(d, MISSING)
                cells.append("売止" if v == SOLD_OUT
                             else ("" if v == MISSING or not isinstance(v, (int, float))
                                   else round(v)))
            w.writerow([
                row.name, row.tier,
                "" if row.is_self or row.is_summary else f"{row.weight:.3f}",
                row.rooms or "", f"{row.distance_km:.1f}" if row.distance_km else "",
                round(row.average) if row.average else "",
                row.coverage, row.soldout_count,
            ] + cells)


# ---- Markdown -----------------------------------------------------------

def render_markdown(report: MatrixReport) -> str:
    """GitHub上でそのまま表示される表を出す.

    HTMLヒートマップは GitHub のファイル閲覧では描画されない（ソースが出るだけ）。
    非公開リポジトリを社内で共有する運用では、GitHub Pages を使わずに
    「リポジトリを開いて .md を押せば読める」ほうが実用的なため、
    色を諦めて可搬性を取った版を用意する。
    """
    if not report.dates:
        return "（対象期間にデータがありません）"

    lines: list[str] = []
    lines.append(f"# ADRマトリクス — {report.property_name}")
    lines.append("")
    lines.append(f"- 基準日: **{report.as_of.isoformat()}** ／ 対象 {len(report.dates)}日")
    lines.append("- 単位は**千円**。すべて「1室2名1泊2食・税サ込」へ正規化（NAR）した値")
    lines.append("- `満` = 売止（在庫なし） ／ `·` = データなし")
    if report.pricing:
        p = report.pricing
        lines.append(
            f"- 自社の価格設定（1名・税サ込）: 宿泊 {p.room:,.0f} ＋ 夕食 {p.dinner:,.0f}"
            f" ＋ 朝食 {p.breakfast:,.0f} ＝ **{p.per_person:,.0f}** "
            f"→ 1室{p.occupancy}名 **{p.total:,.0f}**"
        )
        lines.append("  （HTML版ではこの内訳を画面上で変更でき、"
                     "推奨総額から「食事を除いた宿泊単価」を逆算します）")
    if report.radius_m:
        lines.append(f"- 調査範囲 半径{report.radius_m / 1000:.1f}km ／ 行順 = 類似度スコア降順")
    if report.fixture:
        lines.append("")
        lines.append("> ⚠️ **フィクスチャ（擬似）データです。実勢価格ではありません。**  ")
        lines.append("> 実データで判断するには APIキーを設定し `--source serpapi` で再実行してください。")
    lines.append("")

    header = ["施設"] + [f"{d.strftime('%m/%d')}<br>{report.day_labels[d]}" for d in report.dates]
    header += ["平均", "類似度"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "---|" * len(header))

    for row in report.rows:
        cells = [_cell_text(row.cells.get(d, MISSING)) for d in report.dates]
        avg = f"{row.average / 1000:,.0f}" if row.average else "—"
        sim = "—" if (row.is_self or row.is_summary) else f"{row.weight:.2f}"
        name = row.name
        if row.is_self or row.is_summary:
            name = f"**{name}**"
            cells = [f"**{c}**" for c in cells]
            avg = f"**{avg}**"
        lines.append("| " + " | ".join([name] + cells + [avg, sim]) + " |")

    return "\n".join(lines) + "\n"


# ---- HTML（ヒートマップ） -------------------------------------------------


def _payload(report: MatrixReport, initial: tuple[date, date] | None) -> dict:
    """ブラウザ側で描画するためのデータ一式."""
    def cell(value):
        if value == SOLD_OUT:
            return "SO"
        if value == MISSING or not isinstance(value, (int, float)) or value <= 0:
            return None
        return round(float(value))

    # ブラウザ側で自社ブロックに派生行を差し込むため、行の役割を明示する。
    # 表示名で判定させると、名前を変えた瞬間に静かに壊れる。
    kinds = {"__self__": "self_current", "__self_reco__": "self_reco",
             "__median__": "median"}

    p = report.pricing
    dates = [d.isoformat() for d in report.dates]
    lo = (initial[0].isoformat() if initial else (dates[0] if dates else ""))
    hi = (initial[1].isoformat() if initial else (dates[-1] if dates else ""))
    return {
        "property": report.property_name,
        "asOf": report.as_of.isoformat(),
        "radiusKm": round(report.radius_m / 1000, 1) if report.radius_m else 0,
        "fixture": report.fixture,
        "pricing": {
            "room": round(p.room), "dinner": round(p.dinner),
            "breakfast": round(p.breakfast), "occupancy": p.occupancy,
            "floor": round(p.floor), "ceiling": round(p.ceiling),
        } if p else None,
        "dates": dates,
        "dow": {d.isoformat(): report.day_labels[d] for d in report.dates},
        "events": {d.isoformat(): report.event_labels.get(d, "")
                   for d in report.dates if report.event_labels.get(d)},
        "initial": {"from": lo, "to": hi},
        "rows": [
            {
                "name": r.name,
                "kind": kinds.get(r.comp_id, "comp"),
                "tier": r.tier,
                "weight": None if (r.is_self or r.is_summary) else round(r.weight, 2),
                "rooms": r.rooms or None,
                "distance": round(r.distance_km, 1) if r.distance_km else None,
                "self": r.is_self,
                "summary": r.is_summary,
                "cells": {d.isoformat(): cell(r.cells.get(d, MISSING)) for d in report.dates},
            }
            for r in report.rows
        ],
    }


def render_html(report: MatrixReport,
                initial: tuple[date, date] | None = None) -> str:
    """調査期間を画面上で切り替えられるADRマトリクス.

    全期間のデータをページ内に埋め込み、日付の絞り込みはブラウザ側で行う。
    期間を変えるたびに Python を再実行したり HTML を書き換えたりする必要がない。
    外部リソースを一切参照しないため、ファイル単体で配布・閲覧できる。
    """
    data = json.dumps(_payload(report, initial), ensure_ascii=False)
    data = data.replace("</", "<\\/")   # </script> による早期終了を防ぐ
    esc = html.escape

    # 完全なHTML文書として出力する。charset を省くとブラウザが文字コードを
    # 推測し、日本語が文字化けする（file:// で開いた場合や、Content-Type に
    # charset を付けないサーバ経由で顕在化する）。charset は仕様上
    # 先頭1024バイト以内に置く必要があるため、head の先頭に固定する。
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{esc(report.property_name)} ADRマトリクス</title>
<style>
:root {{
  --ground:#F5F6F2; --surface:#FFFFFF; --alt:#EDEFE9; --ink:#1A211C;
  --ink2:#414B44; --muted:#6B746D; --line:#D8DCD3; --accent:#14584A;
  --warn:#B8452B; --self:#FFF6E8; --selfline:#C9922E; --field:#FFFFFF;
}}
@media (prefers-color-scheme:dark) {{
  :root:not([data-theme="light"]) {{
    --ground:#141715; --surface:#1C201D; --alt:#232823; --ink:#E8EBE6;
    --ink2:#C0C7BF; --muted:#949C94; --line:#2E342E; --accent:#4FB49A;
    --warn:#DD6A46; --self:#2A2317; --selfline:#C9922E; --field:#232823;
  }}
}}
:root[data-theme="dark"] {{
  --ground:#141715; --surface:#1C201D; --alt:#232823; --ink:#E8EBE6;
  --ink2:#C0C7BF; --muted:#949C94; --line:#2E342E; --accent:#4FB49A;
  --warn:#DD6A46; --self:#2A2317; --selfline:#C9922E; --field:#232823;
}}
*{{box-sizing:border-box}}
body{{margin:0;padding:26px 20px 60px;background:var(--ground);color:var(--ink);
 font-family:"Hiragino Sans","Yu Gothic","Noto Sans JP",system-ui,sans-serif;font-size:14px}}
h1{{font-family:"Hiragino Mincho ProN","Yu Mincho","Noto Serif JP",serif;
 font-size:23px;font-weight:600;margin:0 0 5px}}
.sub{{color:var(--muted);font-size:12.5px;margin:0 0 3px}}
.warn{{background:var(--warn);color:#fff;padding:9px 14px;font-weight:700;
 margin:13px 0;border-radius:3px;font-size:13px}}

/* ── 調査期間の入力 ───────────────────────────────── */
.panel{{background:var(--surface);border:1px solid var(--line);border-radius:4px;
 padding:15px 17px;margin:16px 0 14px}}
.panel h2{{font-size:12px;letter-spacing:.08em;color:var(--accent);margin:0 0 12px;
 font-weight:700}}
.fields{{display:flex;flex-wrap:wrap;gap:14px 18px;align-items:flex-end}}
.field{{display:flex;flex-direction:column;gap:5px}}
.field label{{font-size:11px;color:var(--muted)}}
input[type=date],input[type=number],select{{font:inherit;font-size:14px;padding:6px 9px;
 border:1px solid var(--line);border-radius:3px;background:var(--field);
 color:var(--ink);min-width:150px}}
input[type=number]{{min-width:130px;text-align:right;font-variant-numeric:tabular-nums}}
input:focus,select:focus,button:focus-visible{{outline:2px solid var(--accent);
 outline-offset:1px}}

/* ── 自社価格の内訳 ─────────────────────────────── */
.calc{{margin-top:13px;padding-top:13px;border-top:1px dashed var(--line);
 font-size:13px;line-height:1.85;font-variant-numeric:tabular-nums}}
.calc .eq{{color:var(--muted)}}
.calc b{{font-size:16px;color:var(--accent)}}
.calc .bad{{color:var(--warn);font-weight:700}}
.note{{color:var(--muted);font-size:12px;margin-top:6px;line-height:1.7}}
.presets{{display:flex;flex-wrap:wrap;gap:6px;margin-top:13px;
 padding-top:13px;border-top:1px dashed var(--line)}}
button{{font:inherit;font-size:12.5px;padding:5px 11px;border:1px solid var(--line);
 border-radius:3px;background:var(--surface);color:var(--ink2);cursor:pointer}}
button:hover{{background:var(--alt);border-color:var(--accent);color:var(--ink)}}
button.primary{{background:var(--accent);border-color:var(--accent);color:#fff;
 font-size:15px;font-weight:700;padding:8px 26px;min-height:40px}}
button.primary:hover{{background:var(--accent);opacity:.88;color:#fff}}
button.primary:active{{transform:translateY(1px)}}
.applied{{display:inline-block;margin-left:12px;font-size:12.5px;color:var(--accent);
 font-weight:700;opacity:0;transition:opacity .2s}}
.applied.on{{opacity:1}}
@media (prefers-reduced-motion:reduce){{.applied{{transition:none}}}}
.err{{color:var(--warn);font-size:12.5px;margin-top:10px;font-weight:600}}

/* ── サマリ ──────────────────────────────────────── */
.stats{{display:flex;flex-wrap:wrap;gap:1px;background:var(--line);
 border:1px solid var(--line);margin:0 0 14px}}
.stat{{background:var(--surface);padding:11px 16px;flex:1;min-width:118px}}
.stat .k{{font-size:10.5px;color:var(--muted);margin-bottom:4px}}
.stat .v{{font-size:19px;font-variant-numeric:tabular-nums;line-height:1.15}}
.stat .v small{{font-size:12px;color:var(--muted);margin-left:2px}}

.legend{{display:flex;gap:16px;flex-wrap:wrap;align-items:center;margin:0 0 11px;
 font-size:12px;color:var(--muted)}}
.legend b{{color:var(--ink2);font-weight:600}}
.ramp{{display:inline-flex;height:11px;width:130px;border:1px solid var(--line)}}
.ramp i{{flex:1}}

/* ── 表 ─────────────────────────────────────────── */
.scroll{{overflow:auto;max-height:70vh;border:1px solid var(--line);background:var(--surface)}}
table{{border-collapse:separate;border-spacing:0;font-variant-numeric:tabular-nums}}
th,td{{padding:5px 7px;font-size:12px;white-space:nowrap;border-bottom:1px solid var(--line)}}
thead th{{position:sticky;top:0;z-index:3;background:var(--alt);
 border-bottom:2px solid var(--line)}}
th.n{{position:sticky;left:0;z-index:2;background:var(--surface);text-align:left;
 min-width:205px;max-width:205px;overflow:hidden;text-overflow:ellipsis;
 border-right:2px solid var(--line);font-weight:600}}
thead th.n{{z-index:4;background:var(--alt)}}
.tier{{display:block;font-size:10px;color:var(--muted);font-weight:400}}
th.d{{text-align:center;min-width:52px}}
th.d .md{{display:block;font-weight:600}}
th.d .dw{{display:block;font-size:10px;color:var(--muted);font-weight:400}}
th.d.ev{{box-shadow:inset 0 -3px 0 var(--accent)}}
td{{text-align:right}}
td.so{{background:repeating-linear-gradient(45deg,var(--alt),var(--alt) 4px,transparent 4px,transparent 8px);
 color:var(--warn);text-align:center;font-weight:700}}
td.na{{color:var(--line);text-align:center}}
td.plain{{background:var(--alt);font-weight:600}}
td.avg{{background:var(--alt);font-weight:700;border-left:2px solid var(--line)}}
td.m{{color:var(--muted);background:var(--surface)}}
tr.self th.n,tr.self td{{background:var(--self)}}
tr.self th.n{{border-left:3px solid var(--selfline)}}
tr.summary th.n,tr.summary td{{background:var(--alt);font-weight:600}}
tbody tr:hover td:not(.so):not(.na){{outline:2px solid var(--accent);outline-offset:-2px}}
@media (prefers-reduced-motion:reduce){{*{{transition:none!important}}}}
</style>
</head>
<body>

<h1>ADRマトリクス — {esc(report.property_name)}</h1>
<p class="sub">基準日 {report.as_of.isoformat()}{
  f" ／ 調査範囲 半径{report.radius_m / 1000:.1f}km" if report.radius_m else ""}</p>
<p class="sub">単位は千円。すべて「1室2名1泊2食・税サ込」へ正規化（NAR）した値です。</p>
{'<p class="warn">フィクスチャ（擬似）データです。実勢価格ではありません。</p>'
 if report.fixture else ''}

<div class="panel">
  <h2>調べたい日程</h2>
  <div class="fields">
    <div class="field"><label for="from">開始日（宿泊日）</label>
      <input type="date" id="from"></div>
    <div class="field"><label for="to">終了日（宿泊日）</label>
      <input type="date" id="to"></div>
    <div class="field"><label for="month">月でまとめて選ぶ</label>
      <select id="month"><option value="">—</option></select></div>
    <div class="field"><label>&nbsp;</label>
      <div><button type="button" id="apply" class="primary">この期間で表示</button>
      <span class="applied" id="applied">更新しました</span></div></div>
  </div>
  <div class="presets" id="presets"></div>
  <div class="err" id="err" hidden></div>
</div>

<div class="panel">
  <h2>自社の価格設定（1名あたり・税サ込）</h2>
  <div class="fields">
    <div class="field"><label for="pRoom">宿泊単価／人</label>
      <input type="number" id="pRoom" inputmode="numeric" step="100" min="0"></div>
    <div class="field"><label for="pDinner">夕食単価／人</label>
      <input type="number" id="pDinner" inputmode="numeric" step="100" min="0"></div>
    <div class="field"><label for="pBfast">朝食単価／人</label>
      <input type="number" id="pBfast" inputmode="numeric" step="100" min="0"></div>
    <div class="field"><label>&nbsp;</label>
      <div><button type="button" id="applyPrice" class="primary">この価格で計算</button>
      <span class="applied" id="priceApplied">反映しました</span></div></div>
    <div class="field"><label>&nbsp;</label>
      <div><button type="button" id="resetPrice">初期値に戻す</button></div></div>
  </div>
  <div class="calc" id="calc"></div>
  <div class="err" id="priceErr" hidden></div>
  <p class="note">夕食・朝食は原価にほぼ固定されるため、値付けで動かせるのは宿泊単価だけです。
    表の「推奨 宿泊単価／人」は、エンジンの推奨総額から入力された食事単価を差し引いて
    逆算した、<b>OTAプランにそのまま入れられる数字</b>です。<br>
    初期値は<b>基準価格（アンカー）の内訳</b>です。実際の掲出価格は季節・曜日で
    上下するため、表の「現行」行とは一致しません。入力は端末に保存され、次に開いたときも残ります。</p>
</div>

<div class="stats" id="stats"></div>
<div class="legend" id="legend"></div>
<div class="scroll"><table>
  <thead><tr id="head"></tr></thead>
  <tbody id="body"></tbody>
</table></div>

<script>
const D = {data};

const $ = (id) => document.getElementById(id);
const fromEl = $("from"), toEl = $("to"), monthEl = $("month"), errEl = $("err");
const ALL = D.dates;
const MIN = ALL[0], MAX = ALL[ALL.length - 1];

// 収集済みの範囲外は選べないようにする（データが無い期間を指定しても意味がないため）
for (const el of [fromEl, toEl]) {{ el.min = MIN; el.max = MAX; }}
fromEl.value = D.initial.from;
toEl.value = D.initial.to;

// 月セレクタ（データにある月だけ）
const months = [...new Set(ALL.map(d => d.slice(0, 7)))];
for (const m of months) {{
  const o = document.createElement("option");
  o.value = m;
  o.textContent = m.replace("-", "年") + "月";
  monthEl.appendChild(o);
}}

const addDays = (iso, n) => {{
  const t = new Date(iso + "T00:00:00");
  t.setDate(t.getDate() + n);
  return t.toISOString().slice(0, 10);
}};
const clamp = (iso) => iso < MIN ? MIN : (iso > MAX ? MAX : iso);

// クイック選択。基準日（データの起点）からの相対で組む
const PRESETS = [
  ["基準日から7日",  () => [MIN, clamp(addDays(MIN, 6))]],
  ["14日",  () => [MIN, clamp(addDays(MIN, 13))]],
  ["30日",  () => [MIN, clamp(addDays(MIN, 29))]],
  ["90日",  () => [MIN, clamp(addDays(MIN, 89))]],
  ["収集済みの全期間", () => [MIN, MAX]],
];
for (const [label, fn] of PRESETS) {{
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = label;
  b.onclick = () => {{ const [a, z] = fn(); fromEl.value = a; toEl.value = z;
                      monthEl.value = ""; render(); }};
  $("presets").appendChild(b);
}}

monthEl.onchange = () => {{
  if (!monthEl.value) return;
  const inMonth = ALL.filter(d => d.startsWith(monthEl.value));
  fromEl.value = inMonth[0];
  toEl.value = inMonth[inMonth.length - 1];
  render();
}};
fromEl.onchange = toEl.onchange = () => {{ monthEl.value = ""; render(); }};

// 日付ピッカーの change がいつ飛ぶかは環境差が大きい（iOSでは閉じるまで来ない）。
// 明示的に押せるボタンを用意し、「反映された」ことも見えるようにする。
$("apply").onclick = () => {{
  render();
  if (errEl.hidden) {{
    const flag = $("applied");
    flag.classList.add("on");
    setTimeout(() => flag.classList.remove("on"), 1600);
    // 表は画面外にあることが多いので、結果まで送る
    $("stats").scrollIntoView({{ behavior: "smooth", block: "start" }});
  }}
}};

const yen = (n) => Math.round(n).toLocaleString("ja-JP");

/* ── 自社の価格設定 ────────────────────────────────
   マトリクスの単位は「1室2名1泊2食・税サ込」の総額だが、
   OTAに実際に打ち込むのは1名単価である。内訳を持たせておくと
   「推奨総額 → 打ち込む宿泊単価」を機械的に変換できる。      */
const PRICE_FIELDS = [["room", "pRoom"], ["dinner", "pDinner"],
                      ["breakfast", "pBfast"]];
const STORE_KEY = "kanoya.rateComponents.v1";
const DEFAULTS = D.pricing || {{room: 0, dinner: 0, breakfast: 0,
                               occupancy: 2, floor: 0, ceiling: 0}};
const OCC = DEFAULTS.occupancy || 2;
const priceErr = $("priceErr");

// 反映済みの値。入力欄そのものではなくこちらを描画に使う
// （打ちかけの数字が表に流れ込まないようにするため）
let PRICE = {{room: DEFAULTS.room, dinner: DEFAULTS.dinner,
              breakfast: DEFAULTS.breakfast}};

// 前回の入力を憶えておく。localStorage は file:// や
// プライベートウィンドウで例外を投げることがあるので必ず包む
function loadStored() {{
  try {{
    const raw = localStorage.getItem(STORE_KEY);
    if (!raw) return null;
    const v = JSON.parse(raw);
    return PRICE_FIELDS.every(([k]) => typeof v[k] === "number" && v[k] >= 0)
      ? v : null;
  }} catch (e) {{ return null; }}
}}
function saveStored(v) {{
  try {{ localStorage.setItem(STORE_KEY, JSON.stringify(v)); }} catch (e) {{}}
}}

const fillInputs = (v) => {{
  for (const [key, id] of PRICE_FIELDS) $(id).value = v[key];
}};

function readInputs() {{
  const out = {{}};
  for (const [key, id] of PRICE_FIELDS) {{
    const n = Number($(id).value);
    if ($(id).value === "" || !Number.isFinite(n) || n < 0) return null;
    out[key] = n;
  }}
  return out;
}}

function applyPrice() {{
  const v = readInputs();
  if (!v) {{
    priceErr.textContent = "宿泊・夕食・朝食すべてに0以上の金額を入れてください。";
    priceErr.hidden = false;
    return false;
  }}
  priceErr.hidden = true;
  PRICE = v;
  saveStored(v);
  render();
  return true;
}}

$("applyPrice").onclick = () => {{
  if (!applyPrice()) return;
  const flag = $("priceApplied");
  flag.classList.add("on");
  setTimeout(() => flag.classList.remove("on"), 1600);
  $("stats").scrollIntoView({{ behavior: "smooth", block: "start" }});
}};
$("resetPrice").onclick = () => {{ fillInputs(DEFAULTS); applyPrice(); }};
for (const [, id] of PRICE_FIELDS) $(id).onchange = applyPrice;

const stored = loadStored();
fillInputs(stored || DEFAULTS);
if (stored) PRICE = {{room: stored.room, dinner: stored.dinner,
                      breakfast: stored.breakfast}};

const meals = () => PRICE.dinner + PRICE.breakfast;
const perPerson = () => PRICE.room + meals();
const roomTotal = () => perPerson() * OCC;

/** 1室総額から、食事を除いた1名あたり宿泊単価を逆算する.
    食事代が総額を超えるなら値付けとして成立していないので null。 */
const roomRateFor = (total) => {{
  const per = total / OCC - meals();
  return per > 0 ? per : null;
}};

function renderCalc() {{
  const total = roomTotal();
  const parts = "<span class=\\"eq\\">＝ 宿泊 " + yen(PRICE.room) +
    " ＋ 夕食 " + yen(PRICE.dinner) + " ＋ 朝食 " + yen(PRICE.breakfast) + "</span>";
  let out = "1名あたり ¥" + yen(perPerson()) + " " + parts +
    "<br>1室" + OCC + "名1泊2食（表と同じ基準）<b> ¥" + yen(total) + "</b>" +
    ' <span class="eq">／ 食事が総額に占める割合 ' +
    (total ? Math.round(meals() * OCC / total * 100) : 0) + "%</span>";
  const warn = [];
  if (DEFAULTS.floor && total < DEFAULTS.floor)
    warn.push("貢献利益フロア ¥" + yen(DEFAULTS.floor) + " を下回っています。");
  if (DEFAULTS.ceiling && total > DEFAULTS.ceiling)
    warn.push("上限 ¥" + yen(DEFAULTS.ceiling) + " を超えています。");
  if (PRICE.room <= 0) warn.push("宿泊単価が0です。食事代しか取れていません。");
  if (warn.length) out += '<br><span class="bad">⚠ ' + warn.join(" ") + "</span>";
  $("calc").innerHTML = out;
}}

/** 表示する行。自社ブロックの直下に、入力から導いた2行を差し込む。 */
function displayRows() {{
  const flat = Object.fromEntries(ALL.map(d => [d, Math.round(roomTotal())]));
  const setRow = {{
    name: "└ 設定価格（入力）", tier: "自社／入力値", weight: null,
    rooms: null, distance: null, self: true, summary: false, cells: flat,
  }};
  const reco = D.rows.find(r => r.kind === "self_reco");
  const roomCells = {{}};
  for (const d of ALL) {{
    const v = reco ? reco.cells[d] : null;
    const per = (typeof v === "number") ? roomRateFor(v) : null;
    roomCells[d] = per === null ? null : Math.round(per);
  }}
  const roomRow = {{
    name: "└ 推奨 宿泊単価／人", tier: "食事を除いた1名分", weight: null,
    rooms: null, distance: null, self: true, summary: false, cells: roomCells,
  }};

  const out = [];
  for (const r of D.rows) {{
    out.push(r);
    if (r.kind === "self_reco") out.push(setRow, roomRow);
  }}
  return out;
}}

function ramp(v, lo, hi) {{
  const t = hi > lo ? Math.min(1, Math.max(0, (v - lo) / (hi - lo))) : 0.5;
  return ["hsl(163 38% " + (96 - t * 46).toFixed(0) + "%)",
          t < 0.55 ? "#12312a" : "#ffffff"];
}}

function render() {{
  const a = fromEl.value, z = toEl.value;
  errEl.hidden = true;
  renderCalc();

  if (!a || !z) {{ return; }}
  if (a > z) {{
    errEl.textContent = "開始日が終了日より後になっています。";
    errEl.hidden = false;
    return;
  }}
  const dates = ALL.filter(d => d >= a && d <= z);
  if (!dates.length) {{
    errEl.textContent = "指定された期間に収集済みのデータがありません。"
      + "収集済みの範囲は " + MIN + " 〜 " + MAX + " です。";
    errEl.hidden = false;
    $("body").innerHTML = "";
    $("head").innerHTML = "";
    $("stats").innerHTML = "";
    return;
  }}

  // 表示中の競合価格から色の範囲を決める（選んだ期間の中で濃淡が読めるように）
  let lo = Infinity, hi = -Infinity;
  for (const r of D.rows) {{
    if (r.self || r.summary) continue;
    for (const d of dates) {{
      const v = r.cells[d];
      if (typeof v === "number") {{ lo = Math.min(lo, v); hi = Math.max(hi, v); }}
    }}
  }}
  if (!isFinite(lo)) {{ lo = 0; hi = 1; }}

  // ヘッダ
  const head = $("head");
  head.innerHTML = "";
  const th0 = document.createElement("th");
  th0.className = "n"; th0.textContent = "施設";
  head.appendChild(th0);
  for (const d of dates) {{
    const th = document.createElement("th");
    th.className = "d" + (D.events[d] ? " ev" : "");
    if (D.events[d]) th.title = D.events[d];
    const md = document.createElement("span");
    md.className = "md"; md.textContent = d.slice(5).replace("-", "/");
    const dw = document.createElement("span");
    dw.className = "dw"; dw.textContent = D.dow[d];
    th.append(md, dw);
    head.appendChild(th);
  }}
  for (const t of ["平均", "類似度", "客室", "距離km"]) {{
    const th = document.createElement("th");
    th.className = "d"; th.textContent = t;
    head.appendChild(th);
  }}

  // 本体
  const body = $("body");
  body.innerHTML = "";
  const avgOf = (r) => {{
    const vals = dates.map(d => r.cells[d]).filter(v => typeof v === "number");
    return vals.length ? vals.reduce((s, v) => s + v, 0) / vals.length : null;
  }};

  for (const r of displayRows()) {{
    const tr = document.createElement("tr");
    tr.className = r.self ? "self" : (r.summary ? "summary" : "");
    const th = document.createElement("th");
    th.className = "n";
    th.appendChild(document.createTextNode(r.name));
    const tier = document.createElement("span");
    tier.className = "tier"; tier.textContent = r.tier;
    th.appendChild(tier);
    tr.appendChild(th);

    for (const d of dates) {{
      const td = document.createElement("td");
      const v = r.cells[d];
      if (v === "SO") {{
        td.className = "so"; td.textContent = "満"; td.title = "売止（在庫なし）";
      }} else if (v === null || v === undefined) {{
        td.className = "na"; td.textContent = "·"; td.title = "データなし";
      }} else {{
        td.textContent = Math.round(v / 1000).toLocaleString("ja-JP");
        td.title = r.name + " " + d + ": ¥" + yen(v);
        if (r.self || r.summary) {{ td.className = "plain"; }}
        else {{
          const [bg, fg] = ramp(v, lo, hi);
          td.style.background = bg; td.style.color = fg;
        }}
      }}
      tr.appendChild(td);
    }}

    const av = avgOf(r);
    const tdA = document.createElement("td");
    tdA.className = "avg";
    tdA.textContent = av ? Math.round(av / 1000).toLocaleString("ja-JP") : "—";
    tr.appendChild(tdA);
    const meta = [
      r.weight === null ? "—" : r.weight.toFixed(2),
      r.rooms === null ? "—" : String(r.rooms),
      r.distance === null ? "—" : r.distance.toFixed(1),
    ];
    for (const val of meta) {{
      const td = document.createElement("td");
      td.className = "m";
      td.textContent = val;
      tr.appendChild(td);
    }}
    body.appendChild(tr);
  }}

  // サマリ
  const self = D.rows.find(r => r.self);
  const reco = D.rows.filter(r => r.self)[1];
  const med  = D.rows.find(r => r.summary);
  const sAvg = self ? avgOf(self) : null;
  const rAvg = reco ? avgOf(reco) : null;
  const mAvg = med ? avgOf(med) : null;
  const pos  = (sAvg && mAvg) ? (sAvg / mAvg) : null;
  let soldout = 0, missing = 0;
  for (const r of D.rows) {{
    if (r.self || r.summary) continue;
    for (const d of dates) {{
      const v = r.cells[d];
      if (v === "SO") soldout++;
      else if (v === null || v === undefined) missing++;
    }}
  }}
  // 推奨総額の平均から、実際に打ち込む宿泊単価を逆算する。
  // 食事が固定費なので、総額の増減率より宿泊単価の増減率のほうが必ず大きくなる。
  const recoRoom = rAvg === null ? null : roomRateFor(rAvg);
  const swing = (recoRoom !== null && PRICE.room > 0)
    ? (recoRoom / PRICE.room - 1) : null;
  const pct = (x) => (x >= 0 ? "+" : "") + (x * 100).toFixed(1);

  const stat = (k, v, note) =>
    '<div class="stat"><div class="k">' + k + '</div><div class="v">' + v +
    (note ? '<small>' + note + '</small>' : '') + '</div></div>';
  $("stats").innerHTML =
    stat("対象日数", dates.length, "日") +
    stat("自社 現行 平均", sAvg ? Math.round(sAvg / 1000).toLocaleString("ja-JP") : "—", "千円") +
    stat("エンジン推奨 平均", rAvg ? Math.round(rAvg / 1000).toLocaleString("ja-JP") : "—", "千円") +
    stat("市場中央値 平均", mAvg ? Math.round(mAvg / 1000).toLocaleString("ja-JP") : "—", "千円") +
    stat("対 市場中央値", pos ? pos.toFixed(2) : "—", "倍") +
    stat("設定 1室総額", Math.round(roomTotal() / 1000).toLocaleString("ja-JP"), "千円") +
    stat("推奨 宿泊単価／人", recoRoom === null ? "—" : yen(recoRoom), "円") +
    stat("宿泊単価 設定→推奨", swing === null ? "—" : pct(swing), "%") +
    stat("競合の売止", soldout, "セル") +
    stat("データなし", missing, "セル");

  $("legend").innerHTML =
    '<span><b>安</b> <span class="ramp">' +
    [96, 84, 73, 61, 50].map(l => '<i style="background:hsl(163 38% ' + l + '%)"></i>').join("") +
    '</span> <b>高</b>（' + Math.round(lo / 1000) + '〜' + Math.round(hi / 1000) + '千円）</span>' +
    '<span><b>満</b> 売止</span><span><b>·</b> データなし</span>' +
    '<span>行順 = <b>類似度スコア降順</b></span>' +
    '<span>下線付きの日付 = 需要イベント</span>' +
    '<span>「推奨 宿泊単価／人」の行だけ <b>1名・食事別</b>の金額です</span>';
}}

render();
</script>
</body>
</html>
"""
