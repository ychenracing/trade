"""Research: retain each opportunity while funding the existing reserve."""
from quantfusion.config.engine import default_engine_config
from quantfusion.risk.account_budget import plan_account_risk_budget


def test_preserved_books_share_required_reserve_without_erasing_one_book():
    books = [(0, '300308', 'turtle_breakout', 4000, 100.),
             (1, '603986', 'dual_ma', 4000, 100.)]
    receipt, actions = plan_account_risk_budget(
        900_000., 1_000_000., default_engine_config(), books, [],
        lambda symbol: float(symbol == '603986'), date_str='2026-01-05',
        preserve_strategy_valid_holdings=True,
    )
    assert len(actions) == 2
    assert actions[0].shares == actions[1].shares
    assert all(0 < action.shares < 4000 for action in actions)
    assert 800_000. - sum(a.shares * a.price for a in actions) <= receipt['gross_cap']
    assert receipt['strategy_valid_holdings_preserved'] is False


def test_sufficient_reserve_does_not_reduce_valid_holdings():
    receipt, actions = plan_account_risk_budget(
        1_000_000., 1_000_000., default_engine_config(),
        [(0, '300308', 'turtle_breakout', 4000, 100.)], [],
        lambda _: 0., date_str='2026-01-05', preserve_strategy_valid_holdings=True,
    )
    assert actions == []
    assert receipt['strategy_valid_holdings_preserved'] is True
