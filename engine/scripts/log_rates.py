"""提示価格の日次記録 — 変更分だけを入力する.

    python3 scripts/log_rates.py                  # 対話入力
    python3 scripts/log_rates.py --show           # いまの提示価格を見るだけ
    python3 scripts/log_rates.py --set 11/14-11/30=62000
    python3 scripts/log_rates.py --import export.csv   # サイトコントローラーから

**ネットワークへは一切アクセスしない。** 手元のCSVを読み書きするだけ。

**3分で終わることが設計要件**

5室 × 4形態 × 120日を毎日入力させたら、運用は続かない。続かない記録は
無いのと同じである。そこで次の形にしてある。

  1. 記録は**変更履歴**。値が変わった日だけ行が増える。
     変更が無い日は入力なしで終わる（Enter だけ）。
     ただし「確認した」こと自体は rate_log_runs.csv に必ず1行残す
     （行が無い日が「確認して変わっていない」と「誰も見ていない」の
     両方を意味してしまうため）
  2. 表示は**同じ値が続く区間**にまとめる。120行ではなく数行になる
  3. 入力は区間指定。`11/14-11/30=62000` のように、変えたところだけ書く

過去分は復元できない。2026年2月〜9月の提示価格は失われている。
遡って埋めようとしないこと（推定値を実測と混ぜると、あとから区別できない）。

**実行記録が保証していること／していないこと**

このスクリプトは、実行するたびに rate_log_runs.csv へ1行残す。その1行が
保証しているのは「**担当者がスクリプトを実行し、変更なしと申告した**」
という事実だけである。**OTA管理画面との突合を保証するものではない。**

対話モードで画面に出るのは14行、うち価格の区間行は3行しかない。担当者が
Enter を押した時点で表示期間ぜんぶが確認済みとして記録されるが、実際に
OTA側の画面を見たかどうかまでは、仕組みとして検証していない。

したがって **サイトコントローラー連携が入るまで、この記録の信頼性は
担当者の運用に依存する。** この列を「検証済み」として扱わないこと。
そう扱った瞬間に、実行記録そのものが防ごうとしたのと同じ種類の誤解
（確認していないことを確認済みと読む）を生む。

解消される条件: サイトコントローラーから提示価格を機械的に取り込めるように
なれば、申告ベースから実測ベースへ移行できる（--import がその入口）。
その時点でこの注記は不要になる。
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm import rate_log                              # noqa: E402
from kanoya_rm.config import (                              # noqa: E402
    Settings, parse_date, private_path_candidates, resolve_private_path,
)
from kanoya_rm.products import FORMS, LABELS, load as load_products  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def resolve_log_path(settings: Settings) -> Path:
    """記録の置き場所。まだ無ければ先頭候補を作成先として返す."""
    found = resolve_private_path(ROOT / "config", settings.sources, "rate_log")
    if found is not None:
        return found
    candidates = private_path_candidates(ROOT / "config", settings.sources,
                                         "rate_log")
    existing_parent = [p for p in candidates if p.parent.exists()]
    if existing_parent:
        return existing_parent[0]
    if candidates:
        return candidates[0]
    return ROOT / "data" / "rate_log.csv"


def spans(values: dict[date, float], days: list[date]) -> list[tuple[date, date, float]]:
    """同じ値が続く区間へまとめる（120行を数行にするため）.

    隣接判定は days の並び順で行い、暦日の連続では見ない。
    閉館日（火・水）が間に入るたびに区間が切れると、9行で済むものが
    20行になり、まとめた意味がなくなる。
    """
    out: list[tuple[date, date, float]] = []
    for day in days:
        rate = values.get(day, 0.0)
        if out and out[-1][2] == rate:
            out[-1] = (out[-1][0], day, rate)
        else:
            out.append((day, day, rate))
    return out


def parse_span(text: str, base_year: int) -> tuple[date, date]:
    """'11/14-11/30' / '2026-11-14' / '11/14' を期間へ."""
    text = text.strip()
    if "-" in text and text.count("-") == 1 and "/" in text:
        left, right = text.split("-")
        return _one_day(left, base_year), _one_day(right, base_year)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.\.\d{4}-\d{2}-\d{2}", text):
        left, right = text.split("..")
        return parse_date(left), parse_date(right)
    day = _one_day(text, base_year)
    return day, day


def _one_day(text: str, base_year: int) -> date:
    text = text.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return parse_date(text)
    month, day = (int(part) for part in text.split("/"))
    return date(base_year, month, day)


def apply_set(entries: list[rate_log.Entry], spec: str, *, as_of: date,
              form: str, channel: str, note: str, base_year: int,
              settings: Settings | None = None) -> list[rate_log.Entry]:
    """'11/14-11/30=62000' を行へ展開する.

    閉館日は飛ばす。販売していない日の提示価格を記録しても使い道がなく、
    記録だけが倍に膨らむ（火・水が定休なので実際に3割近い）。
    """
    if "=" not in spec:
        raise SystemExit(f"書き方: 期間=金額 （例 11/14-11/30=62000） / 受け取った値: {spec}")
    span_text, rate_text = spec.split("=", 1)
    start, end = parse_span(span_text, base_year)
    rate = float(rate_text.replace(",", "").strip())
    if start > end:
        raise SystemExit(f"期間の前後が逆です: {spec}")
    added: list[rate_log.Entry] = []
    day = start
    while day <= end:
        if settings is None or settings.is_open(day):
            added.append(rate_log.Entry(stay_date=day, snapshot_date=as_of,
                                        product_type=form, posted_rate=rate,
                                        channel=channel, note=note))
        day += timedelta(days=1)
    return added


def show(settings: Settings, entries: list[rate_log.Entry], days: list[date],
         as_of: date, channel: str, runs: list[rate_log.Run],
         max_unconfirmed: int) -> None:
    products = load_products(settings)
    since = rate_log.unconfirmed_days(runs, as_of)
    print(f"  基準日 {as_of} 時点の提示価格（1室2名1泊・税サ込）")
    print(f"  記録件数 {len(entries)} 行"
          f"／最終記録 {rate_log.latest_entry_date(entries) or '—'}")
    if since is None:
        print("  実行記録 なし（まだ一度も確認していません）")
    else:
        last = max(r.run_date for r in runs if r.run_date <= as_of)
        state = "未実行" if since > max_unconfirmed else "確認済み"
        print(f"  実行記録 最後の実行 {last}（{since}日前・{state}）")
    print()
    for form in FORMS:
        values = {}
        for day in days:
            posted = rate_log.posted_as_of(entries, day, as_of, channel=channel)
            values[day] = posted.get(form, 0.0)
        if not any(values.values()):
            continue
        print(f"  【{LABELS[form]}】")
        for start, end, rate in spans(values, days):
            if rate <= 0:
                continue
            span = (f"{start:%m/%d}" if start == end
                    else f"{start:%m/%d}-{end:%m/%d}")
            # 裏付けの無い値をそのまま並べると、現在の掲出価格に見える。
            # 表示はするが、使っていないことが分かるようにする。
            backed = rate_log.room_rate_as_of(
                entries, start, as_of, products, channel=channel, runs=runs,
                max_unconfirmed_days=max_unconfirmed) > 0
            mark = "" if backed else "  ← 未確認（使っていません）"
            print(f"    {span:<14} {rate:>9,.0f}円{mark}")
    never = [d for d in days
             if not rate_log.posted_as_of(entries, d, as_of, channel=channel)]
    unconfirmed = [
        d for d in days
        if d not in never
        and rate_log.room_rate_as_of(entries, d, as_of, products,
                                     channel=channel, runs=runs,
                                     max_unconfirmed_days=max_unconfirmed) <= 0]
    if never or unconfirmed:
        print()
        if never:
            print(f"  一度も記録なし: {len(never)} / {len(days)} 日"
                  f"（最初の日 {never[0]}）")
        if unconfirmed:
            print(f"  記録はあるが未確認: {len(unconfirmed)} / {len(days)} 日"
                  f"（最初の日 {unconfirmed[0]}）")
            print("    実行記録に裏付けが無いため、据え置きを仮定しません。")
        print("    どちらも日次変動幅ガードがかからず、承認区分も判定できません。")
    print()
    print(f"  ※ 部屋代換算（current_public_rate）は、素泊まりがあればその値、"
          f"無ければ食事加算を引いて戻します。")


def import_csv(path: Path, *, as_of: date, channel: str) -> list[rate_log.Entry]:
    """サイトコントローラー等の書き出しを取り込む.

    必須列は stay_date と posted_rate。他は無ければ既定値で補う。
    ネットワークは使わない（手元に落としたファイルを読むだけ）。
    """
    if not path.exists():
        raise SystemExit(f"見つかりません: {path}")
    out: list[rate_log.Entry] = []
    skipped = 0
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                stay = parse_date(row["stay_date"])
                rate = float(str(row["posted_rate"]).replace(",", ""))
            except (KeyError, ValueError, TypeError):
                skipped += 1
                continue
            if rate <= 0:
                skipped += 1
                continue
            snapshot = row.get("snapshot_date")
            out.append(rate_log.Entry(
                stay_date=stay,
                snapshot_date=parse_date(snapshot) if snapshot else as_of,
                product_type=(row.get("product_type") or "room_only").strip(),
                posted_rate=rate,
                channel=(row.get("channel") or channel).strip(),
                note=(row.get("note") or "取り込み").strip(),
            ))
    if skipped:
        print(f"  読めなかった行: {skipped}（stay_date と posted_rate が必要）",
              file=sys.stderr)
    return out


def interactive(settings: Settings, entries: list[rate_log.Entry],
                days: list[date], as_of: date, form: str,
                channel: str) -> list[rate_log.Entry]:
    print()
    print("  変更があった区間だけ入力してください（例 11/14-11/30=62000）。")
    print("  複数あれば1行に1つずつ。変更が無ければ何も入れずに Enter。")
    print("  終わったら空行で確定します。")
    print()
    added: list[rate_log.Entry] = []
    while True:
        try:
            line = input("  > ").strip()
        except EOFError:
            break
        if not line:
            break
        try:
            added.extend(apply_set(entries + added, line, as_of=as_of,
                                   form=form, channel=channel, note="",
                                   base_year=days[0].year if days else as_of.year,
                                   settings=settings))
        except SystemExit as error:
            print(f"    {error}")
            continue
        print(f"    受け付けました（{len(added)}日分）")
    return added


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default="", help="記録する基準日（既定は今日）")
    parser.add_argument("--from", dest="start", default="",
                        help="表示する宿泊日の開始（既定は基準日）")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--product", default="room_only",
                        choices=list(FORMS), help="記録する商品形態")
    parser.add_argument("--channel", default="", help="販路（省略時は sources.json）")
    parser.add_argument("--note", default="")
    parser.add_argument("--show", action="store_true", help="表示だけして終わる")
    parser.add_argument("--set", action="append", default=[],
                        metavar="期間=金額", help="対話せずに設定する")
    parser.add_argument("--import", dest="import_path", default="",
                        help="CSVを取り込む（サイトコントローラー連携の入口）")
    parser.add_argument("--log", default="",
                        help="記録ファイルを明示する（既定は sources.json の rate_log）")
    parser.add_argument("--dry-run", action="store_true", help="書き込まない")
    args = parser.parse_args()

    settings = Settings.load(ROOT / "config")
    as_of = parse_date(args.as_of) if args.as_of else date.today()
    start = parse_date(args.start) if args.start else as_of
    days = [d for d in (start + timedelta(days=i) for i in range(args.days))
            if settings.is_open(d)]
    channel = args.channel or str(
        (settings.sources.get("rate_log") or {}).get("channel") or "")

    path = Path(args.log).expanduser() if args.log else resolve_log_path(settings)
    runs_path = rate_log.runs_path_for(path)
    entries = rate_log.read(path)
    runs = rate_log.read_runs(runs_path)
    max_unconfirmed = int((settings.sources.get("rate_log") or {}).get(
        "max_unconfirmed_days", rate_log.DEFAULT_MAX_UNCONFIRMED_DAYS))

    print("=" * 70)
    print("  提示価格の記録 — 変更分だけ入力する")
    print("=" * 70)
    print(f"  記録先 {path}")
    print(f"  形態   {LABELS.get(args.product, args.product)}"
          f"／販路 {channel or '（指定なし）'}")
    print()
    show(settings, entries, days, as_of, channel, runs, max_unconfirmed)

    if args.show:
        return

    # 「どこを確認したか」は入力の仕方で変わる。広く言い切らないこと。
    #   対話          画面に出した期間ぜんぶを人が見ている
    #   --set        その区間だけを名指しした（残りを見たとは限らない）
    #   --import     ファイルに入っていた宿泊日だけ
    if args.import_path:
        added = import_csv((ROOT / args.import_path).resolve(),
                           as_of=as_of, channel=channel)
        confirmed = [e.stay_date for e in added]
    elif args.set:
        added = []
        for spec in args.set:
            added.extend(apply_set(entries + added, spec, as_of=as_of,
                                   form=args.product, channel=channel,
                                   note=args.note, base_year=start.year,
                                   settings=settings))
        confirmed = [e.stay_date for e in added]
    else:
        added = interactive(settings, entries, days, as_of, args.product, channel)
        confirmed = list(days)

    if args.dry_run:
        print(f"  --dry-run のため書き込みません（変更 {len(added)}行）。")
        return

    # 確認したこと自体を必ず残す。ここを残さないと、行が無い日が
    # 「確認して変わっていない」と「誰も見ていない」の両方を意味してしまい、
    # 後者を前者と読んだ時点で、確認していない日に「据え置いた」という
    # 事実でない記録ができあがる。
    if confirmed:
        first, last = min(confirmed), max(confirmed)
        rate_log.append_run(runs_path, rate_log.Run(
            run_date=as_of, stay_from=first, stay_to=last,
            product_type=args.product, channel=channel,
            note=args.note or ("変更なしを確認" if not added else "")))
        print(f"  確認した範囲を記録しました: {first} 〜 {last} → {runs_path}")

    if not added:
        print("  価格の変更はありませんでした（変更履歴には書き込みません）。")
        print("  ※ 『確認して変わっていない』ことは上の実行記録で残ります。")
        return

    rate_log.append(path, added)
    print(f"  {len(added)} 行を追記しました → {path}")
    print("  反映を確認するには: python3 -m kanoya_rm.cli --days 30")


if __name__ == "__main__":
    main()
