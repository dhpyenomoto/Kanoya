"""ダッシュボード HTML の生成。

数字は 1 つ残らず推定パイプラインから来る。テンプレート側では計算しない。
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass
from datetime import date

from .calibration import Backtest, ReviewRate
from .config import Config, Event
from .occupancy import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_NONE,
    MarketEstimate,
    PropertyEstimate,
)
from .reviews import date_range, rolling_sum
from .signals import Signal
from .verdict import Verdict

#: 需要リボンの移動合計窓（日）。
RIBBON_WINDOW = 7
#: 前期比に色を付ける閾値（相対%）。これ未満は誤差として黒のまま出す。
MOMENTUM_HIGHLIGHT_PCT = 5.0

_SVG_WIDTH = 900
_SVG_HEIGHT = 190
_PLOT_LEFT = 34.0
_PLOT_RIGHT = 892.0
_PLOT_TOP = 12.0
_PLOT_BASE = 164.0
_X_LABEL_COUNT = 7

_CONFIDENCE_CSS = {
    CONFIDENCE_HIGH: "c高",
    CONFIDENCE_MEDIUM: "c中",
    CONFIDENCE_LOW: "c低",
    CONFIDENCE_NONE: "c低",
}


@dataclass(frozen=True)
class RibbonSeries:
    """需要リボンに描く 2 本の系列。"""

    days: list[date]
    area: list[float]
    own: list[float]


def build_ribbon(
    area_series: dict[date, float],
    own_series: dict[date, float],
    start: date,
    end: date,
    window: int = RIBBON_WINDOW,
) -> RibbonSeries:
    """エリア合計と自社の移動合計を組み立てる。"""
    days = date_range(start, end)
    return RibbonSeries(
        days=days,
        area=rolling_sum(area_series, days, window),
        own=rolling_sum(own_series, days, window),
    )


def render_dashboard(
    config: Config,
    market: MarketEstimate,
    review_rate: ReviewRate,
    backtest: Backtest,
    signals: list[Signal],
    verdict: Verdict,
    ribbon: RibbonSeries,
) -> str:
    """ダッシュボード全体の HTML を返す。"""
    return _PAGE.format(
        title=esc(f"{config.area_name} {market.own.prop.name} ｜ レベニュー・インテリジェンス"),
        css=_CSS,
        area_name=esc(config.area_name),
        own_name=esc(market.own.prop.name),
        as_of=market.as_of.isoformat(),
        window_days=market.window_days,
        window_start=market.window_start.isoformat(),
        window_end=market.window_end.isoformat(),
        verdict=_render_verdict(verdict),
        ribbon=_render_ribbon(ribbon, config.events),
        table=_render_table(market),
        foundation=_render_foundation(config, market, review_rate, backtest),
        signals=_render_signals(signals),
        footer=_render_footer(config, market, review_rate),
    )


# --------------------------------------------------------------------------- #
# セクション
# --------------------------------------------------------------------------- #


def _render_verdict(verdict: Verdict) -> str:
    return (
        f'<div class="verdict {verdict.css_class}">\n'
        f'    <div class="lvl">{esc(verdict.action)}</div>\n'
        f"    <h3>{esc(verdict.headline)}</h3>\n"
        f"    <p>{esc(verdict.body)}</p>\n"
        f"  </div>"
    )


def _render_ribbon(ribbon: RibbonSeries, events: list[Event]) -> str:
    if not ribbon.days:
        return '<div class="ribbon"><p class="sub">描画できる観測日がまだない。</p></div>'

    axis_max = _nice_ceiling(max(ribbon.area, default=0.0))
    parts: list[str] = [
        f'<svg viewBox="0 0 {_SVG_WIDTH} {_SVG_HEIGHT}" preserveAspectRatio="none" '
        f'role="img" aria-label="エリア需要と自社シェアの推移">'
    ]

    # 目盛り線（0 は基線と重なるので描かない）
    for value in _gridline_values(axis_max):
        y = _y(value, axis_max)
        parts.append(
            f'<line x1="{_PLOT_LEFT:.0f}" y1="{y:.1f}" x2="{_PLOT_RIGHT:.0f}" y2="{y:.1f}" '
            f'stroke="#2E3B33" stroke-width="1"/>'
            f'<text x="{_PLOT_LEFT - 8:.0f}" y="{y + 4:.1f}" fill="#8A9689" font-size="9" '
            f'font-family="monospace" text-anchor="end">{value:g}</text>'
        )

    area_path = _area_path(ribbon.days, ribbon.area, axis_max)
    parts.append(f'<path d="{area_path}" fill="#7FA891" opacity=".28"/>')
    parts.append(
        f'<path d="{area_path}" fill="none" stroke="#7FA891" stroke-width="1.4" opacity=".7"/>'
    )
    parts.append(
        f'<path d="{_area_path(ribbon.days, ribbon.own, axis_max)}" '
        f'fill="#C6A252" opacity=".85"/>'
    )

    # 需要イベントの縦線
    first, last = ribbon.days[0], ribbon.days[-1]
    for event in events:
        if not first <= event.date <= last:
            continue
        x = _x(ribbon.days.index(event.date), len(ribbon.days))
        parts.append(
            f'<line x1="{x:.1f}" y1="{_PLOT_TOP:.0f}" x2="{x:.1f}" y2="{_PLOT_BASE:.0f}" '
            f'stroke="#C04A3C" stroke-width="1" stroke-dasharray="2 3" opacity=".8">'
            f"<title>{esc(event.name)}</title></line>"
        )

    labels = _x_label_positions(ribbon.days)
    for position, (index, day) in enumerate(labels):
        x = _x(index, len(ribbon.days))
        # 両端は中央揃えのままだと viewBox の外にはみ出して切れる。
        if position == 0:
            anchor = "start"
        elif position == len(labels) - 1:
            anchor = "end"
        else:
            anchor = "middle"
        parts.append(
            f'<text x="{x:.1f}" y="182" fill="#8A9689" font-size="9" '
            f'font-family="monospace" text-anchor="{anchor}">{day.strftime("%m-%d")}</text>'
        )

    parts.append(
        f'<line x1="{_PLOT_LEFT:.0f}" y1="{_PLOT_BASE:.0f}" x2="{_PLOT_RIGHT:.0f}" '
        f'y2="{_PLOT_BASE:.0f}" stroke="#2E3B33"/>'
    )
    parts.append("</svg>")

    return (
        '<div class="ribbon">' + "".join(parts) + "\n"
        '    <div class="legend">\n'
        '      <span><i style="background:#7FA891;opacity:.45"></i>エリア合計</span>\n'
        '      <span><i style="background:#C6A252"></i>自社</span>\n'
        '      <span><i style="background:#C04A3C"></i>需要イベント</span>\n'
        "    </div>\n"
        "  </div>"
    )


def _render_table(market: MarketEstimate) -> str:
    ranked = market.ranked
    max_occupancy = max(
        (e.occupancy for e in ranked if e.occupancy is not None), default=0.0
    )

    rows = [_render_row(e, max_occupancy, market) for e in ranked]

    area = market.area_momentum_pct
    if area is None:
        area_note = (
            '<p class="sub">前期の観測が揃っていないため、市場全体の増減と'
            "比較できない。前期比は季節性を含んだ生の値。</p>"
        )
    else:
        area_note = (
            f'<p class="sub">同期間のエリア全体は <b>{area:+.0f}%</b>。'
            "前期比は季節性をそのまま含むので、この市場全体の増減との差で読む。"
            "括弧内が市場調整後の値。</p>"
        )

    return (
        "<table>\n"
        "    <thead><tr><th>施設</th><th class=\"num\">室数</th>"
        "<th>推定稼働（直近{days}日）</th>\n"
        "    <th class=\"num\">推定販売室数</th><th class=\"num\">前期比（市場調整後）</th>"
        "<th class=\"conf\">信頼度</th></tr></thead>\n"
        "    <tbody>{rows}</tbody>\n"
        "  </table>\n"
        "  {area_note}"
    ).format(days=market.window_days, rows="".join(rows), area_note=area_note)


def _render_row(
    estimate: PropertyEstimate, max_occupancy: float, market: MarketEstimate
) -> str:
    prop = estimate.prop
    own_class = " own" if prop.is_own else ""
    tag = '<span class="tag">自社</span>' if prop.is_own else ""

    if estimate.occupancy_pct is None:
        bar = '<td class="bar"><span style="width:0%"></span><em>—</em></td>'
        sold = "—"
    else:
        width = (estimate.occupancy / max_occupancy * 100) if max_occupancy > 0 else 0.0
        bar = (
            f'<td class="bar"><span style="width:{width:.1f}%"></span>'
            f"<em>{estimate.occupancy_pct:.0f}%</em></td>"
        )
        sold = f"{estimate.sold_rooms:.0f}"

    momentum = estimate.momentum_pct
    adjusted = market.market_adjusted_momentum_pct(estimate)
    if momentum is None:
        momentum_text, momentum_class = "—", ""
    else:
        momentum_text = f"{momentum:+.0f}%"
        if adjusted is not None:
            momentum_text += f" <small>({adjusted:+.0f})</small>"
        # 色は市場調整後で付ける。季節で全施設が揃って落ちる月に、
        # 全行が赤くなって「危機」に見えるのを避ける。
        signal = adjusted if adjusted is not None else momentum
        if signal >= MOMENTUM_HIGHLIGHT_PCT:
            momentum_class = "pos"
        elif signal <= -MOMENTUM_HIGHLIGHT_PCT:
            momentum_class = "neg"
        else:
            momentum_class = ""

    confidence_class = _CONFIDENCE_CSS.get(estimate.confidence, "c低")
    confidence_text = esc(estimate.confidence)
    if math.isfinite(estimate.relative_margin_pt):
        confidence_text += f" ±{estimate.relative_margin_pt:.0f}"

    return (
        f'\n        <tr class="{own_class.strip()}">\n'
        f'          <td class="nm">{esc(prop.name)}{tag}</td>\n'
        f'          <td class="num">{prop.rooms}</td>\n'
        f"          {bar}\n"
        f'          <td class="num">{sold}</td>\n'
        f'          <td class="num {momentum_class}">{momentum_text}</td>\n'
        f'          <td class="conf {confidence_class}">{confidence_text}</td>\n'
        f"        </tr>"
    )


def _render_foundation(
    config: Config,
    market: MarketEstimate,
    review_rate: ReviewRate,
    backtest: Backtest,
) -> str:
    own = market.own
    floor = config.pricing_rules.rating_floor

    if review_rate.is_measured:
        rate_caption = (
            f'<small>95%CI {review_rate.ci_low * 100:.1f}〜{review_rate.ci_high * 100:.1f}%</small>'
        )
        rate_rows = (
            f'<div class="kv"><span>算定方法</span><b>自社実績で実測</b></div>\n'
            f'      <div class="kv"><span>突合日数</span><b>{review_rate.matched_days}日</b></div>\n'
            f'      <div class="kv"><span>突合販売室数</span>'
            f"<b>{review_rate.matched_rooms_sold:.0f}室</b></div>\n"
            f'      <div class="kv"><span>突合レビュー</span>'
            f"<b>{review_rate.matched_reviews:.1f}件</b></div>"
        )
        rate_note = "自社 PMS 実績で実測したレビュー投稿率。競合にも同率を適用する。"
    else:
        rate_caption = "<small>実測できず</small>"
        rate_rows = (
            '<div class="kv"><span>算定方法</span><b>固定値（未実測）</b></div>\n'
            '      <div class="kv"><span>突合日数</span><b>—</b></div>'
        )
        rate_note = (
            "自社実績と突合できていないため、config の固定値を使っている。"
            "この状態の推定値は水準の議論に使えない。相対比較にとどめること。"
        )

    if backtest.n:
        backtest_rows = (
            f'<div class="kv"><span>実績との平均誤差</span>'
            f"<b>{backtest.mean_abs_error_pt:.1f}pt（n={backtest.n}）</b></div>"
            f'<div class="kv"><span>使ってよい範囲</span>'
            f"<b>{esc(backtest.usability)}</b></div>"
        )
    else:
        backtest_rows = (
            '<div class="kv"><span>実績との平均誤差</span><b>未検証</b></div>'
        )

    events = "".join(
        f"<li><time>{event.date.isoformat()}</time><span>{esc(event.name)}</span>"
        f"<em>+{event.lift}</em></li>"
        for event in sorted(config.events, key=lambda e: e.date)
    ) or '<li class="empty">登録された需要イベントがない。</li>'

    rating_text = "—" if own.rating is None else f"{own.rating:.2f}"

    return (
        '<div class="cols">\n'
        '    <div class="card">\n'
        "      <h3>レビュー投稿率</h3>\n"
        f'      <div class="big">{review_rate.rate * 100:.1f}%{rate_caption}</div>\n'
        '      <div style="height:18px"></div>\n'
        f"      {rate_rows}\n"
        f"      {backtest_rows}\n"
        f'      <p class="note">{esc(rate_note)}</p>\n'
        "    </div>\n"
        '    <div class="card">\n'
        "      <h3>エリア内シェアと需要暦</h3>\n"
        f'      <div class="big">{market.own_share * 100:.1f}%'
        "<small>クチコミ総量に占める自社比率</small></div>\n"
        '      <div style="height:18px"></div>\n'
        f'      <div class="kv"><span>エリア合計</span><b>{market.area_reviews:.0f}件</b></div>\n'
        f'      <div class="kv"><span>自社</span><b>{market.own_reviews:.0f}件</b></div>\n'
        f'      <div class="kv"><span>自社評価</span>'
        f"<b>{rating_text} ／ 下限 {floor:g}</b></div>\n"
        '      <div style="height:14px"></div>\n'
        f'      <ul class="ev">{events}</ul>\n'
        "    </div>\n"
        "  </div>"
    )


def _render_signals(signals: list[Signal]) -> str:
    if not signals:
        return (
            '<ul class="sigs"><li class="sig"><div class="sig-h">'
            '<span class="cat">なし</span><span class="lvl">平常</span></div>'
            "<h4>ルール逸脱なし</h4><p>人の介入は不要。自動ルールのまま運用する。</p>"
            "</li></ul>"
        )

    items = "".join(
        f'\n        <li class="sig {signal.css_class}">\n'
        f'          <div class="sig-h"><span class="cat">{esc(signal.category)}</span>\n'
        f'          <span class="lvl">{esc(signal.level)}</span></div>\n'
        f"          <h4>{esc(signal.title)}</h4><p>{esc(signal.body)}</p>\n"
        f"        </li>"
        for signal in signals
    )
    return f'<ul class="sigs">{items}</ul>'


def _render_footer(config: Config, market: MarketEstimate, review_rate: ReviewRate) -> str:
    own = market.own.prop
    est = config.estimation
    rooms = own.rooms
    steps = " / ".join(f"{i / rooms * 100:.0f}" for i in range(rooms + 1))

    limits = [
        "Google Places API は競合施設の宿泊料金を返さない。本ダッシュボードが推定するのは"
        "<strong>需要</strong>であり、<strong>レート</strong>ではない。実レートは正規ライセンスの"
        "レートショッパー（メトロエンジン等）で別途取得し、この需要判断と突き合わせる。",
        "OTA 画面・Google ホテル検索画面のスクレイピングは規約違反であり、本システムには"
        "実装していない。時系列は毎日の userRatingCount 観測の差分だけで組み立てている。",
        f"レビューは滞在から平均 {est.review_lag_days} 日遅れて投稿されるものとして滞在日に"
        f"引き戻している。投稿遅延の分布は施設ごとに異なるため、直近 "
        f"{est.review_lag_days + est.unstable_tail_days} 日は窓から除外している。",
        "オーベルジュ・レストラン併設施設は外来客のレビューが混在する。"
        "config.json の restaurant_review_share で控除しているが、これは推定値であり"
        "感度分析が必要。",
        f"全 {rooms} 室という母数では日次の稼働率は {steps}% しか取らない。"
        f"本手法は {market.window_days} 日窓の集計でのみ意味を持つ。日次の需要予測には使えない。",
        "前期比は 1 つ前の同じ長さの窓との比較であり、季節性をそのまま含む。"
        "奈良は桜と紅葉で市場が大きく動くため、単独では競争力の指標にならない。"
        "括弧内の市場調整後の値で読むこと。前年同期比には 15 か月ぶんの観測が要る。",
        "レビュー投稿率は自社実績から実測した 1 つの値を全施設に当てている。"
        "投稿率が施設ごとに違えば推定は系統的にずれる。順位の議論には耐えるが、"
        "施設間の数 pt 差を有意とみなさないこと。",
        "推定稼働率の絶対値を投資家向け資料やオーナー報告にそのまま転載しないこと。"
        "用途は自社の値付け判断に限る。",
    ]

    if not review_rate.is_measured:
        limits.insert(
            0,
            "<strong>投稿率が未実測のまま生成されている。</strong>"
            "下の数値は水準として読めない。自社 PMS 実績を突合してから使うこと。",
        )

    items = "".join(f"\n    <li>{item}</li>" for item in limits)
    return f"<h3>前提と限界</h3>\n  <ol>{items}\n  </ol>"


# --------------------------------------------------------------------------- #
# 図形ユーティリティ
# --------------------------------------------------------------------------- #


def _x(index: int, count: int) -> float:
    if count <= 1:
        return _PLOT_LEFT
    return _PLOT_LEFT + (_PLOT_RIGHT - _PLOT_LEFT) * index / (count - 1)


def _y(value: float, axis_max: float) -> float:
    if axis_max <= 0:
        return _PLOT_BASE
    ratio = min(1.0, max(0.0, value / axis_max))
    return _PLOT_BASE - (_PLOT_BASE - _PLOT_TOP) * ratio


def _area_path(days: list[date], values: list[float], axis_max: float) -> str:
    """基線から立ち上がる塗り面のパスを返す。"""
    count = len(days)
    points = " ".join(
        f"{_x(i, count):.1f},{_y(v, axis_max):.1f}" for i, v in enumerate(values)
    )
    return (
        f"M{_PLOT_LEFT:.0f},{_PLOT_BASE:.0f} L{points} "
        f"L{_PLOT_RIGHT:.1f},{_PLOT_BASE:.0f} Z"
    )


def _nice_ceiling(value: float) -> float:
    """目盛りに使える切りのよい上限を返す。"""
    if value <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(value))
    for step in (1.0, 2.0, 2.5, 5.0, 10.0):
        candidate = step * magnitude
        if candidate >= value:
            return candidate
    return 10 * magnitude


def _gridline_values(axis_max: float) -> list[float]:
    return [axis_max / 2, axis_max]


def _x_label_positions(days: list[date]) -> list[tuple[int, date]]:
    count = len(days)
    if count <= _X_LABEL_COUNT:
        return list(enumerate(days))
    return [
        (index, days[index])
        for index in (
            round(i * (count - 1) / (_X_LABEL_COUNT - 1)) for i in range(_X_LABEL_COUNT)
        )
    ]


def esc(text: str) -> str:
    return html.escape(str(text), quote=True)


# --------------------------------------------------------------------------- #
# テンプレート
# --------------------------------------------------------------------------- #

_CSS = """
:root{
  --ground:#141C17; --panel:#1D2822; --line:#2E3B33;
  --text:#D6DBD2; --muted:#8A9689; --brass:#C6A252;
  --moss:#7FA891; --shu:#C04A3C;
  --mincho:"Hiragino Mincho ProN","Yu Mincho",YuMincho,"Noto Serif JP",serif;
  --gothic:"Hiragino Sans","Yu Gothic","Noto Sans JP",system-ui,sans-serif;
  --mono:ui-monospace,"SF Mono","Roboto Mono",Menlo,monospace;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--text);font-family:var(--gothic);
  font-size:15px;line-height:1.7;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:40px 22px 80px}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.22em;color:var(--brass)}
