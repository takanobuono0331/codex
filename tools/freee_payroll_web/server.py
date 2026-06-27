"""MCP stdio server exposing freee payroll web automation tools."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .payroll_web import PayrollWebAutomator

mcp = FastMCP(
    "freee-payroll-web",
    instructions=(
        "Operate freee HR payroll through a human-owned Playwright storageState. "
        "Never call finalize or publish without confirm=true from the human operator."
    ),
)


@mcp.tool()
async def freee_payroll_open(company_id: int, year: int, month: int) -> dict[str, Any]:
    """Open a payroll period and return the current employee payroll table."""

    return await PayrollWebAutomator().open(company_id, year, month)


@mcp.tool()
async def freee_payroll_recalc(
    company_id: int,
    year: int,
    month: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run payroll recalculation for the selected period, or preview with dry_run."""

    return await PayrollWebAutomator().recalc(company_id, year, month, dry_run=dry_run)


@mcp.tool()
async def freee_payroll_revert_auto(
    company_id: int,
    year: int,
    month: int,
    employee: str,
    confirm: bool = False,
    approver: str = "",
) -> dict[str, Any]:
    """Revert one employee's manual override back to auto-calc ("自動計算に戻す").

    employee matches by name or employee number. confirm=true is mandatory;
    otherwise returns a read-only dry-run describing the target row.
    """

    return await PayrollWebAutomator().revert_auto(
        company_id,
        year,
        month,
        employee,
        confirm=confirm,
        approver=approver,
    )


@mcp.tool()
async def freee_payroll_finalize(
    company_id: int,
    year: int,
    month: int,
    confirm: bool = False,
    approver: str = "",
) -> dict[str, Any]:
    """Finalize payroll. confirm=true is mandatory; otherwise returns read-only dry-run."""

    return await PayrollWebAutomator().finalize(
        company_id,
        year,
        month,
        confirm=confirm,
        approver=approver,
    )


@mcp.tool()
async def freee_payroll_publish(
    company_id: int,
    year: int,
    month: int,
    confirm: bool = False,
    approver: str = "",
) -> dict[str, Any]:
    """Publish web payslips. confirm=true is mandatory; this may notify employees."""

    return await PayrollWebAutomator().publish(
        company_id,
        year,
        month,
        confirm=confirm,
        approver=approver,
    )


@mcp.tool()
async def freee_payroll_screenshot(company_id: int, year: int, month: int) -> dict[str, Any]:
    """Capture a screenshot for visual verification."""

    return await PayrollWebAutomator().screenshot(company_id, year, month)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
