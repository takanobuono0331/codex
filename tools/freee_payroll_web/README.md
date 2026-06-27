# freee payroll web MCP

Playwright storageState を使って freee人事労務Web の給与操作を行う薄いMCPサーバーです。

## Setup

```bash
cd "/Users/takaono/Documents/New project"
venv/bin/python -m pip install playwright
venv/bin/python -m playwright install chromium
venv/bin/python -m tools.freee_payroll_web.cli login
```

`login` はブラウザを開きます。freeeのログイン、2段階認証、CAPTCHAを人間が完了すると、storageState が `.secrets/` に暗号化保存されます。

## CLI

```bash
venv/bin/python -m tools.freee_payroll_web.cli open --company 12350973 --year 2026 --month 6
venv/bin/python -m tools.freee_payroll_web.cli recalc --company 12350973 --year 2026 --month 6 --dry-run
venv/bin/python -m tools.freee_payroll_web.cli revert-auto --company 12350973 --year 2026 --month 6 --employee 大西恵理
venv/bin/python -m tools.freee_payroll_web.cli finalize --company 12350973 --year 2026 --month 6
venv/bin/python -m tools.freee_payroll_web.cli publish --company 12350973 --year 2026 --month 6
```

`finalize` / `publish` / `revert-auto` は `--confirm` が無い限り画面読取だけを行い、実行しません。

### 実機検証で確定した実ボタン名 (2026-06-27, おの歯科 旧12350973)

| 操作 | トリガーボタン | 確認モーダル | 備考 |
|------|----------------|--------------|------|
| `recalc` | すべて再計算 | 再計算 | 直接編集(overwritten)・インポート行はスキップされる |
| `revert-auto` | 自動計算に戻す | 戻す | 個別明細ページ。直接編集を破棄し自動計算へ。確定済み行は不可 |
| `finalize` | 給与明細を確定 | 確定 | |
| `publish` | 明細の公開設定 | 設定 | 確定後に出現。従業員へ通知が飛ぶ・実質不可逆。本番運用は `publish_month_end.py` を推奨 |

### 給与ページ直リンク (給与計算グループID)

給与明細一覧は `https://p.secure.freee.co.jp/payroll_statements#/{group_id}/{year}/{month}` で直接開けます
(例: 旧12350973 = グループID `1502865`)。`company_id -> group_id` は既定の `DEFAULT_PAYROLL_GROUP_IDS`
に入っており、`FREEE_PAYROLL_GROUP_MAP_FILE` (または `--group-map`) で追加・上書きできます。
グループIDが未登録の事業所は、従来どおりテキスト/ロールベースのナビゲーションにフォールバックします。

## MCP

```bash
venv/bin/python -m tools.freee_payroll_web.server
```

公開ツール:

- `freee_payroll_open`
- `freee_payroll_recalc`
- `freee_payroll_finalize`
- `freee_payroll_publish`
- `freee_payroll_screenshot`

## Guard rails

- 操作前に画面の事業所名を `company_id` の期待名と照合します。
- `12350973` は旧おの歯科として厳格に扱い、既定では `おの歯科医院（旧）` 系の表示だけを許可します。
- freee画面で実際の表示が異なる場合は、誤社防止のため `company_map.local.json` に確認済みの表示名を追加してください。
- 月度は pay_date 基準です。2026年6月度は `--year 2026 --month 6` です。
- 画面上の月度表示が要求月度と一致しない場合は中断します。
- 監査ログは既存 `audit_log.py` 経由で `outputs/ai_audit_log.*` に追記します。従業員名や給与明細の値は監査ログの detail には入れません。

## Tests

```bash
venv/bin/python -m unittest discover tools/freee_payroll_web/tests
```