h1{font-family:var(--mincho);font-weight:400;font-size:clamp(30px,6vw,50px);
  margin:.25em 0 .1em;letter-spacing:.06em}
h1 small{display:block;font-size:14px;letter-spacing:.3em;color:var(--muted);
  font-family:var(--mono);margin-top:14px}
.rule{height:1px;background:linear-gradient(90deg,var(--brass),transparent);margin:30px 0 44px}

section{margin:0 0 54px}
h2{font-family:var(--mincho);font-weight:400;font-size:21px;letter-spacing:.1em;
  margin:0 0 4px;display:flex;align-items:baseline;gap:14px}
h2 .ix{font-family:var(--mono);font-size:11px;color:var(--brass);letter-spacing:.2em;
  margin-right:12px;flex:0 0 auto}
.sub{color:var(--muted);font-size:13px;margin:0 0 20px}

/* 判断 */
.verdict{background:var(--panel);border-left:3px solid var(--brass);padding:26px 28px;
  border-radius:2px}
.verdict .lvl{font-family:var(--mono);font-size:11px;letter-spacing:.2em;color:var(--brass)}
.verdict h3{font-family:var(--mincho);font-weight:400;font-size:clamp(20px,4vw,29px);
  margin:.3em 0 .4em;line-height:1.45}
