"""需要リボンの SVG 生成。

エリア全体のクチコミ発生量（7日移動合計）を面で、そのうち自社が占める分を
金色の帯で描く。面積が市場の大きさ、帯の厚みが自社の取り分。

同じ絵の中に市場と取り分を置くのは、「稼働が落ちた」ときに市場が縮んだのか
シェアを落としたのかを一目で切り分けるため。別々のグラフだとこの区別に
視線の往復が要る。
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from .config import DemandEvent

# 座標系。CSS 側の .ribbon svg が height:190px を与えるので viewBox と一致させる。
VIEW_W, VIEW_H = 900, 190
PLOT_LEFT, PLOT_RIGHT = 34.0, 892.0
BASELINE_Y, TOP_Y = 164.0, 12.0
LABEL_Y = 182.0

COLOR_AREA = "#7FA891"
COLOR_OWN = "#C6A252"
COLOR_EVENT = "#C04A3C"
COLOR_LINE = "#2E3B33"
COLOR_MUTED = "#8A9689"


# 半値の目盛りも整数で表示できる刻みだけを使う（2.5 や 12.5 を避ける）。
NICE_STEPS = (1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0)


def nice_ceiling(value: float) -> float:
    """value 以上でいちばん小さい「きりのよい」数。目盛りを読める値に保つ。"""
    if value <= 0:
        return 1.0
    exp = 10.0 ** math.floor(math.log10(value))
    for step in NICE_STEPS:
        candidate = step * exp
        if candidate >= value - 1e-9:
            return candidate
    return 10.0 * exp


@dataclass(frozen=True)
class Ribbon:
    dates: list[dt.date]
    area: list[float]
    own: list[float]
    events: list[DemandEvent]

    def __post_init__(self) -> None:
        if not (len(self.dates) == len(self.area) == len(self.own)):
            raise ValueError("リボンの日付と系列の長さが揃っていない")


def _x_positions(n: int) -> list[float]:
    if n <= 1:
        return [PLOT_LEFT]
    step = (PLOT_RIGHT - PLOT_LEFT) / (n - 1)
    return [PLOT_LEFT + i * step for i in range(n)]


def _area_path(xs: list[float], values: list[float], scale: float) -> str:
    points = " ".join(
        f"{x:.1f},{BASELINE_Y - v * scale:.1f}" for x, v in zip(xs, values)
    )
    return f"M{PLOT_LEFT:.0f},{BASELINE_Y:.0f} L{points} L{xs[-1]:.1f},{BASELINE_Y:.0f} Z"


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render(ribbon: Ribbon) -> str:
    """リボン 1 枚分の <svg> 要素を返す。"""
    n = len(ribbon.dates)
    if n == 0:
        return '<svg viewBox="0 0 900 190" role="img" aria-label="需要データなし"></svg>'

    xs = _x_positions(n)
    axis_max = nice_ceiling(max(ribbon.area) if ribbon.area else 1.0)
    scale = (BASELINE_Y - TOP_Y) / axis_max

    parts: list[str] = []

    # 目盛り線は半値と上限の 2 本だけ。面の形を読む邪魔をしない程度に留める。
    for tick in (axis_max / 2.0, axis_max):
        y = BASELINE_Y - tick * scale
        parts.append(
            f'<line x1="{PLOT_LEFT:.0f}" y1="{y:.1f}" x2="{PLOT_RIGHT:.0f}" y2="{y:.1f}" '
            f'stroke="{COLOR_LINE}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="26" y="{y + 4:.1f}" fill="{COLOR_MUTED}" font-size="9" '
            f'font-family="monospace" text-anchor="end">{tick:g}</text>'
        )

    area_d = _area_path(xs, ribbon.area, scale)
    parts.append(f'<path d="{area_d}" fill="{COLOR_AREA}" opacity=".28"/>')
    parts.append(
        f'<path d="{area_d}" fill="none" stroke="{COLOR_AREA}" stroke-width="1.4" opacity=".7"/>'
    )
    parts.append(
        f'<path d="{_area_path(xs, ribbon.own, scale)}" fill="{COLOR_OWN}" opacity=".85"/>'
    )

    index_by_date = {day: i for i, day in enumerate(ribbon.dates)}
    for event in ribbon.events:
        idx = index_by_date.get(event.date)
        if idx is None:
            continue
        parts.append(
            f'<line x1="{xs[idx]:.1f}" y1="{TOP_Y:.0f}" x2="{xs[idx]:.1f}" y2="{BASELINE_Y:.0f}" '
            f'stroke="{COLOR_EVENT}" stroke-width="1" stroke-dasharray="2 3" opacity=".8">'
            f"<title>{_escape(event.name)}</title></line>"
        )

    # 日付ラベルは 30 日おき。最後は必ず基準日を出す（窓の右端がどこか示すため）。
    label_indices = list(range(0, n, 30))
    if label_indices[-1] != n - 1:
        label_indices.append(n - 1)
    for idx in label_indices:
        # 両端は中央揃えだと viewBox の外にはみ出して切れる。端に寄せる。
        if idx == 0:
            anchor = "start"
        elif idx == n - 1:
            anchor = "end"
        else:
            anchor = "middle"
        parts.append(
            f'<text x="{xs[idx]:.1f}" y="{LABEL_Y:.0f}" fill="{COLOR_MUTED}" font-size="9" '
            f'font-family="monospace" text-anchor="{anchor}">{ribbon.dates[idx]:%m-%d}</text>'
        )

    parts.append(
        f'<line x1="{PLOT_LEFT:.0f}" y1="{BASELINE_Y:.0f}" x2="{PLOT_RIGHT:.0f}" '
        f'y2="{BASELINE_Y:.0f}" stroke="{COLOR_LINE}"/>'
    )

    return (
        f'<svg viewBox="0 0 {VIEW_W} {VIEW_H}" preserveAspectRatio="none" role="img" '
        f'aria-label="エリア需要と自社シェアの推移">' + "".join(parts) + "</svg>"
    )
