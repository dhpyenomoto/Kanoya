"""ダッシュボードのHTML生成。

画面の目的は「眺めること」ではなく「今日どうするかを1行で決めること」。
上から、判断 → 需要の全体像 → 施設別の相対位置 → 推定の土台 → 例外シグナル、の順。
"""

from __future__ import annotations

import datetime as dt
import html
import math
from dataclasses import dataclass

from .config import Config, Event
from .estimate import Report
from .signals import Signal, Verdict

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
td.num.ns{color:var(--muted)}
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

# リボンの描画座標系（viewBox 0 0 900 190）
X0, X1 = 34.0, 892.0
BASE_Y, TOP_Y = 164.0, 12.0


def _e(s: object) -> str:
    return html.escape(str(s), quote=True)


def _round(x: float) -> int:
    """四捨五入（Python 既定の銀行家丸めを使わない）。"""
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def _pct(x: float) -> str:
    return f"{_round(x * 100)}%"


def _axis_max(peak: float) -> float:
    if peak <= 0:
        return 10.0
    step = 50.0 if peak > 40 else (10.0 if peak > 8 else 2.0)
    return math.ceil(peak / step) * step


def _ribbon_svg(
    dates: tuple[dt.date, ...],
    area: tuple[float, ...],
    own: tuple[float, ...],
    events: tuple[Event, ...],
) -> str:
    n = len(dates)
    if n < 2:
        return '<svg viewBox="0 0 900 190" role="img" aria-label="需要データ不足"></svg>'

    axis = _axis_max(max(area) if area else 0.0)
    step = (X1 - X0) / (n - 1)
    scale = (BASE_Y - TOP_Y) / axis

    def x_at(i: int) -> float:
        return X0 + i * step

    def y_at(v: float) -> float:
        return max(TOP_Y, BASE_Y - v * scale)

    def path(values: tuple[float, ...]) -> str:
        pts = " ".join(f"{x_at(i):.1f},{y_at(v):.1f}" for i, v in enumerate(values))
        return f"M{X0:.0f},{BASE_Y:.0f} L{pts} L{X1:.1f},{BASE_Y:.0f} Z"

    parts: list[str] = [
        '<svg viewBox="0 0 900 190" preserveAspectRatio="none" role="img"'
        ' aria-label="エリア需要と自社シェアの推移">'
    ]
    for grid in (axis / 2, axis):
        gy = y_at(grid)
        parts.append(
            f'<line x1="{X0:.0f}" y1="{gy:.1f}" x2="{X1:.0f}" y2="{gy:.1f}"'
            ' stroke="#2E3B33" stroke-width="1"/>'
            f'<text x="{X0 - 8:.0f}" y="{gy + 4:.1f}" fill="#8A9689" font-size="9"'
            f' font-family="monospace" text-anchor="end">{_round(grid)}</text>'
        )

    parts.append(f'<path d="{path(area)}" fill="#7FA891" opacity=".28"/>')
    parts.append(f'<path d="{path(area)}" fill="none" stroke="#7FA891" stroke-width="1.4" opacity=".7"/>')
    parts.append(f'<path d="{path(own)}" fill="#C6A252" opacity=".85"/>')

    index = {d: i for i, d in enumerate(dates)}
    for ev in events:
        if ev.date in index:
            ex = x_at(index[ev.date])
            parts.append(
                f'<line x1="{ex:.1f}" y1="{TOP_Y:.0f}" x2="{ex:.1f}" y2="{BASE_Y:.0f}"'
                ' stroke="#C04A3C" stroke-width="1" stroke-dasharray="2 3" opacity=".8">'
                f"<title>{_e(ev.name)}</title></line>"
            )

    ticks = list(range(0, n, 30))
    if ticks[-1] != n - 1:
        ticks.append(n - 1)
    for i in ticks:
        # 両端はビューボックスからはみ出して欠けるので、内側に寄せる
        anchor = "start" if i == 0 else ("end" if i == n - 1 else "middle")
        parts.append(
            f'<text x="{x_at(i):.1f}" y="182" fill="#8A9689" font-size="9"'
            f' font-family="monospace" text-anchor="{anchor}">{dates[i]:%m-%d}</text>'
        )

    parts.append(f'<line x1="{X0:.0f}" y1="{BASE_Y:.0f}" x2="{X1:.0f}" y2="{BASE_Y:.0f}" stroke="#2E3B33"/>')
    parts.append("</svg>")
    return "".join(parts)


