# freee人事労務 給与Web操作 自動化MCP 設計書（Codex向け実装仕様）

作成: 2026-06-27 / 起票: claude（司令塔）/ 実装担当: **codex**
対象運用: 福啓会グループ（おの歯科医院ほか）の月次給与処理

---

## 0. 背景（なぜ作るか）

freee人事労務の**公開API（`https://api.freee.co.jp/hr`）には、給与の「再計算・確定（fix）・Web明細公開」エンドポイントが存在しない**。
実機確認済みの人事労務APIで給与に触れるのは以下のみ：

| 操作 | API | 可否 |
|------|-----|:--:|
| 給与明細の取得 | `GET /api/v1/salaries/employee_payroll_statements[/{employee_id}]` | ✅ 読取のみ |
| 勤怠サマリ取得/書換 | `GET\|PUT /api/v1/employees/{id}/work_record_summaries/{year}/{month}` | ✅ |
| 単価/社保/振込先等の設定 | `PUT /api/v1/employees/{id}/basic_pay_rule` 他 | ✅(※下記) |
| **給与の再計算** | — | ❌ 無し |
| **給与の確定** | — | ❌ 無し |
| **Web明細の公開** | — | ❌ 無し |

さらに 2026-06-07 のマスタ移行後、現行OAuthアプリは**旧事業所 12350973 への書き込み認可を持たない**（GET直指定は通るが、`PUT basic_pay_rule` は `403 access_denied: このアプリケーションにはアクセス権限がないエンドポイントです` を返す）。
→ 結論：**給与処理の書き込み側はブラウザ自動化で実装するのが唯一かつ堅実な道**。人間のWebログインセッションを使うため、アプリ認可スコープの制約を受けない。

---

## 1. ゴール

freee人事労務Web（`https://p.secure.freee.co.jp/`）の給与処理を、MCPツール（または薄いCLI＋MCPラッパー）として呼べるようにする。最小ツールセット：

| ツール名 | 役割 | 副作用 |
|---------|------|:--:|
| `freee_payroll_open` | 指定事業所・月度の給与計算画面を開き、全従業員の現在値（総支給/差引/控除/勤務日数/確定状態）を表で返す | なし（読取） |
| `freee_payroll_recalc` | 「再計算」を実行し、勤怠を反映した最新値を返す | 給与明細の値が更新（未確定なら可逆） |
| `freee_payroll_finalize` | 「給与を確定する」 | **要人間承認**。確定状態へ |
| `freee_payroll_publish` | 「Web明細を公開」（従業員へメール通知） | **要人間承認・実質不可逆**（通知が飛ぶ） |
| `freee_payroll_screenshot` | 現画面のスクショ（検証用） | なし |

---

## 2. 技術選定

- **Playwright**（Python or Node。既存資産がPythonなら Python + `playwright`）。
- **セッション永続化**：`storageState`（Cookie+localStorage）を暗号化保存し、毎回のログイン/2段階認証を回避。失効時のみ対話ログイン。
- 形態は2案。**推奨=B**。
  - A: 単体CLIスクリプト（`python freee_payroll.py recalc --company 12350973 --year 2026 --month 6`）。Bashツールから叩く。最短。
  - B: 薄い**自前MCPサーバー**（stdio）。上記CLIを内部呼び出しし、上表のツールを公開。「MCPにエンドポイント」という要望に合致。
- 配置：`tools/freee_payroll_web/` 配下（CLI本体＋MCPサーバー＋storageState＋.env.example）。

---

## 3. 画面フロー（freee人事労務Web）

> セレクタは実装時に実画面で確定すること（freee UIは変わる。role/text ベースで、id/class直書きは避ける）。

