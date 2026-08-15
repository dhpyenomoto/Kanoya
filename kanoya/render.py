"""HTML描画。

読み手は現場のレベニュー担当であって分析者ではない。したがって上から順に
「今日どうするか」→「なぜそう言えるか」→「その根拠はどこまで信用できるか」
の順で並べる。数字の羅列を先に置かない。
"""

from __future__ import annotations

from html import escape

from .models import Report
from .ribbon import render_ribbon

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


def _change_cell(change: float | None, threshold: float) -> str:
    if change is None:
        return '<td class="num">—</td>'
    cls = ""
    if change >= threshold:
        cls = "pos"
    elif change <= -threshold:
        cls = "neg"
    return f'<td class="num {cls}">{change:+.0f}%</td>'


def _table(report: Report, highlight_threshold: float) -> str:
    peak = max((e.occupancy for e in report.estimates), default=0.0) or 1.0
    rows = []
    for est in report.estimates:
        prop = est.property
        tag = '<span class="tag">自社</span>' if prop.is_own else ""
        rows.append(
            f"""
        <tr class="{'own' if prop.is_own else ''}">
          <td class="nm">{escape(prop.name)}{tag}</td>
          <td class="num">{prop.rooms}</td>
          <td class="bar"><span style="width:{est.occupancy / peak * 100:.1f}%"></span>"""
            f"""<em>{est.occupancy:.0f}%</em></td>
          <td class="num">{est.sold_rooms:.0f}</td>
          {_change_cell(est.change_pct, highlight_threshold)}
          <td class="conf c{est.confidence}">{est.confidence}</td>
        </tr>"""
        )
    return f"""  <table>
    <thead><tr><th>施設</th><th class="num">室数</th><th>推定稼働（直近{report.window_days}日）</th>
    <th class="num">推定販売室数</th><th class="num">前期比</th><th class="conf">信頼度</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>"""


def _events(report: Report) -> str:
    if not report.events:
        return '<ul class="ev"><li class="empty">需要イベント未登録</li></ul>'
    items = "".join(
        f"<li><time>{e.date.isoformat()}</time><span>{escape(e.name)}</span>"
        f"<em>+{e.lift_pct}</em></li>"
        for e in report.events
    )
    return f'<ul class="ev">{items}</ul>'


def _signals(report: Report) -> str:
    if not report.signals:
        return (
            '<ul class="sigs"><li class="sig"><div class="sig-h">'
            '<span class="cat">全般</span><span class="lvl">平常</span></div>'
            "<h4>ルール逸脱なし</h4><p>閾値を超えた項目はない。"
            "人の介入は不要で、残室連動をそのまま流す。</p></li></ul>"
        )
    items = "".join(
        f"""
        <li class="sig {s.tone}">
          <div class="sig-h"><span class="cat">{escape(s.category)}</span>
          <span class="lvl">{escape(s.level)}</span></div>
          <h4>{escape(s.title)}</h4><p>{escape(s.body)}</p>
        </li>"""
        for s in report.signals
    )
    return f'<ul class="sigs">{items}</ul>'


def _rate_card(report: Report) -> str:
    rate = report.posting_rate
    bt = report.backtest
    accuracy = (
        f'<div class="kv"><span>実績との平均誤差</span>'
        f"<b>{bt.mean_abs_error_pt:.1f}pt（n={bt.windows}）</b></div>"
        f'<div class="kv"><span>使ってよい範囲</span><b>{escape(bt.usable_for)}</b></div>'
        if bt.windows
        else '<div class="kv"><span>実績との突合</span><b>未実施</b></div>'
    )
    note = (
        "自社PMS実績で実測したレビュー投稿率。競合にも同率を適用する。"
        if rate.is_measured
        else "自社実績が突合できていないため既定値を使用。競合比較は相対差のみに留めること。"
    )
    return f"""    <div class="card">
      <h3>レビュー投稿率</h3>
      <div class="big">{rate.rate * 100:.1f}%<small>95%CI {rate.ci_low * 100:.1f}〜{rate.ci_high * 100:.1f}%</small></div>
      <div style="height:18px"></div>
      <div class="kv"><span>算定方法</span><b>{escape(rate.source)}</b></div>
      <div class="kv"><span>突合日数</span><b>{rate.days}日</b></div>
      <div class="kv"><span>突合販売室数</span><b>{rate.sold_rooms:.0f}室</b></div>
      <div class="kv"><span>突合レビュー</span><b>{rate.raw_reviews:.1f}件</b></div>
      {accuracy}
      <p class="note">{note}</p>
    </div>"""


