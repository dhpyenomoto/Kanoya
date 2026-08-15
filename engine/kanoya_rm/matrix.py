"""施設 × 宿泊日 の ADR マトリクス — 「どの日に誰がいくらで出しているか」を一覧する.

survey レポートは日ごとの判断を出すが、値付けの現場で実際に見たいのは
「この日、競合各社がいくらで出しているか」を横並びにした表である。
価格が1社だけ突出しているのか、市場全体が上がっているのかは、
中央値だけを見ていても分からない。

出力は3系統:
  render_text  端末での一覧（千円単位・売止と欠測を区別）
  render_csv   Excel等での再加工用
  render_html  ヒートマップ付きの視覚表（ブラウザで開く）

行の並びは類似度スコア順。自社を最上段に固定し、直下に市場中央値を置くことで、
「自社が市場のどこにいるか」が縦方向に読めるようにしている。
"""

from __future__ import annotations

import csv
import html
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
class MatrixReport:
    property_name: str
    as_of: date
    dates: list[date]
    rows: list[MatrixRow]
    day_labels: dict[date, str]
    event_labels: dict[date, str]
    fixture: bool
    radius_m: int = 0

    @property
    def competitor_rows(self) -> list[MatrixRow]:
        return [r for r in self.rows if not r.is_self and not r.is_summary]

    def value_range(self) -> tuple[float, float]:
        vals = [v for r in self.competitor_rows for v in r.values()]
        return (min(vals), max(vals)) if vals else (0.0, 1.0)