.verdict p{margin:0;color:var(--muted);max-width:64ch}
.verdict.alert{border-left-color:var(--shu)} .verdict.alert .lvl{color:var(--shu)}
.verdict.up{border-left-color:var(--moss)} .verdict.up .lvl{color:var(--moss)}

/* 需要リボン */
.ribbon{background:var(--panel);border-radius:2px;padding:22px 18px 12px;overflow-x:auto}
.ribbon svg{display:block;min-width:640px;width:100%;height:190px}
.legend{display:flex;gap:22px;font-family:var(--mono);font-size:11px;color:var(--muted);
  padding:12px 4px 0;flex-wrap:wrap}
.legend i{display:inline-block;width:22px;height:3px;margin-right:7px;vertical-align:middle}

/* 表 */
table{width:100%;border-collapse:collapse;font-size:14px}
th{font-family:var(--mono);font-size:10px;letter-spacing:.14em;color:var(--muted);
  text-align:left;font-weight:400;padding:0 10px 10px;border-bottom:1px solid var(--line)}
th.num,td.num,th.conf,td.conf{text-align:right}
td{padding:14px 10px;border-bottom:1px solid var(--line);vertical-align:middle}
tr.own td{background:rgba(198,162,82,.06)}
td.nm{font-family:var(--mincho);font-size:15px}
.tag{font-family:var(--mono);font-size:9px;letter-spacing:.14em;color:var(--ground);
  background:var(--brass);padding:2px 7px;margin-left:10px;border-radius:2px;vertical-align:1px}