1. ログイン：`https://accounts.secure.freee.co.jp/login` → メール/パスワード → （2段階認証/CAPTCHAが出たら人間にハンドオフ）。
2. 人事労務へ：`https://p.secure.freee.co.jp/` → 事業所セレクタで**対象事業所を選択**。
3. 給与 → 給与計算 → **月度を選択**（例「2026年6月度」＝5月勤務分・支払2026-06-30）。
4. `recalc`：「再計算」ボタン → 完了待ち（スピナー消滅/トースト）→ 一覧の各行を読取。
5. `finalize`：「給与を確定する」→ 確認ダイアログ → 確定。
6. `publish`：「Web明細を公開」→ 確認 → 公開（従業員へメール）。

---

## 4. 入出力仕様

共通入力：`company_id`（必須）, `year`（pay_date基準の年）, `month`（**pay_date基準の月**。例：5月勤務分→`month=6`）。

`open` / `recalc` の戻り値（JSON）例：
```json
{
  "company_id": 12350973,
  "company_name_on_screen": "おの歯科医院(旧)",
  "period_label": "2026年6月度",
  "pay_date": "2026-06-30",
  "fixed": false,
  "rows": [
    {"num":"004","name":"市 奈津実","work_days":9,"gross":148452,"total_deduction":1230,"net":147222}
  ],
  "totals": {"gross": 0, "net": 0}
}
```

---

## 5. ガードレール（必須・福啓会の鉄則由来）

1. **誤社書込防止**：操作前に**画面表示中の事業所名 ⇄ 期待 `company_id` の一致をアサート**。不一致なら即中断（6社併存運用）。
2. **段階承認**：`finalize`/`publish` は `confirm=true`（人間承認フラグ）が無ければ実行しない。デフォルトは dry-run（読取のみ）。
3. **可逆性の明示**：`recalc` は未確定なら可逆、`publish` はメール通知が飛ぶため実質不可逆と戻り値に明記。
4. **pay_date基準の月マッピング**を内部で固定（労務対象月＝closing月、API/画面の月度＝pay_date月）。誤月実行を防ぐためツール側で `period_label` を返し、呼び出し側で照合。
5. **秘匿情報**：認証情報・storageStateはリポジトリにコミットしない（`.gitignore`）。`.env.example` のみ同梱。
6. **監査ログ**：実行時刻/事業所/月度/操作/結果を `audit_log.py` 互換で追記。

---

## 6. 既知のハマりどころ（先回り）

- freeeログインの2段階認証/CAPTCHA → 自動化困難。**storageState再利用**で常用回避、失効時のみ人間ログイン。
- 入力欄の高速タイプで文字落ち（既知。JuleaやKuracallで実証）→ 値投入はJS直接 or `fill()` 後に検証読取。
- 再計算の完了検知 → ボタンのスピナー/「保存しました」トースト/行の `calculated_at` 更新で判定。固定スリープ禁止。
- 確定済み(`fixed=true`)・手動上書き(`calc_status: overwritten`)の行は再計算で戻らない → 戻り値にフラグ表示。

---

## 7. オプション高速化（任意・低優先）

ブラウザを正本としつつ、`再計算/確定/公開` クリック時の**XHR（内部JSON API）をキャプチャ**し、storageStateのCookie＋CSRFトークンで直叩きする高速パスを将来追加可能。ただし**非公開・無保証・規約/安定性リスク**があるため、ブラウザ自動化をフォールバックとして必ず残すこと。

---

## 8. 受け入れ条件（Doneの定義）

- [ ] `freee_payroll_open(12350973, 2026, 6)` が9名の現在値表を返す。
- [ ] `freee_payroll_recalc(...)` 後、`work_days`/`gross`/`net` が勤怠反映後の値に更新される。
- [ ] 事業所不一致時に**確実に中断**する（誤社ガードのテスト）。
- [ ] `finalize`/`publish` は `confirm=true` 無しで**絶対に実行されない**。
- [ ] storageState再利用で2回目以降は無人実行（CAPTCHA非発生時）。

---

## 9. 実機検証で確定した事項（2026-06-27 / 旧12350973・読取＋dry-runで確認）

