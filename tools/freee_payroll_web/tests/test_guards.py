from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from tools.freee_payroll_web.payroll_web import (
    ACTION_CONFIRM_LABELS,
    ACTION_LABELS,
    CompanyGuardError,
    ConfirmationRequiredError,
    DEFAULT_PAYROLL_GROUP_IDS,
    PeriodGuardError,
    PayrollWebAutomator,
    PayrollWebConfig,
    assert_company_matches,
    assert_period_matches,
    build_dry_run_result,
    pay_date_for_period,
    period_label,
    require_confirm,
    StorageStateManager,
)


class PayrollGuardTests(unittest.TestCase):
    def test_period_is_pay_date_basis(self) -> None:
        self.assertEqual(period_label(2026, 6), "2026年6月度")
        self.assertEqual(pay_date_for_period(2026, 6), "2026-06-30")

    def test_company_guard_accepts_expected_alias(self) -> None:
        matched = assert_company_matches("12350973", "おの歯科医院（旧）")
        self.assertIn(matched, {"おの歯科医院(旧)", "おの歯科医院（旧）"})

    def test_company_guard_rejects_wrong_company(self) -> None:
        with self.assertRaises(CompanyGuardError):
            assert_company_matches("12350973", "医療法人社団福啓会")

    def test_old_ono_does_not_accept_bare_new_name_by_default(self) -> None:
        with self.assertRaises(CompanyGuardError):
            assert_company_matches("12350973", "おの歯科医院")

    def test_new_ono_does_not_accept_old_name_by_default(self) -> None:
        with self.assertRaises(CompanyGuardError):
            assert_company_matches("12564166", "おの歯科医院（旧）")

    def test_period_guard_rejects_wrong_month(self) -> None:
        self.assertEqual(assert_period_matches(2026, 6, "2026年6月度 給与計算"), "2026年6月度")
        with self.assertRaises(PeriodGuardError):
            assert_period_matches(2026, 6, "2026年5月度 給与計算")

    def test_finalize_publish_need_confirm(self) -> None:
        with self.assertRaises(ConfirmationRequiredError):
            require_confirm("finalize", False)
        with self.assertRaises(ConfirmationRequiredError):
            require_confirm("publish", False)
        require_confirm("finalize", True)
        require_confirm("publish", True)

    def test_dry_run_result_never_executes_sensitive_action(self) -> None:
        payload = build_dry_run_result("publish", 12350973, 2026, 6)
        self.assertFalse(payload["executed"])
        self.assertTrue(payload["dry_run"])
        self.assertTrue(payload["requires_confirm"])
        self.assertEqual(payload["period_label"], "2026年6月度")

    def test_recalc_action_prefers_select_all(self) -> None:
        # 実機ボタンは「すべて再計算」。個別行の「再計算」を先に拾わないこと。
        self.assertEqual(ACTION_LABELS["recalc"][0], "すべて再計算")
        # 確認モーダルは exact「再計算」で、「すべて再計算」を誤クリックしない。
        self.assertIn("再計算", ACTION_CONFIRM_LABELS["recalc"])
        self.assertEqual(ACTION_LABELS["finalize"][0], "給与明細を確定")
        self.assertIn("自動計算に戻す", ACTION_LABELS["revert_auto"])

    def test_default_group_id_for_old_ono(self) -> None:
        self.assertEqual(DEFAULT_PAYROLL_GROUP_IDS["12350973"], "1502865")

    def test_resolve_group_id_from_local_map(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "group_map.json"
            path.write_text('{"12564166": "2000111"}', encoding="utf-8")
            config = PayrollWebConfig.from_args(group_map=str(path))
            automator = PayrollWebAutomator(config)
            self.assertEqual(automator._resolve_group_id("12564166"), "2000111")
            # 既定マップは維持される。
            self.assertEqual(automator._resolve_group_id("12350973"), "1502865")
            self.assertIsNone(automator._resolve_group_id("99999999"))

    def test_find_employee_row_matches_name_and_number(self) -> None:
        rows = [
            {"num": "004", "name": "市 奈津実"},
            {"num": "009", "name": "大西 恵理"},
        ]
        by_name = PayrollWebAutomator._find_employee_row(rows, "大西恵理")
        self.assertEqual(by_name["num"], "009")
        by_num = PayrollWebAutomator._find_employee_row(rows, "004")
        self.assertEqual(by_num["name"], "市 奈津実")
        self.assertIsNone(PayrollWebAutomator._find_employee_row(rows, "存在しない"))

    def test_encrypted_storage_state_round_trip(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json.enc"
            manager = StorageStateManager(path)
            state = {"cookies": [{"name": "session", "value": "secret"}], "origins": []}
            manager.write(state)
            self.assertNotIn(b"secret", path.read_bytes())
            self.assertEqual(manager.read(), state)


if __name__ == "__main__":
    unittest.main()
