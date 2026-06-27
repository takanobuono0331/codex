"""Browser automation core for freee HR payroll operations.

This module intentionally keeps Playwright as a lazy runtime dependency so the
guard rails and MCP wrapper can be tested without a browser install.
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import json
import os
import re
import sys
import unicodedata
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import audit_log  # noqa: E402

AGENT_NAME = "freee_payroll_web"
FREEE_BASE_URL = "https://p.secure.freee.co.jp/"
FREEE_LOGIN_URL = "https://accounts.secure.freee.co.jp/login"


class PayrollWebError(RuntimeError):
    """Base error for the freee payroll web automation tool."""


class CompanyGuardError(PayrollWebError):
    """Raised when the screen company does not match the expected company_id."""


class PeriodGuardError(PayrollWebError):
    """Raised when the visible payroll period does not match the requested month."""


class ConfirmationRequiredError(PayrollWebError):
    """Raised when a sensitive operation lacks explicit confirmation."""


class PlaywrightDependencyError(PayrollWebError):
    """Raised when Playwright is not installed in the active Python env."""


class AuthRequiredError(PayrollWebError):
    """Raised when the storage state is missing or expired."""


class StorageStateError(PayrollWebError):
    """Raised when encrypted Playwright storageState cannot be read or written."""


DEFAULT_COMPANY_ALIASES: dict[str, tuple[str, ...]] = {
    # Keep old/new Ono strict. If freee displays only the bare name, configure a
    # local company_map file after visually confirming the account switcher.
    "12350973": ("おの歯科医院(旧)", "おの歯科医院（旧）"),
    "12564166": ("おの歯科医院(新)", "おの歯科医院（新）", "おの歯科医院（新・既定）"),
    "11047602": ("医療法人社団福啓会", "福啓会"),
    "12056622": ("(株)セラミックファクトリー東京", "株式会社セラミックファクトリー東京", "CFT"),
    "12056630": ("(株)MDCイノベーション", "株式会社MDCイノベーション", "MDC"),
    "12564258": ("オーラルアートクリニック西麻布",),
    "12593806": ("開発用テスト事業所",),
}

# 実機検証(2026-06-27, おの歯科 旧12350973)で確定した実ボタン名。
# - recalc: 一覧トップの「すべて再計算」→確認モーダルの「再計算」。
#   注意: 直接編集(overwritten)/インポートで入った行は「すべて再計算」ではスキップされる。
#   個別行の「再計算」を拾うと全体再計算にならないため、"すべて再計算"を最優先にする。
# - finalize: 「給与明細を確定」→モーダル「確定」。
# - publish: 「Web明細を公開」(2026-06-27時点 未検証・要確認)。
ACTION_LABELS: dict[str, tuple[str, ...]] = {
    "recalc": ("すべて再計算", "全て再計算", "再計算する", "給与を再計算"),
    "finalize": ("給与明細を確定", "給与を確定する", "確定する", "給与確定"),
    # 確定後に出現する公開導線は実画面では「明細の公開設定」(=パネルを開く)。
    # 「Web明細を公開」表記は未確認のため後続に残す。本番運用は publish_month_end.py 参照。
    "publish": ("明細の公開設定", "Web明細を公開", "Web明細公開", "公開する"),
    "revert_auto": ("自動計算に戻す", "自動計算に戻る", "自動計算へ戻す"),
}

# 各操作の確認モーダルで押すボタン。トリガーボタンの部分文字列
# (例「すべて再計算」⊃「再計算」)を誤って再クリックしないよう、モーダル内を
# exact 一致で押す（_click_action 参照）。
ACTION_CONFIRM_LABELS: dict[str, tuple[str, ...]] = {
    "recalc": ("再計算", "実行", "OK"),
    "finalize": ("確定", "確定する", "OK"),
    # 公開パネルの確定ボタンは「設定」(ラジオ:今すぐ公開/日時指定, 通知チェックは既定オフ)。
    "publish": ("設定", "公開", "公開する", "OK"),
    "revert_auto": ("戻す", "自動計算に戻す", "OK", "実行"),
}

CONFIRMATION_LABELS = ("実行", "OK", "はい", "確認", "保存", "確定", "確定する", "公開", "公開する")

# 給与明細一覧の直リンク。ハッシュルートに「給与計算グループID」を含む。
# 例: https://p.secure.freee.co.jp/payroll_statements#/1502865/2026/6
#   (1502865 = おの歯科 旧12350973 の給与計算グループID, 実機確認 2026-06-27)
PAYROLL_STATEMENTS_URL_TEMPLATE = (
    "https://p.secure.freee.co.jp/payroll_statements#/{group_id}/{year}/{month}"
)

# company_id -> 給与計算グループID。実機で確認できたものだけを既定に入れる。
# 追加は FREEE_PAYROLL_GROUP_MAP_FILE (JSON: {"12350973": "1502865"}) で上書き可。
DEFAULT_PAYROLL_GROUP_IDS: dict[str, str] = {
    "12350973": "1502865",
}

# 実画面(2026-06-27)のヘッダ実例:
#   氏名 / 従業員番号 / 勤務・賃金設定 / 給与形態 /
#   差引支給額/手取り (前月比) / 総支給額 (前月比) / 総控除額 (前月比) /
#   最終計算日時 / ステータス / (見出しなしの操作列)
# 注意: freeeは「差引支給額(net)」が「総支給額(gross)」より左に並ぶ。
# また "支給額" は「差引支給額」の部分文字列でもあるため、net を gross より先に
# 判定し、gross は "総支給" 系のみで拾う(_header_key は dict 順で最初の一致を返す)。
HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "num": ("従業員番号", "社員番号", "番号", "No.", "No"),
    "name": ("氏名", "従業員名", "名前", "スタッフ"),
    "work_days": ("出勤日数", "勤務日数", "労働日数"),
    "net": ("差引支給", "手取り", "振込額"),
    "total_deduction": ("総控除", "控除合計", "控除額", "控除"),
    "gross": ("総支給", "支給合計", "給与支給額"),
    "status": ("ステータス", "状態", "確定"),
    "calculated_at": ("最終計算日時", "計算日時", "再計算日時", "更新日時"),
}


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _repo_path(value: str | None, default: Path) -> Path:
    raw = value.strip() if value else ""
    if not raw:
        return default
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def period_label(year: int, month: int) -> str:
    _validate_period(year, month)
    return f"{year}年{month}月度"


def pay_date_for_period(year: int, month: int) -> str:
    _validate_period(year, month)
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, last_day).isoformat()


def _validate_period(year: int, month: int) -> None:
    if not (2000 <= int(year) <= 2100):
        raise ValueError(f"year out of range: {year}")
    if not (1 <= int(month) <= 12):
        raise ValueError(f"month out of range: {month}")


def normalize_company_name(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "")
    return re.sub(r"\s+", "", text)


def _load_company_aliases(path: Path | None) -> dict[str, tuple[str, ...]]:
    aliases: dict[str, tuple[str, ...]] = dict(DEFAULT_COMPANY_ALIASES)
    if not path or not path.exists():
        return aliases
    payload = json.loads(path.read_text(encoding="utf-8"))
    for company_id, value in payload.items():
        if isinstance(value, dict):
            names = value.get("names") or value.get("aliases") or []
        else:
            names = value
        if isinstance(names, str):
            names = [names]
        cleaned = tuple(str(name).strip() for name in names if str(name).strip())
        if cleaned:
            aliases[str(company_id)] = cleaned
    return aliases


def _load_payroll_group_ids(path: Path | None) -> dict[str, str]:
    groups: dict[str, str] = dict(DEFAULT_PAYROLL_GROUP_IDS)
    if not path or not path.exists():
        return groups
    payload = json.loads(path.read_text(encoding="utf-8"))
    for company_id, value in payload.items():
        if isinstance(value, dict):
            gid = value.get("group_id") or value.get("payroll_group_id")
        else:
            gid = value
        if gid is not None and str(gid).strip():
            groups[str(company_id)] = str(gid).strip()
    return groups


def assert_company_matches(
    company_id: int | str,
    company_name_on_screen: str,
    aliases: dict[str, tuple[str, ...]] | None = None,
) -> str:
    """Assert that the visible freee company belongs to the requested company_id."""

    cid = str(company_id)
    mapping = aliases or DEFAULT_COMPANY_ALIASES
    expected = mapping.get(cid)
    if not expected:
        raise CompanyGuardError(
            f"company_id={cid} is not configured. Add it to FREEE_PAYROLL_COMPANY_MAP_FILE."
        )

    actual_norm = normalize_company_name(company_name_on_screen)
    if not actual_norm:
        raise CompanyGuardError(f"company_id={cid}: no company name was visible on screen.")

    for alias in expected:
        alias_norm = normalize_company_name(alias)
        if alias_norm and (alias_norm == actual_norm or alias_norm in actual_norm):
            return alias

    raise CompanyGuardError(
        "freee company guard failed: "
        f"expected company_id={cid} aliases={list(expected)!r}, "
        f"screen={company_name_on_screen!r}"
    )


def assert_company_id_matches(expected_company_id: int | str, active_company_id: int | None) -> int:
    """Authoritative guard: the freee active company_id must equal the requested one.

    Old (12350973) and new (12564166) Ono both display as the bare name
    "おの歯科医院", so name matching cannot tell them apart. The only reliable
    discriminator is $FREEE_DATA.loginUser.company_id embedded in the page.
    """

    expected = int(expected_company_id)
    if active_company_id is None:
        raise CompanyGuardError(
            f"could not read active company_id from freee (expected {expected}). "
            "Refusing to act because old/new Ono share the same display name."
        )
    if int(active_company_id) != expected:
        raise CompanyGuardError(
            f"freee company guard failed: expected company_id={expected}, "
            f"active company_id on screen={active_company_id}"
        )
    return expected


def paydate_phrase(year: int, month: int) -> str:
    """freee給与明細一覧の実表記「M月D日支払」（pay_date基準）。例: 2026/6度→「6月30日支払」。"""

    last_day = calendar.monthrange(year, month)[1]
    return f"{month}月{last_day}日支払"


def assert_period_matches(year: int, month: int, period_text_on_screen: str) -> str:
    actual_norm = normalize_company_name(period_text_on_screen)
    # 正本: freee実画面の支払日表記「M月D日支払」（pay_date基準）。
    phrase = paydate_phrase(year, month)
    if normalize_company_name(phrase) in actual_norm:
        return phrase
    # フォールバック: 旧来想定の「YYYY年M月度」表記。
    expected = period_label(year, month)
    if normalize_company_name(expected) in actual_norm:
        return expected
    raise PeriodGuardError(
        f"freee payroll period guard failed: expected={phrase!r} or {expected!r}, "
        f"screen={period_text_on_screen!r}"
    )


def build_dry_run_result(
    action: str,
    company_id: int | str,
    year: int,
    month: int,
    *,
    reason: str = "confirm=true is required",
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(snapshot or {})
    payload.update(
        {
            "operation": action,
            "executed": False,
            "dry_run": True,
            "requires_confirm": True,
            "reason": reason,
            "company_id": int(company_id),
            "period_label": period_label(year, month),
            "pay_date": pay_date_for_period(year, month),
        }
    )
    return payload


def require_confirm(action: str, confirm: bool) -> None:
    if action in {"finalize", "publish"} and not confirm:
        raise ConfirmationRequiredError(f"{action} requires confirm=true")


@dataclass(slots=True)
class PayrollWebConfig:
    storage_state_path: Path = field(
        default_factory=lambda: _repo_path(
            os.environ.get("FREEE_PAYROLL_STORAGE_STATE"),
            REPO_ROOT / "tools/freee_payroll_web/.secrets/freee_storage_state.json.enc",
        )
    )
    company_map_path: Path | None = field(
        default_factory=lambda: (
            _repo_path(os.environ.get("FREEE_PAYROLL_COMPANY_MAP_FILE"), Path())
            if os.environ.get("FREEE_PAYROLL_COMPANY_MAP_FILE")
            else None
        )
    )
    group_map_path: Path | None = field(
        default_factory=lambda: (
            _repo_path(os.environ.get("FREEE_PAYROLL_GROUP_MAP_FILE"), Path())
            if os.environ.get("FREEE_PAYROLL_GROUP_MAP_FILE")
            else None
        )
    )
    screenshot_dir: Path = field(
        default_factory=lambda: _repo_path(
            os.environ.get("FREEE_PAYROLL_SCREENSHOT_DIR"),
            REPO_ROOT / "outputs/freee_payroll_web/screenshots",
        )
    )
    url_template: str = field(default_factory=lambda: os.environ.get("FREEE_PAYROLL_URL_TEMPLATE", ""))
    company_selector: str = field(default_factory=lambda: os.environ.get("FREEE_PAYROLL_COMPANY_SELECTOR", ""))
    headless: bool = field(default_factory=lambda: _bool_env("FREEE_PAYROLL_HEADLESS", False))
    timeout_ms: int = field(default_factory=lambda: _int_env("FREEE_PAYROLL_TIMEOUT_MS", 30000))
    slow_mo_ms: int = field(default_factory=lambda: _int_env("FREEE_PAYROLL_SLOW_MO_MS", 0))

    @classmethod
    def from_args(
        cls,
        *,
        storage_state: str | None = None,
        company_map: str | None = None,
        group_map: str | None = None,
        headless: bool | None = None,
    ) -> "PayrollWebConfig":
        config = cls()
        if storage_state:
            config.storage_state_path = _repo_path(storage_state, config.storage_state_path)
        if company_map:
            config.company_map_path = _repo_path(company_map, Path())
        if group_map:
            config.group_map_path = _repo_path(group_map, Path())
        if headless is not None:
            config.headless = headless
        return config


def _import_playwright():
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - depends on operator env
        raise PlaywrightDependencyError(
            "Playwright is not installed. Install it in the runtime env with "
            "`python -m pip install playwright` and then `python -m playwright install chromium`."
        ) from exc
    return async_playwright, PlaywrightTimeoutError


class StorageStateManager:
    """Read/write Playwright storageState, encrypting files that end in .enc."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.encrypted = path.suffix == ".enc"
        raw_key_path = os.environ.get("FREEE_PAYROLL_STORAGE_KEY_PATH")
        self.key_path = _repo_path(raw_key_path, path.with_suffix(".key")) if raw_key_path else path.with_suffix(".key")

    def read(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        if self.encrypted:
            payload = self._fernet().decrypt(self.path.read_bytes())
            return json.loads(payload.decode("utf-8"))
        return json.loads(self.path.read_text(encoding="utf-8"))

    def write(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
        if self.encrypted:
            self.path.write_bytes(self._fernet().encrypt(payload))
        else:
            self.path.write_bytes(payload)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _fernet(self):
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:  # pragma: no cover
            raise StorageStateError(
                "Encrypted storageState requires cryptography. Use a .json path for plaintext, "
                "or install cryptography in the runtime env."
            ) from exc

        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        if self.key_path.exists():
            key = self.key_path.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            self.key_path.write_bytes(key)
            try:
                os.chmod(self.key_path, 0o600)
            except OSError:
                pass
        return Fernet(key)


def _parse_number(text: str) -> int | float | None:
    cleaned = unicodedata.normalize("NFKC", text or "")
    # freee は「245,353 (+125,863)」のように本体値の右に前月比を括弧書きする。
    # 括弧以降を捨てて本体値だけを取る(全角括弧はNFKCで半角化済み)。
    cleaned = re.split(r"[(（]", cleaned, maxsplit=1)[0]
    cleaned = cleaned.replace(",", "").replace("¥", "").replace("円", "").strip()
    cleaned = re.sub(r"[^0-9.\-]", "", cleaned)
    if cleaned in {"", "-", "."}:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return int(value) if value.is_integer() else value


def _header_key(header: str) -> str | None:
    norm = normalize_company_name(header).lower()
    for key, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if normalize_company_name(alias).lower() in norm:
                return key
    return None


class PayrollWebAutomator:
    def __init__(self, config: PayrollWebConfig | None = None) -> None:
        self.config = config or PayrollWebConfig()
        self.company_aliases = _load_company_aliases(self.config.company_map_path)
        self.payroll_group_ids = _load_payroll_group_ids(self.config.group_map_path)

    def _resolve_group_id(self, company_id: int | str) -> str | None:
        return self.payroll_group_ids.get(str(company_id))

    @asynccontextmanager
    async def _page(self):
        async_playwright, _ = _import_playwright()
        storage_manager = StorageStateManager(self.config.storage_state_path)
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=self.config.headless,
            slow_mo=self.config.slow_mo_ms,
        )
        storage = storage_manager.read()
        context = await browser.new_context(storage_state=storage, locale="ja-JP")
        page = await context.new_page()
        page.set_default_timeout(self.config.timeout_ms)
        try:
            yield page
        finally:
            try:
                storage_manager.write(await context.storage_state())
            finally:
                await context.close()
                await browser.close()
                await playwright.stop()

    async def login(self) -> dict[str, Any]:
        async with self._page() as page:
            await page.goto(FREEE_LOGIN_URL, wait_until="domcontentloaded")
            print("Log in to freee in the opened browser window. The storageState will be saved after login.")
            await page.wait_for_url(re.compile(r"https://p\.secure\.freee\.co\.jp/.*"), timeout=300_000)
            return {
                "ok": True,
                "storage_state_path": str(self.config.storage_state_path),
                "url": page.url,
            }

    async def open(self, company_id: int | str, year: int, month: int) -> dict[str, Any]:
        async with self._page() as page:
            await self._open_period(page, company_id, year, month)
            snapshot = await self._snapshot(page, company_id, year, month)
            self._audit("open", company_id, year, month, "read", "ok", snapshot=snapshot)
            return snapshot

    async def recalc(
        self,
        company_id: int | str,
        year: int,
        month: int,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        async with self._page() as page:
            await self._open_period(page, company_id, year, month)
            before = await self._snapshot(page, company_id, year, month)
            if dry_run:
                result = dict(before)
                result.update(
                    {
                        "operation": "recalc",
                        "executed": False,
                        "dry_run": True,
                        "reversibility": "未確定なら再計算は可逆です。確定済み・手動上書き行は戻りません。",
                        "note": "「すべて再計算」は直接編集(overwritten)・インポート行をスキップします。"
                        "個別行を自動計算へ戻すには revert_auto を使ってください。",
                    }
                )
                self._audit("recalc", company_id, year, month, "read", "dry_run", snapshot=result)
                return result

            # 実機ボタン「すべて再計算」→確認モーダル「再計算」。
            await self._click_action(page, "recalc")
            await self._wait_for_idle_after_action(page)
            after = await self._snapshot(page, company_id, year, month)
            after.update(
                {
                    "operation": "recalc",
                    "executed": True,
                    "dry_run": False,
                    "reversibility": "未確定なら再計算は可逆です。確定済み・手動上書き行は戻りません。",
                    "note": "「すべて再計算」は直接編集(overwritten)・インポート行をスキップします。"
                    "それらは revert_auto で自動計算へ戻してから再計算してください。",
                }
            )
            self._audit("recalc", company_id, year, month, "write", "ok", snapshot=after)
            return after

    async def finalize(
        self,
        company_id: int | str,
        year: int,
        month: int,
        *,
        confirm: bool = False,
        approver: str = "",
    ) -> dict[str, Any]:
        if not confirm:
            snapshot = await self.open(company_id, year, month)
            result = build_dry_run_result("finalize", company_id, year, month, snapshot=snapshot)
            result["irreversible_warning"] = "給与確定は人間承認後にのみ実行します。"
            self._audit("finalize", company_id, year, month, "read", "dry_run", snapshot=result)
            return result

        async with self._page() as page:
            await self._open_period(page, company_id, year, month)
            await self._click_action(page, "finalize")
            await self._wait_for_idle_after_action(page)
            snapshot = await self._snapshot(page, company_id, year, month)
            snapshot.update(
                {
                    "operation": "finalize",
                    "executed": True,
                    "dry_run": False,
                    "approver": approver or "human-confirmed",
                }
            )
            self._audit("finalize", company_id, year, month, "write", "ok", approver=approver, snapshot=snapshot)
            return snapshot

    async def publish(
        self,
        company_id: int | str,
        year: int,
        month: int,
        *,
        confirm: bool = False,
        approver: str = "",
    ) -> dict[str, Any]:
        if not confirm:
            snapshot = await self.open(company_id, year, month)
            result = build_dry_run_result("publish", company_id, year, month, snapshot=snapshot)
            result["irreversible_warning"] = "Web明細公開は従業員へ通知が飛ぶため、confirm=trueなしでは実行しません。"
            self._audit("publish", company_id, year, month, "read", "dry_run", snapshot=result)
            return result

        async with self._page() as page:
            await self._open_period(page, company_id, year, month)
            await self._click_action(page, "publish")
            await self._wait_for_idle_after_action(page)
            snapshot = await self._snapshot(page, company_id, year, month)
            snapshot.update(
                {
                    "operation": "publish",
                    "executed": True,
                    "dry_run": False,
                    "approver": approver or "human-confirmed",
                    "irreversible_warning": "Web明細公開済み。従業員通知が送られている可能性があります。",
                }
            )
            self._audit("publish", company_id, year, month, "write", "ok", approver=approver, snapshot=snapshot)
            return snapshot

    async def revert_auto(
        self,
        company_id: int | str,
        year: int,
        month: int,
        employee: str,
        *,
        confirm: bool = False,
        approver: str = "",
    ) -> dict[str, Any]:
        """個別明細の直接編集(上書き)を破棄し自動計算へ戻す（実機ボタン「自動計算に戻す」）。

        employee は従業員名 or 従業員番号の部分一致。確定済み行には使えない。
        confirm=true が無ければ対象行の現状だけを返す dry-run。
        """

        if not str(employee).strip():
            raise PayrollWebError("revert_auto requires a non-empty employee (name or number)")

        async with self._page() as page:
            await self._open_period(page, company_id, year, month)
            snapshot = await self._snapshot(page, company_id, year, month)
            target = self._find_employee_row(snapshot.get("rows", []), employee)

            if not confirm:
                result = build_dry_run_result(
                    "revert_auto", company_id, year, month, snapshot=snapshot
                )
                result["employee_query"] = employee
                result["target_row"] = target
                result["reversibility"] = (
                    "自動計算に戻すと直接編集した値は破棄され、自動計算値で上書きされます。"
                    "確定済み行には使えません。"
                )
                if target is None:
                    result["warning"] = f"employee={employee!r} に一致する行が見つかりませんでした。"
                self._audit(
                    "revert_auto", company_id, year, month, "read", "dry_run",
                    approver=approver, snapshot=result,
                )
                return result

            if target is None:
                raise PayrollWebError(
                    f"revert_auto: employee={employee!r} に一致する行が見つかりません"
                )
            if target.get("fixed"):
                raise PayrollWebError(
                    f"revert_auto: employee={employee!r} は確定済みのため自動計算に戻せません"
                )

            await self._open_employee_detail(page, target)
            await self._click_action(page, "revert_auto")
            await self._wait_for_idle_after_action(page)
            # 一覧へ戻って結果を確認。
            await self._open_period(page, company_id, year, month)
            after = await self._snapshot(page, company_id, year, month)
            after.update(
                {
                    "operation": "revert_auto",
                    "executed": True,
                    "dry_run": False,
                    "employee_query": employee,
                    "target_row": self._find_employee_row(after.get("rows", []), employee),
                    "approver": approver or "human-confirmed",
                    "reversibility": "直接編集の値は破棄済み。自動計算値に戻りました。",
                }
            )
            self._audit(
                "revert_auto", company_id, year, month, "write", "ok",
                approver=approver, snapshot=after,
            )
            return after

    @staticmethod
    def _find_employee_row(
        rows: list[dict[str, Any]], employee: str
    ) -> dict[str, Any] | None:
        query = normalize_company_name(str(employee))
        if not query:
            return None
        for row in rows:
            num = normalize_company_name(str(row.get("num") or ""))
            name = normalize_company_name(str(row.get("name") or ""))
            if query in {num, name} or (name and query in name) or (num and query == num):
                return row
        return None

    async def _open_employee_detail(self, page, row: dict[str, Any]) -> None:
        for key in ("name", "num"):
            value = str(row.get(key) or "").strip()
            if value and await self._try_click_text(page, value, exact=False, timeout=3000):
                await self._soft_wait(page)
                return
        raise PayrollWebError(
            f"revert_auto: 従業員行を開けませんでした (name={row.get('name')!r} num={row.get('num')!r})"
        )

    async def screenshot(self, company_id: int | str, year: int, month: int) -> dict[str, Any]:
        async with self._page() as page:
            await self._open_period(page, company_id, year, month)
            snapshot = await self._snapshot(page, company_id, year, month)
            self.config.screenshot_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
            path = self.config.screenshot_dir / f"freee_payroll_{company_id}_{year}_{month:02d}_{stamp}.png"
            await page.screenshot(path=str(path), full_page=True)
            snapshot.update({"screenshot_path": str(path)})
            self._audit("screenshot", company_id, year, month, "read", "ok", snapshot=snapshot)
            return snapshot

    async def _open_period(self, page, company_id: int | str, year: int, month: int) -> None:
        _validate_period(year, month)
        await page.goto(FREEE_BASE_URL, wait_until="domcontentloaded")
        await self._ensure_authenticated(page)
        await self._select_company_if_needed(page, company_id)

        group_id = self._resolve_group_id(company_id)
        if self.config.url_template:
            url = self.config.url_template.format(
                company_id=company_id, group_id=group_id or "", year=year, month=month
            )
            await page.goto(url, wait_until="domcontentloaded")
        elif group_id:
            # 実機確認済みの直リンク。給与計算グループID基準のハッシュルート。
            url = PAYROLL_STATEMENTS_URL_TEMPLATE.format(group_id=group_id, year=year, month=month)
            await page.goto(url, wait_until="domcontentloaded")
        else:
            await self._navigate_to_payroll(page)
            await self._select_period(page, year, month)

        await self._soft_wait(page)
        active = await self._read_active_company_id(page)
        assert_company_id_matches(company_id, active)
        period_on_screen = await self._read_period_label(page, year, month)
        assert_period_matches(year, month, period_on_screen)

    async def _ensure_authenticated(self, page) -> None:
        if "accounts.secure.freee.co.jp" in page.url:
            raise AuthRequiredError(
                "freee login is required. Run `python -m tools.freee_payroll_web.cli login` "
                "with a headed browser, complete 2FA/CAPTCHA, then retry."
            )
        text = await self._safe_body_text(page)
        if "ログイン" in text and "メールアドレス" in text and "パスワード" in text:
            raise AuthRequiredError(
                "freee login form is visible. Refresh storageState with the `login` CLI command."
            )

    async def _select_company_if_needed(self, page, company_id: int | str) -> None:
        cid = int(company_id)
        active = await self._read_active_company_id(page)
        if active == cid:
            return

        # 実機検証(2026-06-27): `?company_id=` ではアクティブ事業所は切り替わらない。
        # 正本のアクティブ事業所は window.$FREEE_DATA.loginUser.company_id。
        # 横断セッション切替は login_to で行い、必ず active id で再検証する。
        # Name-based switching is unsafe here: old/new Ono share one display name.
        try:
            await page.goto(f"https://secure.freee.co.jp/login_to/{cid}", wait_until="domcontentloaded")
            await self._soft_wait(page)
            await page.goto(FREEE_BASE_URL, wait_until="domcontentloaded")
            await self._soft_wait(page)
            active = await self._read_active_company_id(page)
            if active == cid:
                return
        except Exception:  # noqa: BLE001 - fall through to the guard error
            active = await self._read_active_company_id(page)

        raise CompanyGuardError(
            f"Unable to switch to company_id={cid}. Active company_id={active}. "
            "`?company_id=` does not switch the active company; this login may lack access, "
            "or refresh storageState with the company already active via the `login` command."
        )

    async def _navigate_to_payroll(self, page) -> None:
        # Role/text based navigation; freee changes class names often.
        for label in ("給与", "給与計算"):
            await self._try_click_text(page, label, exact=False, timeout=2500)
            await self._soft_wait(page)

    async def _select_period(self, page, year: int, month: int) -> None:
        label = period_label(year, month)
        if label in await self._safe_body_text(page):
            await self._try_click_text(page, label, exact=False, timeout=1500)
            return

        for opener in ("月度", "支給月", "対象月", "給与月"):
            if await self._try_click_text(page, opener, exact=False, timeout=1500):
                break
        if not await self._try_click_text(page, label, exact=False, timeout=5000):
            # Leave the page as-is, but the returned period_label will make the
            # mismatch visible to the caller before any write action.
            return
        await self._soft_wait(page)

    async def _snapshot(self, page, company_id: int | str, year: int, month: int) -> dict[str, Any]:
        active_company_id = await self._read_active_company_id(page)
        assert_company_id_matches(company_id, active_company_id)
        company_name = await self._read_company_name(page)
        period_on_screen = await self._read_period_label(page, year, month)
        assert_period_matches(year, month, period_on_screen)
        rows = await self._read_rows(page)
        fixed = await self._detect_fixed(page, rows)
        totals = {
            "gross": sum(row.get("gross") or 0 for row in rows),
            "net": sum(row.get("net") or 0 for row in rows),
            "total_deduction": sum(row.get("total_deduction") or 0 for row in rows),
        }
        return {
            "company_id": int(company_id),
            "active_company_id": active_company_id,
            "company_name_on_screen": company_name,
            "period_label": period_label(year, month),
            "period_label_on_screen": period_on_screen,
            "pay_date": pay_date_for_period(year, month),
            "fixed": fixed,
            "rows": rows,
            "row_count": len(rows),
            "totals": totals,
            "url": page.url,
        }

    async def _read_rows(self, page) -> list[dict[str, Any]]:
        tables = await page.locator("table").all()
        for table in tables:
            rows = await self._parse_table(table)
            if rows:
                return rows
        return []

    async def _parse_table(self, table) -> list[dict[str, Any]]:
        header_texts: list[str] = []
        header_cells = await table.locator("thead tr th, thead tr td").all()
        for cell in header_cells:
            header_texts.append((await cell.inner_text()).strip())
        keys = [_header_key(text) for text in header_texts]

        body_rows = await table.locator("tbody tr").all()
        parsed: list[dict[str, Any]] = []
        for tr in body_rows:
            cells = await tr.locator("th, td").all()
            values = [(await cell.inner_text()).strip() for cell in cells]
            if not any(values):
                continue
            row: dict[str, Any] = {}
            if keys and any(keys):
                # freeeは見出しなしの操作列を末尾に持ち、thead(9) < tbody(10) になる。
                # ヘッダをボディ幅までNoneで詰めて添字を保ったままマップする。
                row_keys = list(keys)
                if len(row_keys) < len(values):
                    row_keys += [None] * (len(values) - len(row_keys))
                for key, value in zip(row_keys, values, strict=False):
                    if key:
                        row.setdefault(key, value)
                row["raw_cells"] = values
            else:
                row = self._parse_row_fallback(values)
            if not row.get("name") and len(values) >= 2:
                # ヘッダが拾えなかった場合のみ: 数字だけのセルを従業員番号とみなす。
                num_idx = next(
                    (i for i, v in enumerate(values[:2]) if _parse_number(v) is not None), 0
                )
                name_idx = 1 - num_idx if num_idx < 2 else 1
                row.setdefault("num", values[num_idx])
                row.setdefault("name", values[name_idx])
            if row.get("name"):
                self._coerce_row(row)
                parsed.append(row)
        return parsed

    def _parse_row_fallback(self, values: list[str]) -> dict[str, Any]:
        row: dict[str, Any] = {}
        if values:
            row["num"] = values[0]
        if len(values) > 1:
            row["name"] = values[1]
        numeric = [_parse_number(v) for v in values]
        money_values = [v for v in numeric if isinstance(v, int) and abs(v) >= 1000]
        if money_values:
            row["gross"] = money_values[0]
        if len(money_values) >= 2:
            row["net"] = money_values[-1]
        for value, parsed in zip(values, numeric, strict=False):
            if parsed is not None and "日" in value and "work_days" not in row:
                row["work_days"] = parsed
        row["raw_cells"] = values
        return row

    def _coerce_row(self, row: dict[str, Any]) -> None:
        for key in ("work_days", "gross", "total_deduction", "net"):
            if key in row:
                row[key] = _parse_number(str(row[key]))
        for key in ("num", "name", "status", "calculated_at"):
            if key in row and row[key] is not None:
                row[key] = str(row[key]).strip()
        status_text = row.get("status", "")
        row["fixed"] = "確定" in status_text and "未確定" not in status_text
        raw_text = " ".join(str(v) for v in row.get("raw_cells", []))
        row["overwritten"] = "手動" in raw_text or "上書" in raw_text

    async def _detect_fixed(self, page, rows: list[dict[str, Any]]) -> bool:
        if rows and all(bool(row.get("fixed")) for row in rows):
            return True
        text = await self._safe_body_text(page)
        if "確定済" in text or "給与確定済" in text:
            return True
        # 給与明細一覧では確定済みのとき「未確定に戻す」ボタンと「確定：<日時>」表記が出る。
        if "未確定に戻す" in text:
            return True
        return bool(re.search(r"確定[:：]\s*\d{4}\s*年", text))

    async def _click_action(self, page, action: str) -> None:
        labels = ACTION_LABELS[action]
        for label in labels:
            if await self._try_click_button_or_text(page, label, timeout=5000):
                await self._soft_wait(page)
                await self._confirm_action(page, action)
                return
        raise PayrollWebError(f"Could not find action button for {action}: {labels}")

    async def _confirm_action(self, page, action: str) -> None:
        # 確認モーダル内のボタンを exact 一致で押す。トリガー名の部分文字列
        # (例「すべて再計算」⊃「再計算」)を誤って再クリックしないよう dialog にスコープする。
        confirm_labels = ACTION_CONFIRM_LABELS.get(action, CONFIRMATION_LABELS)
        for label in confirm_labels:
            if await self._click_dialog_button(page, label, timeout=2000):
                await self._soft_wait(page)
                return
        # フォールバック: role=dialog でない実装向けの汎用クリック。
        await self._click_first_confirmation(page)

    async def _click_dialog_button(self, page, label: str, *, timeout: int) -> bool:
        for dialog_selector in ("[role='dialog']", "[role='alertdialog']", "[aria-modal='true']", ".modal"):
            try:
                dialog = page.locator(dialog_selector).last
                button = dialog.get_by_role("button", name=label, exact=True)
                await button.first.click(timeout=timeout)
                return True
            except Exception:  # noqa: BLE001 - try the next dialog container
                continue
        # ダイアログ要素が無い実装向け: ページ全体から exact 一致のボタンを押す。
        try:
            await page.get_by_role("button", name=label, exact=True).first.click(timeout=timeout)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _click_first_confirmation(self, page) -> None:
        for label in CONFIRMATION_LABELS:
            if await self._try_click_button_or_text(page, label, timeout=1200):
                await self._soft_wait(page)
                return

    async def _try_click_button_or_text(self, page, label: str, *, timeout: int) -> bool:
        try:
            button = page.get_by_role("button", name=re.compile(re.escape(label)))
            await button.first.click(timeout=timeout)
            return True
        except Exception:  # noqa: BLE001 - Playwright locator miss
            return await self._try_click_text(page, label, exact=False, timeout=timeout)

    async def _try_click_text(self, page, label: str, *, exact: bool, timeout: int) -> bool:
        try:
            await page.get_by_text(label, exact=exact).first.click(timeout=timeout)
            return True
        except Exception:  # noqa: BLE001 - Playwright locator miss
            return False

    async def _wait_for_idle_after_action(self, page) -> None:
        await self._soft_wait(page)
        for toast in ("完了", "保存しました", "更新しました", "公開しました", "確定しました"):
            try:
                await page.get_by_text(toast, exact=False).first.wait_for(timeout=2000)
                break
            except Exception:  # noqa: BLE001
                continue
        await self._soft_wait(page)

    async def _soft_wait(self, page) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:  # noqa: BLE001
            await page.wait_for_timeout(500)

    async def _read_active_company_id(self, page) -> int | None:
        """Read the authoritative active company_id from $FREEE_DATA.loginUser.

        This is the only reliable old/new discriminator (both display the bare
        name "おの歯科医院"). Falls back to a regex over the page HTML.
        """

        try:
            val = await page.evaluate(
                "() => (window.$FREEE_DATA && window.$FREEE_DATA.loginUser "
                "&& window.$FREEE_DATA.loginUser.company_id) || null"
            )
            if val:
                return int(val)
        except Exception:  # noqa: BLE001 - fall back to HTML scan
            pass
        try:
            html = await page.content()
            match = re.search(r'"loginUser"\s*:\s*\{[^}]*?"company_id"\s*:\s*(\d+)', html)
            if match:
                return int(match.group(1))
        except Exception:  # noqa: BLE001
            pass
        return None

    async def _read_company_name(self, page) -> str:
        if self.config.company_selector:
            try:
                text = await page.locator(self.config.company_selector).first.inner_text(timeout=3000)
                if text.strip():
                    return text.strip()
            except Exception:  # noqa: BLE001
                pass

        header_chunks: list[str] = []
        for selector in ("header", "nav", "[role='banner']"):
            try:
                text = await page.locator(selector).first.inner_text(timeout=1500)
                if text.strip():
                    header_chunks.append(text.strip())
            except Exception:  # noqa: BLE001
                continue
        text = "\n".join(header_chunks) or await self._safe_body_text(page)
        norm_text = normalize_company_name(text)
        for aliases in self.company_aliases.values():
            for alias in aliases:
                if normalize_company_name(alias) in norm_text:
                    return alias
        return text[:300].strip()

    async def _read_period_label(self, page, year: int, month: int) -> str:
        text = await self._safe_body_text(page)
        norm = normalize_company_name(text)
        # 正本: freee実画面の「M月D日支払」表記(pay_date基準)。
        phrase = paydate_phrase(year, month)
        if normalize_company_name(phrase) in norm:
            return f"{year}年{phrase}"
        expected = period_label(year, month)
        if normalize_company_name(expected) in norm:
            return expected
        match = re.search(r"\d{4}\s*年\s*\d{1,2}\s*月度", text)
        if match:
            return match.group(0)
        wage = re.search(
            r"賃金計算期間[^\d]*\d{4}年\d{1,2}月\d{1,2}日[^\d]*\d{4}年\d{1,2}月\d{1,2}日", text
        )
        if wage:
            return wage.group(0)
        return text[:300].strip()

    async def _safe_body_text(self, page) -> str:
        try:
            return await page.locator("body").inner_text(timeout=3000)
        except Exception:  # noqa: BLE001
            return ""

    def _audit(
        self,
        action: str,
        company_id: int | str,
        year: int,
        month: int,
        mode: str,
        result: str,
        *,
        approver: str = "",
        snapshot: dict[str, Any] | None = None,
    ) -> None:
        company_name = ""
        rows = 0
        fixed = ""
        if snapshot:
            company_name = str(snapshot.get("company_name_on_screen") or "")
            rows = int(snapshot.get("row_count") or len(snapshot.get("rows") or []))
            fixed = str(snapshot.get("fixed", ""))
        audit_log.log(
            AGENT_NAME,
            action,
            actor="codex",
            company_id=company_id,
            company_name=company_name,
            target=period_label(year, month),
            mode=mode,
            approver=approver,
            result=result,
            detail=f"period={period_label(year, month)} pay_date={pay_date_for_period(year, month)} rows={rows} fixed={fixed}",
        )


def _json_print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


async def run_cli(args: argparse.Namespace) -> dict[str, Any]:
    config = PayrollWebConfig.from_args(
        storage_state=args.storage_state,
        company_map=args.company_map,
        group_map=getattr(args, "group_map", None),
        headless=args.headless,
    )
    automator = PayrollWebAutomator(config)
    if args.command == "login":
        return await automator.login()
    if args.command == "open":
        return await automator.open(args.company, args.year, args.month)
    if args.command == "recalc":
        return await automator.recalc(args.company, args.year, args.month, dry_run=args.dry_run)
    if args.command == "revert-auto":
        return await automator.revert_auto(
            args.company,
            args.year,
            args.month,
            args.employee,
            confirm=args.confirm,
            approver=args.approver,
        )
    if args.command == "finalize":
        return await automator.finalize(
            args.company,
            args.year,
            args.month,
            confirm=args.confirm,
            approver=args.approver,
        )
    if args.command == "publish":
        return await automator.publish(
            args.company,
            args.year,
            args.month,
            confirm=args.confirm,
            approver=args.approver,
        )
    if args.command == "screenshot":
        return await automator.screenshot(args.company, args.year, args.month)
    raise ValueError(f"unknown command: {args.command}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="freee HR payroll web automation")
    parser.add_argument("--storage-state", default=None)
    parser.add_argument("--company-map", default=None)
    parser.add_argument(
        "--group-map",
        default=None,
        help="JSON map of company_id -> 給与計算グループID (FREEE_PAYROLL_GROUP_MAP_FILE)",
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="override FREEE_PAYROLL_HEADLESS",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="open freee login and save storageState")
    login.set_defaults(needs_period=False)

    for name in ("open", "recalc", "revert-auto", "finalize", "publish", "screenshot"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--company", required=True, help="freee company_id")
        cmd.add_argument("--year", required=True, type=int, help="pay_date basis year")
        cmd.add_argument("--month", required=True, type=int, help="pay_date basis month")
        if name == "recalc":
            cmd.add_argument("--dry-run", action="store_true")
        if name == "revert-auto":
            cmd.add_argument(
                "--employee",
                required=True,
                help="従業員名 or 従業員番号(部分一致)。直接編集を破棄し自動計算へ戻す対象",
            )
            cmd.add_argument("--confirm", action="store_true")
            cmd.add_argument("--approver", default="")
        if name in {"finalize", "publish"}:
            cmd.add_argument("--confirm", action="store_true")
            cmd.add_argument("--approver", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = asyncio.run(run_cli(args))
    except PayrollWebError as exc:
        _json_print({"ok": False, "error": type(exc).__name__, "message": str(exc)})
        return 2
    _json_print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