def _table(report: Report) -> str:
    peak = max((e.occupancy for e in report.estimates), default=0.0) or 1.0
    rows: list[str] = []
    for est in report.estimates:
        delta = est.delta_pt
        if est.delta_significant:
            cls, hint = ("pos" if delta > 0 else "neg"), "クチコミ件数の差として有意（両側5%）"
        else:
            cls, hint = "ns", "件数の揺らぎで説明できる範囲。動かす理由にしない"
        tag = '<span class="tag">自社</span>' if est.prop.is_own else ""
        rows.append(
            f"""        <tr class="{'own' if est.prop.is_own else ''}">
          <td class="nm">{_e(est.prop.name)}{tag}</td>
          <td class="num">{est.prop.rooms}</td>
          <td class="bar" title="推定誤差 ±{est.occupancy_se_pt * 1.96:.0f}pt（95%）"><span style="width:{est.occupancy / peak * 100:.1f}%"></span><em>{_pct(est.occupancy)}</em></td>
          <td class="num">{_round(est.sold_rooms)}</td>
          <td class="num {cls}" title="{hint}">{delta:+.0f}pt</td>
          <td class="conf c{est.confidence}">{est.confidence}</td>
        </tr>"""
        )
    return f"""  <table>
    <thead><tr><th>施設</th><th class="num">室数</th><th>推定稼働（直近{report.window.days}日）</th>
    <th class="num">推定販売室数</th><th class="num">前期比</th><th class="conf">信頼度</th></tr></thead>
    <tbody>
{chr(10).join(rows)}</tbody>
  </table>"""


def _events_list(cfg: Config, asof: dt.date) -> str:
    items = [ev for ev in cfg.events if ev.date >= asof - dt.timedelta(days=365)]
    if not items:
        return '<ul class="ev"><li class="empty">登録された需要イベントがありません。</li></ul>'
    lis = "".join(
        f"<li><time>{ev.date.isoformat()}</time><span>{_e(ev.name)}</span><em>+{ev.weight}</em></li>"
        for ev in sorted(items, key=lambda e: e.date)
    )
    return f'<ul class="ev">{lis}</ul>'


def _cards(cfg: Config, report: Report) -> str:
    cal = report.calibration
    floor = cfg.own.rating_floor
    rating = f"{report.own_rating:.2f}" if report.own_rating is not None else "—"
    floor_txt = f" ／ 下限 {floor:.1f}" if floor is not None else ""
    mae = (
        f"{cal.mean_abs_error_pt:.1f}pt（n={cal.backtest_n}）"
        if cal.mean_abs_error_pt is not None
        else "未検証"
    )
    return f"""  <div class="cols">
    <div class="card">
      <h3>レビュー投稿率</h3>
      <div class="big">{cal.rate * 100:.1f}%<small>95%CI {cal.ci_low * 100:.1f}〜{cal.ci_high * 100:.1f}%</small></div>
      <div style="height:18px"></div>
      <div class="kv"><span>算定方法</span><b>自社実績で実測</b></div>
      <div class="kv"><span>突合日数</span><b>{cal.days}日</b></div>
      <div class="kv"><span>突合販売室数</span><b>{cal.sold_rooms}室</b></div>
      <div class="kv"><span>突合レビュー</span><b>{cal.reviews_raw:.1f}件</b></div>
      <div class="kv"><span>外来控除後</span><b>{cal.reviews_effective:.1f}件</b></div>
      <div class="kv"><span>実績との平均誤差</span><b>{mae}</b></div>
      <div class="kv"><span>使ってよい範囲</span><b>{cal.usable_for}</b></div>
      <p class="note">自社PMS実績で実測したレビュー投稿率（1販売室あたり）。競合にも同率を適用する。
      外来控除は config.json の restaurant_review_share による推定。</p>
    </div>
    <div class="card">
      <h3>エリア内シェアと需要暦</h3>
      <div class="big">{report.area_share * 100:.1f}%<small>クチコミ総量に占める自社比率</small></div>
      <div style="height:18px"></div>
      <div class="kv"><span>エリア合計</span><b>{_round(report.area_reviews)}件</b></div>
      <div class="kv"><span>自社</span><b>{_round(report.own_reviews)}件</b></div>
      <div class="kv"><span>自社評価</span><b>{rating}{floor_txt}</b></div>
      <div style="height:14px"></div>
{_events_list(cfg, report.asof)}
    </div>
  </div>"""


