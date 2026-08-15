"""需要リボンの SVG 生成。

面積がエリア全体のクチコミ発生量（7日移動合計）、金色の帯がそのうち自社ぶん。
帯が細るときはシェアを落としている、という一枚の絵にするのが目的。
軸は日付とクチコミ件数だけで、価格は一切含まない。
"""

from __future__ import annotations

import math
from html import escape

from .models import RibbonSeries

WIDTH, HEIGHT = 900, 190
X_LEFT, X_RIGHT = 34.0, 892.0
Y_TOP, Y_BASE = 12.0, 164.0
LABEL_Y = 182

AREA_COLOR = "#7FA891"
OWN_COLOR = "#C6A252"
EVENT_COLOR = "#C04A3C"
GRID_COLOR = "#2E3B33"
MUTED = "#8A9689"

GRID_STEP = 25


def _scale_top(peak: float) -> float:
    """目盛りの上限。25刻みの切り上げ。最低でも50は取る。"""
    if peak <= 0:
        return float(GRID_STEP * 2)
    return float(max(GRID_STEP * 2, math.ceil(peak / GRID_STEP) * GRID_STEP))


def _area_path(xs: list[float], ys: list[float]) -> str:
    points = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    return f"M{X_LEFT:.0f},{Y_BASE:.0f} L{points} L{xs[-1]:.1f},{Y_BASE:.0f} Z"


def render_ribbon(ribbon: RibbonSeries) -> str:
    n = len(ribbon.dates)
    if n < 2:
        return '<svg viewBox="0 0 900 190" role="img" aria-label="データ不足"></svg>'

    top = _scale_top(max(ribbon.area) if ribbon.area else 0.0)
    step_x = (X_RIGHT - X_LEFT) / (n - 1)
    unit_y = (Y_BASE - Y_TOP) / top

    xs = [X_LEFT + i * step_x for i in range(n)]
    area_ys = [Y_BASE - v * unit_y for v in ribbon.area]
    own_ys = [Y_BASE - v * unit_y for v in ribbon.own]

    parts = [
        f'<svg viewBox="0 0 {WIDTH} {HEIGHT}" preserveAspectRatio="none" role="img"'
        ' aria-label="エリア需要と自社シェアの推移">'
    ]

    # 目盛り
    for value in range(GRID_STEP, int(top) + 1, GRID_STEP):
        if value % (GRID_STEP * 2):
            continue
        y = Y_BASE - value * unit_y
        parts.append(
            f'<line x1="{X_LEFT:.0f}" y1="{y:.1f}" x2="{X_RIGHT:.0f}" y2="{y:.1f}"'
            f' stroke="{GRID_COLOR}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="26" y="{y + 4:.1f}" fill="{MUTED}" font-size="9"'
            f' font-family="monospace" text-anchor="end">{value}</text>'
        )

    # エリア合計（塗り＋輪郭）
    area_d = _area_path(xs, area_ys)
    parts.append(f'<path d="{area_d}" fill="{AREA_COLOR}" opacity=".28"/>')
    parts.append(
        f'<path d="{area_d}" fill="none" stroke="{AREA_COLOR}"'
        ' stroke-width="1.4" opacity=".7"/>'
    )

    # 自社ぶん（エリアに重ねて描く。積み上げではない）
    parts.append(
        f'<path d="{_area_path(xs, own_ys)}" fill="{OWN_COLOR}" opacity=".85"/>'
    )

    # 需要イベント（窓の中にあるものだけ）
    first, last = ribbon.dates[0], ribbon.dates[-1]
    for event in ribbon.events:
        if not first <= event.date <= last:
            continue
        x = X_LEFT + (event.date - first).days * step_x
        parts.append(
            f'<line x1="{x:.1f}" y1="{Y_TOP:.0f}" x2="{x:.1f}" y2="{Y_BASE:.0f}"'
            f' stroke="{EVENT_COLOR}" stroke-width="1" stroke-dasharray="2 3"'
            f' opacity=".8"><title>{escape(event.name)}</title></line>'
        )

    # 日付ラベル
    label_step = max(1, (n - 1) // 6)
    for i in range(0, n, label_step):
        parts.append(
            f'<text x="{xs[i]:.1f}" y="{LABEL_Y}" fill="{MUTED}" font-size="9"'
            f' font-family="monospace" text-anchor="middle">'
            f'{ribbon.dates[i]:%m-%d}</text>'
        )

    parts.append(
        f'<line x1="{X_LEFT:.0f}" y1="{Y_BASE:.0f}" x2="{X_RIGHT:.0f}"'
        f' y2="{Y_BASE:.0f}" stroke="{GRID_COLOR}"/>'
    )
    parts.append("</svg>")
    return "".join(parts)