td.num{font-family:var(--mono)}
td.num.pos{color:var(--moss)} td.num.neg{color:var(--shu)}
td.num small{font-size:10px;opacity:.7;margin-left:3px}
td.bar{position:relative;min-width:210px;padding-right:56px}
td.bar span{display:block;height:8px;background:var(--moss);opacity:.55;border-radius:1px}
tr.own td.bar span{background:var(--brass);opacity:.9}
td.bar em{font-family:var(--mono);font-style:normal;font-size:12px;color:var(--text);
  position:absolute;right:10px;top:50%;transform:translateY(-50%);width:40px;text-align:right}
td.conf{font-family:var(--mono);font-size:11px}
.c高{color:var(--moss)} .c中{color:var(--brass)} .c低{color:var(--shu)}

/* 2カラム */
.cols{display:grid;grid-template-columns:1fr 1fr;gap:22px}
@media(max-width:760px){.cols{grid-template-columns:1fr}}
.card{background:var(--panel);padding:24px;border-radius:2px}
.card h3{font-family:var(--mono);font-size:10px;letter-spacing:.18em;color:var(--muted);
  margin:0 0 16px;font-weight:400}
.big{font-family:var(--mono);font-size:38px;color:var(--brass);line-height:1}
.big small{font-size:13px;color:var(--muted);margin-left:8px}
.kv{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--line);
  font-size:13px}
