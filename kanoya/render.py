"""分析結果 → 1 枚の静的 HTML。

外部 CDN もフォント配信も使わない。生成物は単体で開ける HTML 1 ファイル。
"""

from __future__ import annotations

import math
from datetime import timedelta
from html import escape
from pathlib import Path

from .analysis import Analysis
from .decide import Signal
from .estimate import Estimate

TEMPLATE = Path(__file__).parent / "templates" / "dashboard.html"

# リボンの座標系
SVG_W, SVG_H = 900, 190
X0, X1 = 34, 892
Y_BASE, Y_TOP = 164, 12
MOSS, BRASS, SHU, LINE, MUTED = "#7FA891", "#C6A252", "#C04A3C", "#2E3B33", "#8A9689"


def _short_name(name: str) -> str:
    parts = name.split()
    return parts[-1] if len(parts) > 1 else name


def _nice_max(peak: float) -> float:
    """目盛りが読める丸めた上限（1 / 2 / 2.5 / 5 / 10 × 10^n）。"""
    if peak <= 0:
        return 10.0
    base = 10 ** math.floor(math.log10(peak))
    for m in (1, 2, 2.5, 5, 10):
        if peak <= m * base:
            return m * base
    return 10 * base


def build_ribbon_svg(an: Analysis) -> str:
    area, own = an.area_rolling, an.own_rolling
    n = len(area)
    if n < 2:
        return '<svg viewBox="0 0 900 190" role="img" aria-label="需要リボン（データ不足）"></svg>'

    step = (X1 - X0) / (n - 1)
    ymax = _nice_max(max(area) if area else 0)
    span = Y_BASE - Y_TOP

    def px(i: int) -> float:
        return X0 + i * step

    def py(v: float) -> float:
        return Y_BASE - min(v / ymax, 1.0) * span

    def area_path(vals: list[float]) -> str:
        pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(vals))
        return f"M{X0},{Y_BASE} L{pts} L{px(n - 1):.1f},{Y_BASE} Z"

    parts: list[str] = [
        f'<svg viewBox="0 0 {SVG_W} {SVG_H}" preserveAspectRatio="none" role="img" '
        f'aria-label="エリア需要と自社シェアの推移">'
    ]

    for frac in (0.5, 1.0):
        y = Y_BASE - frac * span
        parts.append(
            f'<line x1="{X0}" y1="{y:.1f}" x2="{X1}" y2="{y:.1f}" stroke="{LINE}" stroke-width="1"/>'
            f'<text x="{X0 - 8}" y="{y + 4:.1f}" fill="{MUTED}" font-size="9" '
            f'font-family="monospace" text-anchor="end">{ymax * frac:.0f}</text>'
        )

    d_area = area_path(area)
    parts.append(f'<path d="{d_area}" fill="{MOSS}" opacity=".28"/>')
    parts.append(f'<path d="{d_area}" fill="none" stroke="{MOSS}" stroke-width="1.4" opacity=".7"/>')
    parts.append(f'<path d="{area_path(own)}" fill="{BRASS}" opacity=".85"/>')

    for ev in an.cfg.demand_events:
        if not (an.ribbon_start <= ev.date <= an.ribbon_end):
            continue
        x = px((ev.date - an.ribbon_start).days)
        parts.append(
            f'<line x1="{x:.1f}" y1="{Y_TOP}" x2="{x:.1f}" y2="{Y_BASE}" stroke="{SHU}" '
            f'stroke-width="1" stroke-dasharray="2 3" opacity=".8">'
            f"<title>{escape(ev.name)}</title></line>"
        )

    label_every = max(1, (n - 1) // 6)
    for i in range(0, n, label_every):
        d = an.ribbon_start + timedelta(days=i)
        parts.append(
            f'<text x="{px(i):.1f}" y="182" fill="{MUTED}" font-size="9" '
            f'font-family="monospace" text-anchor="middle">{d.strftime("%m-%d")}</text>'
        )

    parts.append(f'<line x1="{X0}" y1="{Y_BASE}" x2="{X1}" y2="{Y_BASE}" stroke="{LINE}"/>')
    parts.append("</svg>")
    return "".join(parts)


def build_verdict_html(an: Analysis) -> str:
    v = an.verdict
    cls = f"verdict {v.kind}" + (" alert" if v.alert else "")
    return (
        f'<div class="{cls}">\n'
        f'    <div class="lvl">{escape(v.label)}</div>\n'
        f"    <h3>{escape(v.headline)}</h3>\n"
        f"    <p>{escape(v.body)}</p>\n"
        f"  </div>"
    )


def _delta_cell(delta: float | None) -> str:
    if delta is None:
        return '<td class="num">—</td>'
    cls = "pos" if delta >= 5 else "neg" if delta <= -5 else ""
    return f'<td class="num {cls}">{delta:+.0f}%</td>'


def build_table_rows(estimates: list[Estimate]) -> str:
    if not estimates:
        return '<tr><td colspan="6">推定できる施設がありません。</td></tr>'
    top = max(e.occupancy for e in estimates) or 1.0
    rows = []
    for e in estimates:
        tag = '<span class="tag">自社</span>' if e.is_own else ""
        width = (e.occupancy / top * 100) if top else 0.0
        rows.append(
            f'\n        <tr class="{"own" if e.is_own else ""}">\n'
            f'          <td class="nm">{escape(e.name)}{tag}</td>\n'
            f'          <td class="num">{e.rooms}</td>\n'
            f'          <td class="bar" title="95%区間 {e.occupancy_low * 100:.0f}〜'
            f'{e.occupancy_high * 100:.0f}%（クチコミ {e.reviews:.0f} 件）">'
            f'<span style="width:{width:.1f}%"></span>'
            f"<em>{e.occupancy * 100:.0f}%</em></td>\n"
            f'          <td class="num">{e.rooms_sold:.0f}</td>\n'
            f"          {_delta_cell(e.delta_pct)}\n"
            f'          <td class="conf c{e.confidence}">{e.confidence}'
            f"<small>±{e.band_pt:.0f}pt</small></td>\n"
            f"        </tr>"
        )
    return "".join(rows)


def _kv(label: str, value: str) -> str:
    return f'      <div class="kv"><span>{label}</span><b>{escape(value)}</b></div>\n'


def build_calibration_kv(an: Analysis) -> str:
    c = an.calibration
    out = _kv("算定方法", c.method)
    out += _kv("突合日数", f"{c.overlap_days}日")
    out += _kv("突合販売室数", f"{c.rooms_sold:.0f}室")
    out += _kv("突合レビュー", f"{c.reviews:.1f}件")
    if c.mae_pt is not None:
        out += _kv("実績との平均誤差", f"{c.mae_pt:.1f}pt（n={c.buckets}）")
    else:
        out += _kv("実績との平均誤差", "未検証")
    own = next((e for e in an.estimates if e.is_own), None)
    if own is not None:
        out += _kv("自社推定の95%幅", f"±{own.band_pt:.0f}pt")
    if an.own_actual_occupancy is not None and an.own_model_occupancy is not None:
        diff = (an.own_model_occupancy - an.own_actual_occupancy) * 100
        out += _kv(
            "窓内ホールドアウト",
            f"推定 {an.own_model_occupancy:.0%} / 実績 {an.own_actual_occupancy:.0%}（{diff:+.0f}pt）",
        )
    out += _kv("使ってよい範囲", c.usability)
    return out.rstrip("\n")


def build_events(an: Analysis) -> str:
    if not an.cfg.demand_events:
        return '<li class="empty">需要イベントが登録されていません。</li>'
    items = []
    for e in an.cfg.demand_events:
        items.append(
            f"<li><time>{e.date.isoformat()}</time><span>{escape(e.name)}</span>"
            f"<em>+{e.lift_pct:.0f}</em></li>"
        )
    return "".join(items)


def build_signals(signals: list[Signal]) -> str:
    out = []
    for s in signals:
        cls = f"sig {s.css}".strip()
        out.append(
            f'\n        <li class="{cls}">\n'
            f'          <div class="sig-h"><span class="cat">{escape(s.category)}</span>\n'
            f'          <span class="lvl">{escape(s.level)}</span></div>\n'
            f"          <h4>{escape(s.title)}</h4><p>{escape(s.body)}</p>\n"
            f"        </li>"
        )
    return "".join(out)


def render(an: Analysis) -> str:
    cfg = an.cfg
    rooms = cfg.own.rooms
    c = an.calibration

    rating_txt = (
        f"{an.own_rating:.2f} ／ 下限 {cfg.rules.rating_floor}"
        if an.own_rating is not None
        else f"— ／ 下限 {cfg.rules.rating_floor}"
    )
    ci = (
        f"95%CI {c.ci_low * 100:.1f}〜{c.ci_high * 100:.1f}%"
        if c.ci_low is not None and c.ci_high is not None
        else "信頼区間なし"
    )
    note = (
        "自社PMS実績で実測したレビュー投稿率。競合にも同率を適用する。"
        if c.measured
        else "自社実績と突合できていないため config の既定値を使っている。"
        "この状態の推定は水準の議論に使えない。"
    )
    steps = "/".join(f"{i / rooms * 100:.0f}" for i in range(rooms + 1)) + "%"

    values = {
        "{{PROPERTY_NAME}}": escape(cfg.own.name),
        "{{SHORT_NAME}}": escape(_short_name(cfg.own.name)),
        "{{HEADER_SUB}}": (
            f"{an.as_of.isoformat()} 時点 ・ 直近{cfg.estimation.window_days}日 ・ Google Places 由来"
        ),
        "{{VERDICT}}": build_verdict_html(an),
        "{{ROLLING_DAYS}}": str(cfg.estimation.rolling_days),
        "{{RIBBON_SVG}}": build_ribbon_svg(an),
        "{{WINDOW_DAYS}}": str(cfg.estimation.window_days),
        "{{TABLE_ROWS}}": build_table_rows(an.estimates),
        "{{RATE}}": f"{c.rate * 100:.1f}%",
        "{{RATE_CI}}": ci,
        "{{CAL_KV}}": build_calibration_kv(an),
        "{{CAL_NOTE}}": note,
        "{{SHARE}}": f"{an.own_share * 100:.1f}%" if an.own_share is not None else "—",
        "{{AREA_REVIEWS}}": f"{an.area_reviews:.0f}",
        "{{OWN_REVIEWS}}": f"{an.own_reviews:.0f}",
        "{{OWN_RATING}}": rating_txt,
        "{{EVENTS}}": build_events(an),
        "{{SIGNALS}}": build_signals(an.signals),
        "{{LAG_DAYS}}": str(cfg.estimation.review_lag_days),
        "{{OWN_ROOMS}}": str(rooms),
        "{{OWN_STEPS}}": steps,
    }

    html = TEMPLATE.read_text(encoding="utf-8")
    for token, value in values.items():
        html = html.replace(token, value)
    return html


def write(an: Analysis, out_path: str | Path) -> Path:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(an), encoding="utf-8")
    return path
