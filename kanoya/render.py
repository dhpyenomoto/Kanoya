"""ダッシュボードの HTML 出力。

依存ライブラリを増やさないため、テンプレートエンジンは使わない。
外部由来の文字列（施設名など）は必ず escape を通す。
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .calibrate import BacktestResult, PostingRate
from .config import Config, DemandEvent
from .estimate import AreaSeries, PropertyEstimate, Window, compset_median
from .verdict import Signal, Verdict

# --- リボンの座標系 -------------------------------------------------------
SVG_W, SVG_H = 900, 190
PLOT_L, PLOT_R = 34, 892
PLOT_T, PLOT_B = 12, 164
LABEL_Y = 182
X_LABELS = 7

CSS = """
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
""".strip()


@dataclass(frozen=True)
class Report:
    """レンダラに渡す一式。組み立ては pipeline.py が行う。"""

    config: Config
    as_of: date
    window: Window
    estimates: list[PropertyEstimate]
    verdict: Verdict
    signals: list[Signal]
    series: AreaSeries
    rate: PostingRate
    backtest: BacktestResult | None
    rating: float | None
    own_review_total: int | None


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _nice_max(value: float) -> float:
    """グリッド線が半分の位置に来るような、切りのよい上限。"""
    if value <= 0:
        return 10.0
    exponent = math.floor(math.log10(value))
    base = 10 ** exponent
    for step in (1, 2, 2.5, 5, 10):
        candidate = step * base
        if candidate >= value:
            return float(candidate)
    return float(10 * base)


def _ribbon(series: AreaSeries, events: list[DemandEvent]) -> str:
    """需要リボンの SVG。面積が市場、金の帯が自社の取り分。"""
    if len(series.days) < 2:
        return (
            '<svg viewBox="0 0 900 190" role="img" aria-label="需要リボン（データ不足）">'
            '<text x="450" y="95" fill="#8A9689" font-size="12" '
            'font-family="monospace" text-anchor="middle">'
            'スナップショットが不足しています</text></svg>'
        )

    n = len(series.days)
    vmax = _nice_max(max(series.area) if series.area else 0)
    span_x = PLOT_R - PLOT_L
    span_y = PLOT_B - PLOT_T

    def x_at(i: int) -> float:
        return PLOT_L + span_x * i / (n - 1)

    def y_at(v: float) -> float:
        return PLOT_B - min(v / vmax, 1.0) * span_y

    parts: list[str] = [
        f'<svg viewBox="0 0 {SVG_W} {SVG_H}" preserveAspectRatio="none" role="img" '
        f'aria-label="エリア需要と自社シェアの推移">'
    ]

    # グリッド（1/2 と満尺の 2 本だけ。線を増やしても読みやすくならない）
    for frac in (0.5, 1.0):
        value = vmax * frac
        y = y_at(value)
        parts.append(
            f'<line x1="{PLOT_L}" y1="{y:.1f}" x2="{PLOT_R}" y2="{y:.1f}" '
            f'stroke="#2E3B33" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{PLOT_L - 8}" y="{y + 4:.1f}" fill="#8A9689" font-size="9" '
            f'font-family="monospace" text-anchor="end">{value:g}</text>'
        )

    def area_path(values: list[float]) -> str:
        points = " ".join(f"{x_at(i):.1f},{y_at(v):.1f}" for i, v in enumerate(values))
        return f"M{PLOT_L},{PLOT_B} L{points} L{x_at(n - 1):.1f},{PLOT_B} Z"

    area_d = area_path(series.area)
    parts.append(f'<path d="{area_d}" fill="#7FA891" opacity=".28"/>')
    parts.append(
        f'<path d="{area_d}" fill="none" stroke="#7FA891" stroke-width="1.4" opacity=".7"/>'
    )
    parts.append(f'<path d="{area_path(series.own)}" fill="#C6A252" opacity=".85"/>')

    # 需要イベント（窓に入っているものだけ）
    first, last = series.days[0], series.days[-1]
    for event in events:
        if not first <= event.date <= last:
            continue
        index = (event.date - first).days
        x = x_at(index)
        parts.append(
            f'<line x1="{x:.1f}" y1="{PLOT_T}" x2="{x:.1f}" y2="{PLOT_B}" '
            f'stroke="#C04A3C" stroke-width="1" stroke-dasharray="2 3" opacity=".8">'
            f'<title>{_e(event.label)}</title></line>'
        )

    for step in range(X_LABELS):
        index = round((n - 1) * step / (X_LABELS - 1))
        day = series.days[index]
        parts.append(
            f'<text x="{x_at(index):.1f}" y="{LABEL_Y}" fill="#8A9689" font-size="9" '
            f'font-family="monospace" text-anchor="middle">{day.strftime("%m-%d")}</text>'
        )

    parts.append(
        f'<line x1="{PLOT_L}" y1="{PLOT_B}" x2="{PLOT_R}" y2="{PLOT_B}" stroke="#2E3B33"/>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _delta_cell(estimate: PropertyEstimate) -> str:
    delta = estimate.delta_pct
    if delta is None:
        return '<td class="num">—</td>'
    cls = "pos" if delta >= 5 else "neg" if delta <= -5 else ""
    # ±0.5% 未満に符号を付けると、丸めの綾が動きに見えてしまう。
    text = "±0%" if abs(delta) < 0.5 else f"{delta:+.0f}%"
    return f'<td class="num {cls}">{text}</td>'


def _table(estimates: list[PropertyEstimate], window_days: int) -> str:
    if not estimates:
        return '<p class="sub">推定できる施設がありません。</p>'
    top = max((e.occupancy for e in estimates), default=0.0) or 1.0
    rows: list[str] = []
    for estimate in estimates:
        prop = estimate.prop
        width = estimate.occupancy / top * 100
        tag = '<span class="tag">自社</span>' if prop.is_own else ""
        rows.append(
            f"""
        <tr class="{'own' if prop.is_own else ''}">
          <td class="nm">{_e(prop.label)}{tag}</td>
          <td class="num">{prop.rooms}</td>
          <td class="bar"><span style="width:{width:.1f}%"></span>"""
            f"""<em>{estimate.occupancy * 100:.0f}%</em></td>
          <td class="num">{estimate.rooms_sold:.0f}</td>
          {_delta_cell(estimate)}
          <td class="conf c{estimate.confidence}">{estimate.confidence}</td>
        </tr>"""
        )
    return f"""<table>
    <thead><tr><th>施設</th><th class="num">室数</th><th>推定稼働（直近{window_days}日）</th>
    <th class="num">推定販売室数</th><th class="num">前期比（エリア調整）</th><th class="conf">信頼度</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>"""


def _signals(signals: list[Signal]) -> str:
    if not signals:
        return (
            '<ul class="sigs"><li class="sig"><h4>ルール逸脱なし</h4>'
            "<p>介入を要する項目はありません。</p></li></ul>"
        )
    items = "".join(
        f"""
        <li class="sig {sig.tone}">
          <div class="sig-h"><span class="cat">{_e(sig.category)}</span>
          <span class="lvl">{_e(sig.level)}</span></div>
          <h4>{_e(sig.title)}</h4><p>{_e(sig.body)}</p>
        </li>"""
        for sig in signals
    )
    return f'<ul class="sigs">{items}</ul>'


def _events(events: list[DemandEvent]) -> str:
    if not events:
        return '<ul class="ev"><li class="empty">需要イベントが未登録です。</li></ul>'
    items = "".join(
        f"<li><time>{event.date.isoformat()}</time><span>{_e(event.label)}</span>"
        f"<em>+{event.lift}</em></li>"
        for event in events
    )
    return f'<ul class="ev">{items}</ul>'


def _rate_card(report: Report) -> str:
    rate = report.rate
    backtest = report.backtest
    if rate.measured:
        note = "自社PMS実績で実測したレビュー投稿率。競合にも同率を適用する。"
    else:
        note = (
            "自社実績が無いためフォールバック値を使用中。"
            "この状態で稼働率の絶対値を読んではいけない。"
        )
    if backtest is not None:
        accuracy = (
            f'<div class="kv"><span>実績との平均誤差</span>'
            f"<b>{backtest.mean_abs_error_pt:.1f}pt（n={backtest.windows}）</b></div>"
            f'<div class="kv"><span>使ってよい範囲</span>'
            f"<b>{_e(backtest.usage_label)}</b></div>"
        )
    else:
        accuracy = '<div class="kv"><span>実績との平均誤差</span><b>未検証</b></div>'

    return f"""<div class="card">
      <h3>レビュー投稿率</h3>
      <div class="big">{rate.rate * 100:.1f}%<small>95%CI {rate.low * 100:.1f}〜{rate.high * 100:.1f}%</small></div>
      <div style="height:18px"></div>
      <div class="kv"><span>算定方法</span><b>{_e(rate.source_label)}</b></div>
      <div class="kv"><span>突合日数</span><b>{rate.matched_days}日</b></div>
      <div class="kv"><span>突合販売室数</span><b>{rate.matched_rooms_sold}室</b></div>
      <div class="kv"><span>突合レビュー</span><b>{rate.matched_reviews:.1f}件</b></div>
      {accuracy}
      <p class="note">{_e(note)}</p>
    </div>"""


def _share_card(report: Report) -> str:
    series = report.series
    area_total = sum(series.area)
    own_total = sum(series.own)
    rating = report.rating
    floor = report.config.decision.rating_floor
    rating_text = f"{rating:.2f} ／ 下限 {floor:.1f}" if rating is not None else "未取得"
    return f"""<div class="card">
      <h3>エリア内シェアと需要暦</h3>
      <div class="big">{series.own_share * 100:.1f}%<small>クチコミ総量に占める自社比率</small></div>
      <div style="height:18px"></div>
      <div class="kv"><span>エリア合計</span><b>{area_total / 7:.0f}件</b></div>
      <div class="kv"><span>自社</span><b>{own_total / 7:.0f}件</b></div>
      <div class="kv"><span>自社評価</span><b>{_e(rating_text)}</b></div>
      <div style="height:14px"></div>
      {_events(report.config.demand_events)}
    </div>"""


FOOTER_NOTES = [
    "Google Places API は競合施設の宿泊料金を返さない。本ダッシュボードが推定するのは"
    "<strong>需要</strong>であり、<strong>レート</strong>ではない。実レートは正規ライセンスの"
    "レートショッパー（メトロエンジン等）で別途取得し、この需要判断と突き合わせる。",
    "OTA画面・Googleホテル検索画面のスクレイピングは規約違反であり、本システムには実装していない。",
    "Places API が返すレビュー本文は 1 施設あたり最大 5 件。速度の測定に使えるのは"
    "userRatingCount（累計件数）の日次差分だけであり、台帳が無い期間は遡って復元できない。",
    "レビューは滞在から平均{lag}日遅れて投稿されるものとして滞在日に引き戻している。"
    "投稿遅延の分布は施設ごとに異なるため、直近{tail}日の推定値は不安定として窓から外している。",
    "オーベルジュ・レストラン併設施設は外来客のレビューが混在する。"
    "config.json の restaurant_review_share で控除しているが、これは推定値であり感度分析が必要。",
    "全{rooms}室という母数では日次の稼働率は{grid}しか取らない。"
    "本手法は{window}日窓の集計でのみ意味を持つ。日次の需要予測には使えない。",
    "推定稼働率の絶対値を投資家向け資料やオーナー報告にそのまま転載しないこと。"
    "用途は自社の値付け判断に限る。",
]


def _footer(config: Config) -> str:
    rooms = config.own.rooms
    grid = "/".join(f"{i * 100 // rooms}" for i in range(rooms + 1)) + "%"
    notes = [
        note.format(
            lag=config.estimation.review_lag_days,
            tail=config.estimation.unstable_tail_days,
            rooms=rooms,
            grid=grid,
            window=config.estimation.window_days,
        )
        for note in FOOTER_NOTES
    ]
    items = "".join(f"<li>{note}</li>" for note in notes)
    return f"""<footer>
  <h3>前提と限界</h3>
  <ol>{items}</ol>