.kv:last-of-type{border-bottom:0}
.kv b{font-family:var(--mono);font-weight:400}
.card p.note{color:var(--muted);font-size:12px;margin:16px 0 0;line-height:1.65}

/* シグナル */
ul.sigs{list-style:none;padding:0;margin:0;display:grid;gap:12px}
.sig{background:var(--panel);padding:20px 22px;border-radius:2px;border-left:2px solid var(--moss)}
.sig.alert{border-left-color:var(--shu)} .sig.up,.sig.watch{border-left-color:var(--brass)}
.sig-h{display:flex;gap:12px;align-items:center;font-family:var(--mono);font-size:10px;
  letter-spacing:.16em}
.sig-h .cat{color:var(--muted)} .sig-h .lvl{color:var(--brass)}
.sig.alert .sig-h .lvl{color:var(--shu)}
.sig h4{font-family:var(--mincho);font-weight:400;font-size:17px;margin:.5em 0 .35em}
.sig p{margin:0;color:var(--muted);font-size:13.5px}

/* イベント */
ul.ev{list-style:none;padding:0;margin:0}
ul.ev li{display:flex;gap:14px;align-items:baseline;padding:9px 0;border-bottom:1px solid var(--line);
  font-size:13px}
ul.ev time{font-family:var(--mono);font-size:11px;color:var(--brass);min-width:82px}
ul.ev em{margin-left:auto;font-family:var(--mono);font-style:normal;color:var(--muted);font-size:11px}
ul.ev li.empty{color:var(--muted);display:block}

