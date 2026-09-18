"""Research: ordinary preserve defers only immaterial multi-group overshoot."""
from quantfusion.config.engine import default_engine_config
from quantfusion.risk.account_budget import plan_account_risk_budget


def test_immaterial_multi_group_overshoot_defers_held_trims():
    """Diversified ordinary books tolerate overshoot within one daily-loss."""
    cfg = default_engine_config()
    # optical + memory → multi-group; equity/cap chosen for immaterial overshoot
    books = [(0, '300308', 'turtle_breakout', 2600, 100.),
             (1, '603986', 'dual_ma', 2600, 100.)]
    receipt, actions = plan_account_risk_budget(
        880_000., 1_000_000., cfg, books, [],
        lambda symbol: float(symbol == '603986'), date_str='2026-01-05',
        preserve_strategy_valid_holdings=True,
    )
    assert receipt['gross_before'] > receipt['gross_cap']
    overshoot = receipt['gross_before'] - receipt['gross_cap']
    assert overshoot <= 880_000. * cfg['daily_loss_limit'] + 1e-6
    assert actions == []
    assert receipt['ordinary_held_trim_deferred'] is True
    assert receipt['strategy_valid_holdings_preserved'] is True


def test_immaterial_single_group_overshoot_still_trims():
    """Concentrated single-group books keep main's full ordinary reserve trim."""
    cfg = default_engine_config()
    books = [(0, '300308', 'turtle_breakout', 2600, 100.),
             (1, '300502', 'dual_ma', 2600, 100.)]  # both optical
    receipt, actions = plan_account_risk_budget(
        880_000., 1_000_000., cfg, books, [],
        lambda symbol: float(symbol == '300502'), date_str='2026-01-05',
        preserve_strategy_valid_holdings=True,
    )
    assert receipt['gross_before'] > receipt['gross_cap']
    overshoot = receipt['gross_before'] - receipt['gross_cap']
    assert overshoot <= 880_000. * cfg['daily_loss_limit'] + 1e-6
    assert len(actions) == 2
    assert receipt['ordinary_held_trim_deferred'] is False
    assert receipt['strategy_valid_holdings_preserved'] is False


def test_material_ordinary_overshoot_still_shares_reserve_trim():
    """Large ordinary overshoot still funds the reserve with shared pro-rata."""
    cfg = default_engine_config()
    books = [(0, '300308', 'turtle_breakout', 4000, 100.),
             (1, '603986', 'dual_ma', 4000, 100.)]
    receipt, actions = plan_account_risk_budget(
        900_000., 1_000_000., cfg, books, [],
        lambda symbol: float(symbol == '603986'), date_str='2026-01-05',
        preserve_strategy_valid_holdings=True,
    )
    assert receipt['gross_before'] > receipt['gross_cap']
    overshoot = receipt['gross_before'] - receipt['gross_cap']
    assert overshoot > 900_000. * cfg['daily_loss_limit']
    assert len(actions) == 2
    assert actions[0].shares == actions[1].shares
    assert all(0 < action.shares < 4000 for action in actions)
    assert 800_000. - sum(a.shares * a.price for a in actions) <= receipt['gross_cap'] + 1e-6
    assert receipt['ordinary_held_trim_deferred'] is False
    assert receipt['strategy_valid_holdings_preserved'] is False


def test_sufficient_reserve_does_not_reduce_valid_holdings():
    receipt, actions = plan_account_risk_budget(
        1_000_000., 1_000_000., default_engine_config(),
        [(0, '300308', 'turtle_breakout', 4000, 100.)], [],
        lambda _: 0., date_str='2026-01-05', preserve_strategy_valid_holdings=True,
    )
    assert actions == []
    assert receipt['strategy_valid_holdings_preserved'] is True
    assert receipt['ordinary_held_trim_deferred'] is False


def test_alert_path_does_not_defer_ordinary_held_trims():
    books = [(0, '300308', 'turtle_breakout', 8000, 100.)]
    receipt, _ = plan_account_risk_budget(
        900_000., 1_000_000., default_engine_config(), books, [],
        lambda _: 0., date_str='2026-01-05',
        preserve_strategy_valid_holdings=True,
        risk_alert_active=True,
    )
    assert receipt['ordinary_path_active'] is False
    assert receipt['ordinary_held_trim_deferred'] is False
