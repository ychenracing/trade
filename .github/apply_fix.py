"""One-use, SHA-fenced edits; this file never enters the production tree."""
from pathlib import Path
import subprocess

EXPECTED = {
    'quantfusion/account/service.py': 'dc201cb827e1667e9a3889274c57156f2121b34d',
    'quantfusion/application/account_scan.py': '59eded0570e4b3981b7c8e9f3316384ba26a6c01',
    'quantfusion/application/daily_scan.py': '7fa1894667e165e2a8147b26bce303454dc59467',
    'quantfusion/application/daily_report.py': '387a77a7560d8e0cd55dfc772912c2361928885d',
}
for name, oid in EXPECTED.items():
    assert subprocess.check_output(['git', 'hash-object', name], text=True).strip() == oid, name
assert subprocess.check_output(['git', 'ls-files', '*AGENTS.md'], text=True).splitlines() == ['AGENTS.md']

def replace(name, before, after):
    path = Path(name)
    text = path.read_text()
    assert text.count(before) == 1, (name, before[:100], text.count(before))
    path.write_text(text.replace(before, after))

replace('quantfusion/account/service.py', '\ndef _compute_target_shares(', '''
def partial_sell_quantity(symbol: str, shares: int) -> int:
    """Bound a partial-sale estimate without increasing the requested reduction.

    STAR ordinary declarations start at 200 shares, then increment by one:
    https://edu.sse.com.cn/tib/ysptj/c/4768085.shtml
    Whole-balance exits retain their existing odd-lot and T+1 handling at the
    caller. This does not change replay fills or market-volume unit conversion.
    """
    star = symbol.startswith("68")
    quantity = floor_to_lot(shares, lot_size=1 if star else 100)
    return quantity if quantity >= (200 if star else 100) else 0


def _compute_target_shares(''')
replace('quantfusion/account/service.py', '    "compute_target_shares",\n', '    "compute_target_shares",\n    "partial_sell_quantity",\n')
replace('quantfusion/application/account_scan.py', '    compute_target_shares,\n', '    compute_target_shares,\n    partial_sell_quantity,\n')
replace('quantfusion/application/account_scan.py', '''            executable = min(desired, row["sellable_shares"])
            # Existing full-position advisories keep their original T+1
            # semantics; a new partial reduction uses executable board lots.
            if desired < row["shares"]:
                executable = floor_to_lot(executable)
            row.update(action="SELL" if row["action"] == "SELL" else "REDUCE_REVIEW",
                       recommended_shares=executable, blocked_shares=desired-executable,
                       execution_status=("EXECUTABLE" if executable == desired else
                                         "PARTIALLY_T1_BLOCKED" if executable else "T1_BLOCKED"),''', '''            available = min(desired, row["sellable_shares"])
            executable = available
            # Preserve whole-balance exits, but never round a partial risk
            # reduction up merely to satisfy an exchange minimum quantity.
            if desired < row["shares"]:
                executable = partial_sell_quantity(reduction.symbol, available)
            quantity_blocked = executable < available
            row.update(action="SELL" if row["action"] == "SELL" else "REDUCE_REVIEW",
                       recommended_shares=executable, blocked_shares=desired-executable,
                       execution_status=(
                           "PARTIALLY_QUANTITY_BLOCKED" if quantity_blocked and executable else
                           "QUANTITY_BLOCKED" if quantity_blocked else
                           "EXECUTABLE" if executable == desired else
                           "PARTIALLY_T1_BLOCKED" if executable else "T1_BLOCKED"),''')
replace('quantfusion/application/daily_report.py', '''        elif row.get("execution_status") == "SELLABLE_UNKNOWN":''', '''        elif row.get("execution_status") in {"QUANTITY_BLOCKED", "PARTIALLY_QUANTITY_BLOCKED"}:
            lines.append("卖出限制：部分减仓数量受最低申报数量或整手规则限制，可能同时受快照可卖数量限制；未自动增加卖出量，受限部分不代表已完成风险减仓。")
        elif row.get("execution_status") == "SELLABLE_UNKNOWN":''')

name = 'quantfusion/application/daily_scan.py'
replace(name, 'from quantfusion.io.state_store import (', 'from quantfusion.io.artifacts import atomic_json\nfrom quantfusion.io.state_store import (')
path = Path(name)
text = path.read_text()
a = text.index('    # Best-effort update: false remains truthful if the update itself fails.')
b = text.index('    if risk_state_saved:\n        print(', a)
text = text[:a] + '''    # These writes are required for a complete publication. A failure must not
    # advance the success pointer or emit a new reading report. Never roll back
    # risk state that was already saved successfully.
    publication_error = ""
    try:
        if risk_state_saved or risk_state_save_error:
            artifact["risk_state_saved"] = risk_state_saved
            if risk_state_save_error:
                artifact["risk_state_save_error"] = risk_state_save_error
            # Retain the initial serializer's date/default=str semantics.
            normalized = json.loads(json.dumps(
                artifact, ensure_ascii=False, default=str, allow_nan=False,
            ))
            atomic_json(normalized, output_file)
        # An identity mismatch deliberately retains old state and valid sells;
        # this controlled degradation is not an I/O failure.
        if not risk_state_save_error:
            atomic_json({"file": output_file.name, "run_id": run_id,
                         "scan_date": end_date}, output_dir / "latest_success.json")
    except (OSError, ValueError, TypeError) as exc:
        publication_error = str(exc)

''' + text[b:]
path.write_text(text)
replace(name, '''    if risk_state_save_error:
        return 1
    publish_daily_report''', '''    if publication_error:
        print(f"  ✗ 结果发布未完成: {publication_error}")
        print("  本次运行失败；不生成新阅读报告，不回滚已保存的风险状态。")
    if risk_state_save_error or publication_error:
        return 1
    publish_daily_report''')
replace('README.md', '状态标记回写和成功索引更新是尽力操作，不是多文件整体事务。', '状态标记回写和成功索引更新失败也返回非零，不生成新阅读报告；已保存的风险状态不回滚。这不是多文件整体事务，读取索引仍须核对文件、日期和 run_id。')
replace('docs/ARCHITECTURE.md', '  -> 尽力回写信号中的保存状态\n  -> 无状态保存错误时尽力更新 latest_success.json', '  -> 回写信号中的保存状态；写入失败则本次运行失败\n  -> 无状态保存错误时更新 latest_success.json；写入失败则本次运行失败')
# Integrate regressions into existing test modules, not permanent extra tooling.
for payload, target in [
    ('sell_tests.txt', 'tests/unit/test_account_signal_sellability.py'),
    ('publication_tests.txt', 'tests/integration/test_daily_artifact_transactions.py'),
]:
    p = Path(target)
    p.write_text(p.read_text() + '\n\n' + Path('.github/' + payload).read_text())