</footer>"""


def render(report: Report) -> str:
    config = report.config
    window = report.window
    verdict = report.verdict
    median = compset_median(report.estimates)
    median_text = f"{median * 100:.0f}%" if median is not None else "—"

    subtitle = (
        f"{report.as_of.isoformat()} 時点 ・ "
        f"{window.start.isoformat()}〜{window.end.isoformat()} ・ Google Places 由来"
    )

    return f"""<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(config.own.label)} ｜ レベニュー・インテリジェンス</title>
<style>
{CSS}
</style></head><body><div class="wrap">

<header>
  <div class="eyebrow">dhp都市開発グループ ／ Revenue Intelligence</div>
  <h1>{_e(config.own.label)}<small>{_e(subtitle)}</small></h1>
  <div class="rule"></div>
</header>

<section>
  <h2><span class="ix">01</span>今日の判断</h2>
  <div class="verdict {verdict.tone}">
    <div class="lvl">{_e(verdict.level)}</div>
    <h3>{_e(verdict.headline)}</h3>
    <p>{_e(verdict.detail)}</p>
  </div>
</section>

<section>
  <h2><span class="ix">02</span>需要リボン</h2>
  <p class="sub">{_e(config.area_label)}全体のクチコミ発生量（7日移動合計）と、そのうち{_e(config.own.label)}が占める分。
  面積が市場の大きさ、金色の帯が自社の取り分。帯が細るときはシェアを落としている。</p>
  <div class="ribbon">{_ribbon(report.series, config.demand_events)}
    <div class="legend">
      <span><i style="background:#7FA891;opacity:.45"></i>エリア合計</span>
      <span><i style="background:#C6A252"></i>自社</span>
      <span><i style="background:#C04A3C"></i>需要イベント</span>
    </div>
  </div>
</section>

<section>
  <h2><span class="ix">03</span>コンプセット推定稼働</h2>
  <p class="sub">レビュー増分 ÷ 投稿率 ÷ 客室数。窓は {window.days} 日（{window.start.isoformat()}〜{window.end.isoformat()}）。
  競合中央値は {median_text}。前期比はエリア全体の季節変動を差し引いた値で、市場に対して取れているかを示す。
  絶対値ではなく施設間の相対差とモメンタムを読む。</p>
  {_table(report.estimates, window.days)}
</section>

<section>
  <h2><span class="ix">04</span>推定の土台</h2>
  <p class="sub">この数字が信用できるかどうかは、自社実績とのキャリブレーションで決まる。</p>
  <div class="cols">
    {_rate_card(report)}
    {_share_card(report)}
  </div>
</section>

<section>
  <h2><span class="ix">05</span>シグナル</h2>
  <p class="sub">今日の判断に付随して確認する項目。ルール逸脱時のみ人が介入する。</p>
  {_signals(report.signals)}
</section>

{_footer(config)}

</div></body></html>
"""


def write(report: Report, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(report), encoding="utf-8")
    return path
