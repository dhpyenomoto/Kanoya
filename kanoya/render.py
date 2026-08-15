"""ダッシュボードの HTML 生成。

このモジュールは計算を持たない。Dashboard に載っている確定値を表示に
落とすだけ。数字の意味づけ（信頼度・判断・シグナル）は estimate / signals
側で決まっており、ここで閾値を書かない。
"""

from __future__ import annotations

import datetime as dt

from .dashboard import Dashboard
from .ribbon import render as render_ribbon

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
/* td.bar の min-width で表は 430px 前後を要求する。狭い画面ではページ全体が
   横スクロールしてしまうので、表だけを自前のスクロール枠に閉じ込める。 */
.tablewrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:14px;min-width:430px}
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


def esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _verdict_class(tone: str) -> str:
    return {"raise": "raise", "hold": "hold", "capture": "alert"}.get(tone, "hold")


def _change_class(change: float | None) -> str:
    """±5% を超える動きだけ色を付ける。それ未満は推定の揺らぎと区別できない。"""
    if change is None:
        return ""
    if change >= 5.0:
        return "pos"
    if change <= -5.0:
        return "neg"
    return ""


def _row(dash: Dashboard, est, max_occupancy: float) -> str:
    conf = est.confidence(
        dash.config.estimation.confidence_high_rse,
        dash.config.estimation.confidence_medium_rse,
    )
    change = est.change_pct
    width = (est.occupancy / max_occupancy * 100.0) if max_occupancy > 0 else 0.0
    tag = '<span class="tag">自社</span>' if est.prop.is_own else ""
    change_text = f"{change:+.0f}%" if change is not None else "—"
    return f"""        <tr class="{'own' if est.prop.is_own else ''}">
          <td class="nm">{esc(est.prop.name)}{tag}</td>
          <td class="num">{est.prop.rooms}</td>
          <td class="bar"><span style="width:{width:.1f}%"></span><em>{est.occupancy * 100:.0f}%</em></td>
          <td class="num">{est.rooms_sold:.0f}</td>
          <td class="num {_change_class(change)}">{change_text}</td>
          <td class="conf c{conf}">{conf}</td>
        </tr>"""


def _signal(sig) -> str:
    return f"""        <li class="sig {sig.tone}">
          <div class="sig-h"><span class="cat">{esc(sig.category)}</span>
          <span class="lvl">{esc(sig.level)}</span></div>
          <h4>{esc(sig.title)}</h4><p>{esc(sig.detail)}</p>
        </li>"""


def _event(event: "object", as_of: dt.date) -> str:
    return (
        f"<li><time>{event.date:%Y-%m-%d}</time><span>{esc(event.name)}</span>"
        f"<em>+{event.lift_pct}</em></li>"
    )


def _occupancy_steps(rooms: int) -> str:
    return "/".join(f"{i * 100 / rooms:g}" for i in range(rooms + 1)) + "%"