def _signals(signals: list[Signal]) -> str:
    if not signals:
        return '  <ul class="sigs"><li class="sig"><div class="sig-h"><span class="cat">なし</span>' \
               '<span class="lvl">平常</span></div><h4>逸脱シグナルなし</h4>' \
               '<p>ルール内で運用を継続する。人の介入は不要。</p></li></ul>'
    items = "".join(
        f"""
        <li class="sig {s.tone}">
          <div class="sig-h"><span class="cat">{_e(s.category)}</span>
          <span class="lvl">{_e(s.level)}</span></div>
          <h4>{_e(s.title)}</h4><p>{_e(s.detail)}</p>
        </li>"""
        for s in signals
    )
    return f'  <ul class="sigs">{items}</ul>'


FOOTER = """  <h3>前提と限界</h3>
  <ol>
    <li>Google Places API は競合施設の宿泊料金を返さない。本ダッシュボードが推定するのは
      <strong>需要</strong>であり、<strong>レート</strong>ではない。実レートは正規ライセンスの
      レートショッパー（メトロエンジン等）で別途取得し、この需要判断と突き合わせる。</li>
    <li>OTA画面・Googleホテル検索画面のスクレイピングは規約違反であり、本システムには実装していない。</li>
    <li>レビューは滞在から平均{lag}日遅れて投稿されるものとして滞在日に引き戻している。
      投稿遅延の分布は施設ごとに異なるため、直近{tail}日の推定値は不安定。</li>
    <li>オーベルジュ・レストラン併設施設は外来客のレビューが混在する。
      config.json の restaurant_review_share で控除しているが、これは推定値であり感度分析が必要。</li>
    <li>全{rooms}室という母数では日次の稼働率は {steps} しか取らない。
      本手法は{window}日窓の集計でのみ意味を持つ。日次の需要予測には使えない。</li>
    <li>推定稼働率の絶対値を投資家向け資料やオーナー報告にそのまま転載しないこと。
      用途は自社の値付け判断に限る。</li>
  </ol>"""


def _footer(cfg: Config) -> str:
    rooms = cfg.own.rooms
    steps = "/".join(f"{_round(i / rooms * 100)}" for i in range(rooms + 1)) + "%"
    return FOOTER.format(
        lag=cfg.model.review_lag_days,
        tail=cfg.model.unstable_tail_days,
        rooms=rooms,
        steps=steps,
        window=cfg.model.window_days,
    )


def render(cfg: Config, report: Report, verdict: Verdict, signals: list[Signal]) -> str:
    return f"""<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(cfg.own.name.split()[-1])} ｜ レベニュー・インテリジェンス</title>
<style>{CSS}</style></head><body><div class="wrap">

<header>
  <div class="eyebrow">dhp都市開発グループ ／ Revenue Intelligence</div>
  <h1>{_e(cfg.own.name)}<small>{report.asof.isoformat()} 時点 ・ 直近{report.window.days}日 ・ Google Places 由来</small></h1>
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
  <p class="sub">エリア全体のクチコミ発生量（{cfg.model.rolling_days}日移動合計）と、そのうち{_e(cfg.own.name.split()[-1])}が占める分。
  面積が市場の大きさ、金色の帯が自社の取り分。帯が細るときはシェアを落としている。</p>
  <div class="ribbon">{_ribbon_svg(report.ribbon_dates, report.ribbon_area, report.ribbon_own, cfg.events)}
    <div class="legend">
      <span><i style="background:#7FA891;opacity:.45"></i>エリア合計</span>
      <span><i style="background:#C6A252"></i>{_e(cfg.own.name.split()[-1])}</span>
      <span><i style="background:#C04A3C"></i>需要イベント</span>
    </div>
  </div>
</section>

<section>
  <h2><span class="ix">03</span>コンプセット推定稼働</h2>
  <p class="sub">レビュー増分 ÷ 投稿率 ÷ 客室数。絶対値ではなく施設間の相対差とモメンタムを読む。</p>
{_table(report)}
</section>

<section>
  <h2><span class="ix">04</span>推定の土台</h2>
  <p class="sub">この数字が信用できるかどうかは、自社実績とのキャリブレーションで決まる。</p>
{_cards(cfg, report)}
</section>

<section>
  <h2><span class="ix">05</span>シグナル</h2>
  <p class="sub">今日の判断に付随して確認する項目。ルール逸脱時のみ人が介入する。</p>
{_signals(signals)}
</section>

<footer>
{_footer(cfg)}
</footer>

</div></body></html>
"""