実画面（`https://p.secure.freee.co.jp/payroll_statements`）の挙動を確認し、`tools/freee_payroll_web/payroll_web.py` を以下へ更新済み。

- **実ボタン名（ACTION_LABELS / ACTION_CONFIRM_LABELS）**
  | 操作 | トリガー | 確認モーダル | 注意 |
  |------|----------|--------------|------|
  | recalc | **すべて再計算** | 再計算 | 直接編集(overwritten)・インポート行はスキップ。個別行の「再計算」は全体再計算にならない |
  | revert_auto | **自動計算に戻す** | 戻す | 個別明細ページ。直接編集を破棄→自動計算。確定済み行は不可 |
  | finalize | **給与明細を確定** | 確定 | |
  | publish | **明細の公開設定**（確定後に出現） | 設定 | role=button実在を確認(2026-06-28)。「Web明細を公開」表記は未確認のため後続に保持。通知が飛ぶ・実質不可逆。本番は publish_month_end.py |
  - 確認クリックは `role=dialog` 内を **exact 一致** で押す（「すべて再計算」⊃「再計算」の誤クリック防止）。
- **期間表記**：「○年○月度」は無く「**M月D日支払**」（pay_date基準）。`paydate_phrase()`/`assert_period_matches()`/`_read_period_label()` を支払日表記基準に整合。
- **給与ページ直リンク**：`payroll_statements#/{group_id}/{year}/{month}`。`group_id` は給与計算グループID（旧12350973 = **1502865**）。`DEFAULT_PAYROLL_GROUP_IDS` に既定登録、`FREEE_PAYROLL_GROUP_MAP_FILE` で追加可。未登録事業所はテキスト/ロールナビにフォールバック。
- **事業所切替**：`?company_id=` では切り替わらない。正本は `window.$FREEE_DATA.loginUser.company_id`。横断切替は `secure.freee.co.jp/login_to/{company_id}` で行い active id で再検証（`_select_company_if_needed`）。
- **新規ツール**：`freee_payroll_revert_auto`（CLI `revert-auto --employee <名 or 番号> [--confirm]`）。confirm無しは対象行の現状を返す dry-run。
- **一覧パース修正（検証中に判明した既存バグ）**：実画面は thead 9 列＋見出しなし操作列でボディ 10 列のためヘッダ不一致でフォールバックし氏名/番号が逆転していた→ヘッダをボディ幅に詰めてマップ。列順は **差引支給額(net) → 総支給額(gross) → 総控除額** で、金額は「245,353 (+125,863)」形式（括弧内は前月比）→括弧以降を除去。確定検知に「未確定に戻す」「確定：<日時>」表記を追加。
  - 検証結果：9名・`gross=net+控除`が全行整合（例 002: 245,353+48,900=294,253）。当期は確定済み（`fixed=true`）。
- **ボタン実在プローブ（2026-06-28・確定済み期間で読取のみ）**：`明細の公開設定`=role=button実在✅／`未確定に戻す`=button実在✅（=確定済みの証）。`すべて再計算`/`給与明細を確定`/`自動計算に戻す`は当期では未出現（確定済みでは再計算・確定ボタンが出ず、`自動計算に戻す`は個別明細ページにあるため）。これら3つの**ボタン実在の最終確認は、未確定の期 or 個別明細ページで write 検証時に行うこと**。

---

## 付録：今回の検証対象データ（おの歯科 旧12350973・2026年6月度＝5月勤務分）

9名在籍。給与明細は全員 `gross=0 / net=0 / work_days=0`・`fixed=false`・`calculated_at` が4月のまま＝**勤怠未反映の初期状態**。5月勤怠（`work_record_summaries/2026/6`）は登録済み。
- 単価未設定の要対応：**009 大西恵理（時給0・5月3日/637分勤務）**。
- 001 小野貴庸は役員報酬0化済み（is_board_member=true）。