def _share_card(report: Report) -> str:
    share = report.share
    rating = report.own.rating
    rating_text = f"{rating:.2f} ／ 下限 {report.rating_floor:.1f}" if rating else "—"
    return f"""    <div class="card">
      <h3>エリア内シェアと需要暦</h3>
      <div class="big">{share.share_pct:.1f}%<small>クチコミ総量に占める自社比率</small></div>
      <div style="height:18px"></div>
      <div class="kv"><span>エリア合計</span><b>{share.total_raw_reviews:.0f}件</b></div>
      <div class="kv"><span>自社</span><b>{share.own_raw_reviews:.0f}件</b></div>
      <div class="kv"><span>自社評価</span><b>{rating_text}</b></div>
      <div style="height:14px"></div>
      {_events(report)}
    </div>"""


def _footer(report: Report) -> str:
    rooms = report.own.property.rooms
    lag = report.review_lag_days
    return f"""<footer>
  <h3>前提と限界</h3>
  <ol>
    <li>Google Places API は競合施設の宿泊料金を返さない。本ダッシュボードが推定するのは
      <strong>需要</strong>であり、<strong>レート</strong>ではない。実レートは正規ライセンスの
      レートショッパー（メトロエンジン等）で別途取得し、この需要判断と突き合わせる。</li>
    <li>OTA画面・Googleホテル検索画面のスクレイピングは規約違反であり、本システムには実装していない。</li>
    <li>レビューは滞在から平均{lag}日遅れて投稿されるものとして滞在日に引き戻している。
      投稿遅延の分布は施設ごとに異なるため、直近{lag}日の推定値は不安定。</li>
    <li>オーベルジュ・レストラン併設施設は外来客のレビューが混在する。
      config.json の restaurant_review_share で控除しているが、これは推定値であり感度分析が必要。</li>
    <li>全{rooms}室という母数では日次の稼働率は {_granularity(rooms)}% しか取らない。
      本手法は{report.window_days}日窓の集計でのみ意味を持つ。日次の需要予測には使えない。</li>
    <li>推定稼働率の絶対値を投資家向け資料やオーナー報告にそのまま転載しないこと。
      用途は自社の値付け判断に限る。</li>
  </ol>
</footer>"""


def _granularity(rooms: int) -> str:
    """客室数が少ないと日次稼働は飛び飛びの値しか取らない。それを明示する。"""
    return "/".join(f"{i / rooms * 100:.0f}" for i in range(rooms + 1))


def render_html(report: Report) -> str:
    own = report.own
    verdict = report.verdict
    ribbon_svg = render_ribbon(report.ribbon)
    highlight = 5.0

    return f"""<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>鹿のや ｜ レベニュー・インテリジェンス</title>
<style>
{CSS}
</style></head><body><div class="wrap">

<header>
  <div class="eyebrow">dhp都市開発グループ ／ Revenue Intelligence</div>
  <h1>{escape(own.property.name)}<small>{report.generated_on.isoformat()} 時点 ・ 直近{report.window_days}日 ・ Google Places 由来</small></h1>
  <div class="rule"></div>
</header>

<section>
  <h2><span class="ix">01</span>今日の判断</h2>
  <div class="verdict {verdict.tone}">
    <div class="lvl">{escape(verdict.level)}</div>
    <h3>{escape(verdict.headline)}</h3>
    <p>{escape(verdict.body)}</p>
  </div>
</section>

<section>
  <h2><span class="ix">02</span>需要リボン</h2>
  <p class="sub">エリア全体のクチコミ発生量（7日移動合計）と、そのうち{escape(_short_name(own.property.name))}が占める分。
  面積が市場の大きさ、金色の帯が自社の取り分。帯が細るときはシェアを落としている。</p>
  <div class="ribbon">{ribbon_svg}
    <div class="legend">
      <span><i style="background:#7FA891;opacity:.45"></i>エリア合計</span>
      <span><i style="background:#C6A252"></i>{escape(_short_name(own.property.name))}</span>
      <span><i style="background:#C04A3C"></i>需要イベント</span>
    </div>
  </div>
</section>

<section>
  <h2><span class="ix">03</span>コンプセット推定稼働</h2>
  <p class="sub">レビュー増分 ÷ 投稿率 ÷ 客室数。絶対値ではなく施設間の相対差とモメンタムを読む。</p>
{_table(report, highlight)}
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
  {_signals(report)}
</section>

{_footer(report)}

</div></body></html>
"""


def _short_name(name: str) -> str:
    """「奈良春日 鹿のや」→「鹿のや」。本文中で施設名を繰り返すときに使う。"""
    return name.split()[-1] if " " in name or "　" in name else name