def render(dash: Dashboard) -> str:
    cfg = dash.config
    est = dash.estimates
    max_occ = max((e.occupancy for e in est), default=0.0)
    rate = dash.rate

    rows = "\n".join(_row(dash, e, max_occ) for e in est)

    if dash.signals:
        signals = "\n".join(_signal(s) for s in dash.signals)
    else:
        signals = (
            '        <li class="sig"><div class="sig-h"><span class="cat">全般</span>'
            '<span class="lvl">なし</span></div><h4>ルール逸脱なし</h4>'
            "<p>本日は人の介入を要する項目がない。自動の残室連動に任せる。</p></li>"
        )

    events = "".join(_event(e, dash.as_of) for e in cfg.events)
    if not events:
        events = '<li class="empty">需要暦が未登録。config.json の demand_events に追加すること。</li>'

    mae = dash.backtest.mean_absolute_error_pt
    mae_text = (
        f"{mae:.1f}pt（n={len(dash.backtest.blocks)}）"
        if dash.backtest.blocks
        else "未検証"
    )
    rating_text = (
        f"{dash.own_rating:.2f} ／ 下限 {cfg.rules.rating_floor:g}"
        if dash.own_rating is not None
        else f"未取得 ／ 下限 {cfg.rules.rating_floor:g}"
    )
    share = (dash.own_reviews / dash.area_reviews * 100.0) if dash.area_reviews > 0 else 0.0

    return f"""<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(cfg.own.name.split()[-1] if ' ' in cfg.own.name else cfg.own.name)} ｜ レベニュー・インテリジェンス</title>
<style>
{CSS}
</style></head><body><div class="wrap">

<header>
  <div class="eyebrow">{esc(cfg.group_label)} ／ Revenue Intelligence</div>
  <h1>{esc(cfg.own.name)}<small>{dash.as_of:%Y-%m-%d} 時点 ・ 直近{dash.current.days}日 ・ Google Places 由来</small></h1>
  <div class="rule"></div>
</header>

<section>
  <h2><span class="ix">01</span>今日の判断</h2>
  <div class="verdict {_verdict_class(dash.verdict.tone)}">
    <div class="lvl">{esc(dash.verdict.level)}</div>
    <h3>{esc(dash.verdict.headline)}</h3>
    <p>{esc(dash.verdict.detail)}</p>
  </div>
</section>

<section>
  <h2><span class="ix">02</span>需要リボン</h2>
  <p class="sub">エリア全体のクチコミ発生量（7日移動合計）と、そのうち{esc(dash.short_name)}が占める分。
  面積が市場の大きさ、金色の帯が自社の取り分。帯が細るときはシェアを落としている。</p>
  <div class="ribbon">{render_ribbon(dash.ribbon)}
    <div class="legend">
      <span><i style="background:#7FA891;opacity:.45"></i>エリア合計</span>
      <span><i style="background:#C6A252"></i>{esc(dash.short_name)}</span>
      <span><i style="background:#C04A3C"></i>需要イベント</span>
    </div>
  </div>
</section>

<section>
  <h2><span class="ix">03</span>コンプセット推定稼働</h2>
  <p class="sub">レビュー増分 ÷ 投稿率 ÷ 客室数。絶対値ではなく施設間の相対差とモメンタムを読む。</p>
  <div class="tablewrap"><table>
    <thead><tr><th>施設</th><th class="num">室数</th><th>推定稼働（直近{dash.current.days}日）</th>
    <th class="num">推定販売室数</th><th class="num">前期比</th><th class="conf">信頼度</th></tr></thead>
    <tbody>
{rows}</tbody>
  </table></div>
</section>

<section>
  <h2><span class="ix">04</span>推定の土台</h2>
  <p class="sub">この数字が信用できるかどうかは、自社実績とのキャリブレーションで決まる。</p>
  <div class="cols">
    <div class="card">
      <h3>レビュー投稿率</h3>
      <div class="big">{rate.point * 100:.1f}%<small>95%CI {rate.low * 100:.1f}〜{rate.high * 100:.1f}%</small></div>
      <div style="height:18px"></div>
      <div class="kv"><span>算定方法</span><b>自社実績で実測</b></div>
      <div class="kv"><span>突合日数</span><b>{rate.days}日</b></div>
      <div class="kv"><span>突合販売室数</span><b>{rate.rooms_sold}室</b></div>
      <div class="kv"><span>突合レビュー</span><b>{rate.reviews:.1f}件</b></div>
      <div class="kv"><span>実績との平均誤差</span><b>{mae_text}</b></div><div class="kv"><span>使ってよい範囲</span><b>{esc(dash.backtest.usable_range)}</b></div>
      <p class="note">自社PMS実績で実測したレビュー投稿率。競合にも同率を適用する。</p>
    </div>
    <div class="card">
      <h3>エリア内シェアと需要暦</h3>
      <div class="big">{share:.1f}%<small>クチコミ総量に占める自社比率</small></div>
      <div style="height:18px"></div>
      <div class="kv"><span>エリア合計</span><b>{dash.area_reviews:.0f}件</b></div>
      <div class="kv"><span>自社</span><b>{dash.own_reviews:.0f}件</b></div>
      <div class="kv"><span>自社評価</span><b>{rating_text}</b></div>
      <div style="height:14px"></div>
      <ul class="ev">{events}</ul>
    </div>
  </div>
</section>

<section>
  <h2><span class="ix">05</span>シグナル</h2>
  <p class="sub">今日の判断に付随して確認する項目。ルール逸脱時のみ人が介入する。</p>
  <ul class="sigs">
{signals}</ul>
</section>

<footer>
  <h3>前提と限界</h3>
  <ol>
    <li>Google Places API は競合施設の宿泊料金を返さない。本ダッシュボードが推定するのは
      <strong>需要</strong>であり、<strong>レート</strong>ではない。実レートは正規ライセンスの
      レートショッパー（メトロエンジン等）で別途取得し、この需要判断と突き合わせる。</li>
    <li>OTA画面・Googleホテル検索画面のスクレイピングは規約違反であり、本システムには実装していない。</li>
    <li>レビューは滞在から平均{cfg.estimation.review_lag_days}日遅れて投稿されるものとして滞在日に引き戻している。
      投稿遅延の分布は施設ごとに異なるため、直近{cfg.estimation.unstable_tail_days}日の推定値は不安定。</li>
    <li>オーベルジュ・レストラン併設施設は外来客のレビューが混在する。
      config.json の restaurant_review_share で控除しているが、これは推定値であり感度分析が必要。</li>
    <li>全{cfg.own.rooms}室という母数では日次の稼働率は {_occupancy_steps(cfg.own.rooms)} しか取らない。
      本手法は{dash.current.days}日窓の集計でのみ意味を持つ。日次の需要予測には使えない。</li>
    <li>推定稼働率の絶対値を投資家向け資料やオーナー報告にそのまま転載しないこと。
      用途は自社の値付け判断に限る。</li>
  </ol>
</footer>

</div></body></html>
"""