footer{margin-top:70px;padding-top:26px;border-top:1px solid var(--line);
  color:var(--muted);font-size:12px;line-height:1.85}
footer h3{font-family:var(--mono);font-size:10px;letter-spacing:.18em;color:var(--brass);
  font-weight:400;margin:0 0 12px}
footer ol{padding-left:1.2em;margin:0}
footer li{margin-bottom:6px}
@media(prefers-reduced-motion:no-preference){
  .verdict,.ribbon,.card,.sig{animation:rise .5s ease both}
  @keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
}
"""

_PAGE = """<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>{css}</style></head><body><div class="wrap">

<header>
  <div class="eyebrow">dhp都市開発グループ ／ Revenue Intelligence</div>
  <h1>{own_name}<small>{as_of} 時点 ・ 直近{window_days}日 ・ Google Places 由来</small></h1>
  <div class="rule"></div>
</header>

<section>
  <h2><span class="ix">01</span>今日の判断</h2>
  {verdict}
</section>

<section>
  <h2><span class="ix">02</span>需要リボン</h2>
  <p class="sub">エリア全体のクチコミ発生量（7日移動合計）と、そのうち自社が占める分。
  面積が市場の大きさ、金色の帯が自社の取り分。帯が細るときはシェアを落としている。</p>
  {ribbon}
</section>

<section>
  <h2><span class="ix">03</span>コンプセット推定稼働</h2>
  <p class="sub">レビュー増分 ÷ 投稿率 ÷ 客室数。絶対値ではなく施設間の相対差とモメンタムを読む。
  集計対象は滞在日 {window_start} 〜 {window_end}。信頼度の ± は施設間比較に使える
  95% 区間の半幅（ポイント）で、この幅より小さい差は誤差と区別できない。</p>
  {table}
</section>

<section>
  <h2><span class="ix">04</span>推定の土台</h2>
  <p class="sub">この数字が信用できるかどうかは、自社実績とのキャリブレーションで決まる。</p>
  {foundation}
</section>

<section>
  <h2><span class="ix">05</span>シグナル</h2>
  <p class="sub">今日の判断に付随して確認する項目。ルール逸脱時のみ人が介入する。</p>
  {signals}
</section>

<footer>
  {footer}
</footer>

</div></body></html>
"""
