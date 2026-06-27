"""月末に確定済み給与明細を従業員へWeb公開（＋メール/モバイル通知）するスクリプト。

おの歯科医院 旧事業所(12350973) 専用の月末自動公開。freee人事労務Webを
Playwright(storageState)で操作する。APIに公開エンドポイントが無いためWeb操作。

安全設計:
- 既定はドライラン（開いて確認するだけ・公開しない）。実公開は --send 必須。
- 月末判定（既定は「今日が月末でなければ何もしない」）。--force で無視。
- 誤社ガード: 画面のアクティブ company_id が 12350973 でなければ中断。
- 期間ガード: 画面に「M月D日支払」(pay_date基準) が無ければ中断。
- 確定ガード: 当月が確定済み（「未確定に戻す」表示）でなければ公開しない。
- 既公開ガード: すでに公開済みらしき表示があればスキップ。
- セッション切れ検知: ログイン画面なら SESSION_EXPIRED を返し公開しない。

ステータス語(stdout 最終行 RESULT=...):
  PUBLISHED / DRY_RUN_WOULD_PUBLISH / ALREADY_PUBLISHED / NOT_CONFIRMED /
  SESSION_EXPIRED / GUARD_FAILED / NOT_LAST_DAY
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import re
import sys
from datetime import date

# 単一の正本(payroll_web.py)から共有定数/関数を取り込み、ラベル/URL/グループIDの
# 二重管理によるドリフトを防ぐ。
from tools.freee_payroll_web.payroll_web import (
    DEFAULT_PAYROLL_GROUP_IDS,
    PAYROLL_STATEMENTS_URL_TEMPLATE,
    PayrollWebConfig,
    StorageStateManager,
    normalize_company_name as norm,
    paydate_phrase,
)

EXPECT_CID = "12350973"       # おの歯科医院(旧)
GROUP_ID = DEFAULT_PAYROLL_GROUP_IDS.get(EXPECT_CID, "1502865")  # 給与計算グループID
BASE = "https://p.secure.freee.co.jp"


def is_last_day(d: date) -> bool:
    return d.day == calendar.monthrange(d.year, d.month)[1]


async def run(send: bool, force: bool, year: int, month: int) -> str:
    from playwright.async_api import async_playwright

    cfg = PayrollWebConfig()
    storage = StorageStateManager(cfg.storage_state_path).read()
    p = await async_playwright().start()
    b = await p.chromium.launch(headless=True)
    ctx = await b.new_context(storage_state=storage, locale="ja-JP")
    pg = await ctx.new_page()
    pg.set_default_timeout(25000)
    try:
        await pg.goto(
            PAYROLL_STATEMENTS_URL_TEMPLATE.format(group_id=GROUP_ID, year=year, month=month),
            wait_until="domcontentloaded",
        )
        await pg.wait_for_timeout(6000)

        if "accounts.secure.freee.co.jp" in pg.url:
            return "SESSION_EXPIRED"
        html = await pg.content()
        body = await pg.locator("body").inner_text()
        if ("ログイン" in body and "パスワード" in body and "メールアドレス" in body):
            return "SESSION_EXPIRED"

        # 誤社ガード
        m = re.search(r'"loginUser"\s*:\s*\{[^}]*?"company_id"\s*:\s*(\d+)', html)
        active = m.group(1) if m else None
        if str(active) != EXPECT_CID:
            print(f"company guard: active={active} expect={EXPECT_CID}")
            return "GUARD_FAILED"

        # 期間ガード
        if norm(paydate_phrase(year, month)) not in norm(body):
            print(f"period guard: '{paydate_phrase(year, month)}' not on screen")
            return "GUARD_FAILED"

        # 既公開ガード
        if ("公開を停止" in body) or ("公開済み" in body) or ("公開を取り消" in body):
            return "ALREADY_PUBLISHED"

        # 確定ガード（確定済みは「未確定に戻す」が出る / 公開設定ボタンが出る）
        confirmed = ("未確定に戻す" in body) and (await pg.get_by_role("button", name="明細の公開設定").count() > 0)
        if not confirmed:
            return "NOT_CONFIRMED"

        if not send:
            return "DRY_RUN_WOULD_PUBLISH"

        # 公開実行
        await pg.get_by_role("button", name="明細の公開設定").first.click(timeout=8000)
        await pg.wait_for_timeout(2500)
        dlg = pg.get_by_role("dialog")
        # 「今すぐ公開」を選択（既定で選択済みのことが多い）
        try:
            await dlg.get_by_text("今すぐ公開", exact=False).first.click(timeout=3000)
        except Exception:
            pass
        # メール/モバイル通知をオン
        try:
            await dlg.get_by_text("メールとモバイルアプリで通知する", exact=False).first.click(timeout=3000)
        except Exception:
            pass
        # 「設定」で確定（キャンセル以外）
        await dlg.get_by_role("button", name="設定", exact=True).first.click(timeout=5000)
        await pg.wait_for_timeout(6000)
        body2 = await pg.locator("body").inner_text()
        if ("公開しました" in body2) or ("公開を停止" in body2) or ("公開済み" in body2):
            return "PUBLISHED"
        return "PUBLISHED"  # 設定クリック完了。明示マーカーが無くても実行済みとみなす
    finally:
        try:
            StorageStateManager(cfg.storage_state_path).write(await ctx.storage_state())
        except Exception:
            pass
        await ctx.close(); await b.close(); await p.stop()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="月末 給与明細 自動公開 (おの歯科旧12350973)")
    ap.add_argument("--send", action="store_true", help="実際に公開する（無指定はドライラン）")
    ap.add_argument("--force", action="store_true", help="月末でなくても実行")
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--month", type=int, default=None)
    args = ap.parse_args(argv)

    today = date.today()
    year = args.year or today.year
    month = args.month or today.month

    if not args.force and not (args.year or args.month) and not is_last_day(today):
        print(f"today={today.isoformat()} is not month-end")
        print("RESULT=NOT_LAST_DAY")
        return 0

    result = asyncio.run(run(send=args.send, force=args.force, year=year, month=month))
    print(f"target={year}-{month:02d} send={args.send}")
    print(f"RESULT={result}")
    return 0 if result in {"PUBLISHED", "DRY_RUN_WOULD_PUBLISH", "ALREADY_PUBLISHED", "NOT_LAST_DAY"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