def build(settings: Settings, ctx, window: tuple[date, date], *,
          fixture: bool = False, radius_m: int = 0) -> MatrixReport:
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
    for comp in order:
        row = MatrixRow(comp_id=comp.id, name=comp.name, tier=comp.tier,
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


# ---- HTML（ヒートマップ） -------------------------------------------------

def _ramp(value: float, lo: float, hi: float) -> tuple[str, str]:
    """ADRの大きさを単一色相の濃淡へ写す（順序尺度なので単色ランプが正しい）."""
    if hi <= lo:
        t = 0.5
    else:
        t = min(1.0, max(0.0, (value - lo) / (hi - lo)))
    # 明度のみを動かす。色相を混ぜると大小関係が読めなくなる。
    lightness = 96 - t * 46          # 96% → 50%
    text = "#12312a" if t < 0.55 else "#ffffff"
    return f"hsl(163 38% {lightness:.0f}%)", text


def render_html(report: MatrixReport) -> str:
    lo, hi = report.value_range()
    esc = html.escape

    head = "".join(
        f'<th class="d{" ev" if report.event_labels.get(d) else ""}'
        f'{" we" if report.day_labels[d] in ("SAT", "FRI") else ""}">'
        f'<span class="md">{d.strftime("%m/%d")}</span>'
        f'<span class="dw">{report.day_labels[d]}</span></th>'
        for d in report.dates
    )

    body_rows: list[str] = []
    for row in report.rows:
        classes = []
        if row.is_self:
            classes.append("self")
        if row.is_summary:
            classes.append("summary")
        cells = []
        for d in report.dates:
            value = row.cells.get(d, MISSING)
            if value == SOLD_OUT:
                cells.append('<td class="so" title="売止（在庫なし）">満</td>')
            elif value == MISSING or not isinstance(value, (int, float)) or value <= 0:
                cells.append('<td class="na" title="データなし">·</td>')
            else:
                if row.is_self or row.is_summary:
                    cells.append(f'<td class="plain">{value / 1000:,.0f}</td>')
                else:
                    bg, fg = _ramp(value, lo, hi)
                    cells.append(
                        f'<td style="background:{bg};color:{fg}" '
                        f'title="{esc(row.name)} {d.isoformat()}: ¥{value:,.0f}">'
                        f'{value / 1000:,.0f}</td>'
                    )
        meta = (f'<td class="m">{row.weight:.2f}</td>'
                f'<td class="m">{row.rooms or "—"}</td>'
                f'<td class="m">{row.distance_km:.1f}</td>'
                if not (row.is_self or row.is_summary)
                else '<td class="m">—</td><td class="m">—</td><td class="m">—</td>')
        avg = f'{row.average / 1000:,.0f}' if row.average else "—"
        body_rows.append(
            f'<tr class="{" ".join(classes)}">'
            f'<th class="n">{esc(row.name)}'
            f'<span class="tier">{esc(row.tier)}</span></th>'
            f'{"".join(cells)}<td class="avg">{avg}</td>{meta}</tr>'
        )

    warn = ('<p class="warn">██ フィクスチャ（擬似）データです。実勢価格ではありません。'
            'APIキーを設定し <code>--source serpapi</code> で再実行してください。</p>'
            if report.fixture else "")
    radius = (f'／ 調査範囲 半径{report.radius_m / 1000:.1f}km'
              if report.radius_m else "")

    return f"""<title>{esc(report.property_name)} ADRマトリクス</title>
<style>
:root {{
  --ground:#F5F6F2; --surface:#FFFFFF; --alt:#EDEFE9; --ink:#1A211C;
  --ink2:#414B44; --muted:#6B746D; --line:#D8DCD3; --accent:#14584A;
  --warn:#B8452B; --self:#FFF6E8; --selfline:#C9922E;
}}
@media (prefers-color-scheme:dark) {{
  :root:not([data-theme="light"]) {{
    --ground:#141715; --surface:#1C201D; --alt:#232823; --ink:#E8EBE6;
    --ink2:#C0C7BF; --muted:#949C94; --line:#2E342E; --accent:#4FB49A;
    --warn:#DD6A46; --self:#2A2317; --selfline:#C9922E;
  }}
}}
:root[data-theme="dark"] {{
  --ground:#141715; --surface:#1C201D; --alt:#232823; --ink:#E8EBE6;
  --ink2:#C0C7BF; --muted:#949C94; --line:#2E342E; --accent:#4FB49A;
  --warn:#DD6A46; --self:#2A2317; --selfline:#C9922E;
}}
*{{box-sizing:border-box}}
body{{margin:0;padding:28px 22px 64px;background:var(--ground);color:var(--ink);
 font-family:"Hiragino Sans","Yu Gothic","Noto Sans JP",system-ui,sans-serif;font-size:14px}}
h1{{font-family:"Hiragino Mincho ProN","Yu Mincho","Noto Serif JP",serif;
 font-size:24px;font-weight:600;margin:0 0 6px}}
.sub{{color:var(--muted);font-size:13px;margin:0 0 4px}}
.warn{{background:var(--warn);color:#fff;padding:9px 14px;font-weight:700;
 margin:14px 0;border-radius:3px;font-size:13px}}
.legend{{display:flex;gap:18px;flex-wrap:wrap;align-items:center;margin:16px 0 12px;
 font-size:12px;color:var(--muted)}}
.legend b{{color:var(--ink2);font-weight:600}}
.ramp{{display:inline-flex;height:12px;width:150px;border:1px solid var(--line)}}
.ramp i{{flex:1}}
.scroll{{overflow:auto;max-height:78vh;border:1px solid var(--line);background:var(--surface)}}
table{{border-collapse:separate;border-spacing:0;font-variant-numeric:tabular-nums}}
th,td{{padding:5px 7px;font-size:12px;white-space:nowrap;border-bottom:1px solid var(--line)}}
thead th{{position:sticky;top:0;z-index:3;background:var(--alt);border-bottom:2px solid var(--line)}}
th.n{{position:sticky;left:0;z-index:2;background:var(--surface);text-align:left;
 min-width:210px;max-width:210px;overflow:hidden;text-overflow:ellipsis;
 border-right:2px solid var(--line);font-weight:600}}
thead th.n{{z-index:4;background:var(--alt)}}
.tier{{display:block;font-size:10px;color:var(--muted);font-weight:400}}
th.d{{text-align:center;min-width:52px}}
th.d .md{{display:block;font-weight:600}}
th.d .dw{{display:block;font-size:10px;color:var(--muted);font-weight:400}}
th.d.we{{background:var(--surface)}}
th.d.ev{{box-shadow:inset 0 -3px 0 var(--accent)}}
td{{text-align:right;font-variant-numeric:tabular-nums}}
td.so{{background:repeating-linear-gradient(45deg,var(--alt),var(--alt) 4px,transparent 4px,transparent 8px);
 color:var(--warn);text-align:center;font-weight:700}}
td.na{{color:var(--line);text-align:center}}
td.plain{{background:var(--alt);font-weight:600}}
td.avg{{background:var(--alt);font-weight:700;border-left:2px solid var(--line)}}
td.m{{color:var(--muted);background:var(--surface)}}
tr.self th.n,tr.self td{{background:var(--self)}}
tr.self{{border-left:3px solid var(--selfline)}}
tr.self th.n{{border-left:3px solid var(--selfline)}}
tr.summary th.n,tr.summary td{{background:var(--alt);font-weight:600}}
tbody tr:hover td:not(.so):not(.na){{outline:2px solid var(--accent);outline-offset:-2px}}
</style>
<h1>ADRマトリクス — {esc(report.property_name)}</h1>
<p class="sub">基準日 {report.as_of.isoformat()} ／ 対象 {len(report.dates)}日{radius}</p>
<p class="sub">単位は千円。すべて「1室2名1泊2食・税サ込」へ正規化（NAR）した値です。</p>
{warn}
<div class="legend">
  <span><b>安</b> <span class="ramp"><i style="background:hsl(163 38% 96%)"></i>
    <i style="background:hsl(163 38% 84%)"></i><i style="background:hsl(163 38% 73%)"></i>
    <i style="background:hsl(163 38% 61%)"></i><i style="background:hsl(163 38% 50%)"></i></span> <b>高</b>
    （{lo / 1000:,.0f}〜{hi / 1000:,.0f}千円）</span>
  <span><b>満</b> 売止（在庫なし）</span>
  <span><b>·</b> データなし</span>
  <span>行の並び = <b>類似度スコア降順</b>（似ている競合が上）</span>
  <span>下線付きの日付 = 需要イベント</span>
</div>
<div class="scroll"><table>
<thead><tr><th class="n">施設</th>{head}
<th class="d">平均</th><th class="d">類似度</th><th class="d">客室</th><th class="d">距離km</th></tr></thead>
<tbody>{"".join(body_rows)}</tbody>
</table></div>
"""
