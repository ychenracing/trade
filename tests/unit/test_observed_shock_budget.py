"""A recent observed shock must constrain held and proposed correlated exposure."""
import pandas as pd
import pytest

from quantfusion.config.engine import default_engine_config
from quantfusion.domain.models import Signal
from quantfusion.risk import account_budget as budget


def complete_protection(
    books: list[tuple[int, str, str, int, float]],
    *,
    stop_ratio: float = 0.98,
) -> dict[tuple[int, str, str], budget.ProtectionEvidence]:
    return {
        (state, symbol, strategy): budget.ProtectionEvidence(
            price * stop_ratio,
            "test_close_known_stop",
            complete=True,
        )
        for state, symbol, strategy, _, price in books
    }


def test_shock_plan_reduces_existing_risk_and_blocks_buy_offset():
    cfg = default_engine_config()
    books = [(0, '300394', 'turtle_breakout', 19400, 145.79),
             (0, '300502', 'turtle_breakout', 17600, 263.36)]
    signal = Signal('300394', 'turtle_breakout', 'buy', 4000, 145.79,
                    signal_date='2025-09-03')
    receipt, actions = budget.plan_account_risk_budget(
        8470205.414495502, 9273963.414495502, cfg, books,
        [(0, signal, signal.target_shares * signal.price)], lambda _: 1.,
        date_str='2025-09-03',
        stress_by_symbol={'300394': .20, '300502': .20, '603986': .10})
    assert receipt['buy_scale'] == 0
    assert actions
    assert {action.symbol for action in actions} == {'300394', '300502'}
    remaining = sum((shares - sum(a.shares for a in actions if a.symbol == symbol))
                    * price * receipt['systemic_stress_fraction']
                    for _, symbol, _, shares, price in books)
    assert remaining <= receipt['remaining_loss_budget']
    assert all(shares > sum(a.shares for a in actions if a.symbol == symbol)
               for _, symbol, _, shares, _ in books)


def test_observed_shock_is_close_known_and_shared_across_industry():
    dates = pd.date_range('2025-01-01', periods=5)
    frames = {'300502': pd.DataFrame({'close': [100., 90., 92., 110., 1.]}, index=dates),
              '300394': pd.DataFrame({'close': [100., 100., 101., 102., 1.]}, index=dates)}
    cfg = default_engine_config()
    stress = budget.observed_shock_stress(frames, dates[2], cfg)
    assert stress['300502'] == pytest.approx(.10)
    assert stress['300394'] == pytest.approx(.10)
    truncated = {s: f.loc[:dates[2]] for s, f in frames.items()}
    assert budget.observed_shock_stress(truncated, dates[2], cfg) == stress
    assert budget.observed_shock_stress(frames, dates[3], cfg) == {}


def test_observed_shock_waits_for_account_level_confirmation():
    cfg = default_engine_config()
    books = [(0, '300394', 'turtle_breakout', 4000, 200.)]
    receipt, actions = budget.plan_account_risk_budget(
        970_000., 1_000_000., cfg, books, [], lambda _: 1.,
        date_str='2025-08-14', stress_by_symbol={'300394': .20},
        protection_by_book=complete_protection(books))
    assert receipt['observed_shock_confirmed'] is False
    assert receipt['observed_shock_stress'] == {}
    assert actions == []


def test_single_cluster_shock_does_not_force_healthy_winner_exit():
    cfg = default_engine_config()
    books = [(0, '300394', 'turtle_breakout', 4000, 200.)]
    receipt, actions = budget.plan_account_risk_budget(
        930_000., 1_000_000., cfg, books, [], lambda _: 1.,
        date_str='2026-04-24', stress_by_symbol={'300394': .20},
        protection_by_book=complete_protection(books))
    assert receipt['observed_shock_confirmed'] is False
    assert receipt['shocked_group_count'] == 1
    assert actions == []


def test_broad_but_nonsevere_pullback_preserves_established_books():
    cfg = default_engine_config()
    books = [(0, '300394', 'turtle_breakout', 4000, 200.)]
    receipt, actions = budget.plan_account_risk_budget(
        930_000., 1_000_000., cfg, books, [], lambda _: 1.,
        date_str='2026-06-05',
        stress_by_symbol={'300394': .078, '603986': .078},
        protection_by_book=complete_protection(books))
    assert receipt['observed_shock_confirmed'] is False
    assert actions == []


def test_confirmed_shock_episode_blocks_reentry_until_evidence_clears():
    cfg = default_engine_config()
    signal = Signal('300394', 'turtle_breakout', 'buy', 1000, 200.,
                    signal_date='2025-09-03')
    active, _ = budget.plan_account_risk_budget(
        950_000., 1_000_000., cfg, [], [(0, signal, 200_000.)], lambda _: 1.,
        date_str='2025-09-03', stress_by_symbol={'300394': .20},
        shock_episode_active=True)
    cleared, _ = budget.plan_account_risk_budget(
        950_000., 1_000_000., cfg, [], [(0, signal, 200_000.)], lambda _: 1.,
        date_str='2025-09-16', shock_episode_active=True)
    assert active['observed_shock_confirmed'] is False
    assert active['shock_episode_active'] is True
    assert active['buy_scale'] == 0
    assert cleared['shock_episode_active'] is False
    assert cleared['buy_scale'] > 0


def test_active_shock_episode_does_not_repeat_the_same_held_reduction():
    cfg = default_engine_config()
    books = [(0, '300394', 'turtle_breakout', 4000, 200.)]
    receipt, actions = budget.plan_account_risk_budget(
        850_000., 1_000_000., cfg, books, [], lambda _: 1.,
        date_str='2025-09-04',
        stress_by_symbol={'300394': .20, '603986': .10},
        shock_episode_active=True, shock_floor=860_000.)
    assert receipt['observed_shock_confirmed'] is False
    assert receipt['shock_episode_active'] is True
    assert receipt['reduction_suspended_during_episode'] is True
    assert actions == []


def test_confirmed_concentration_uses_existing_cycle_floor_for_one_reduction():
    cfg = default_engine_config()
    books = [
        (0, '300394', 'turtle_breakout', 4_000, 200.),
        (0, '603986', 'turtle_breakout', 100, 100.),
    ]
    evidence = {'300394': .20, '603986': .10}
    ordinary, ordinary_actions = budget.plan_account_risk_budget(
        910_000., 1_000_000., cfg, books, [], lambda _: 1.,
        date_str='2025-09-02', stress_by_symbol=evidence)
    protected, actions = budget.plan_account_risk_budget(
        910_000., 1_000_000., cfg, books, [], lambda _: 1.,
        date_str='2025-09-02', stress_by_symbol=evidence,
        shock_floor=860_000.)
    assert protected['observed_shock_confirmed'] is True
    assert protected['effective_policy_floor'] == 860_000.
    assert sum(action.shares for action in actions) > sum(
        action.shares for action in ordinary_actions)


def test_board_gap_budget_credits_a_new_independent_industry():
    cfg = default_engine_config()
    books = [(0, '300308', 'turtle_breakout', 4000, 10.)]
    optical = Signal('300502', 'turtle_breakout', 'buy', 1000, 20.,
                     signal_date='2026-01-05')
    equipment = Signal('688072', 'turtle_breakout', 'buy', 1000, 20.,
                       signal_date='2026-01-05')
    correlated, _ = budget.plan_account_risk_budget(
        92_000., 100_000., cfg, books, [(0, optical, 20_000.)], lambda _: 1.,
        date_str='2026-01-05')
    diversified, _ = budget.plan_account_risk_budget(
        92_000., 100_000., cfg, books, [(0, equipment, 20_000.)], lambda _: 1.,
        date_str='2026-01-05')
    assert diversified['buy_gap_scale'] > correlated['buy_gap_scale']


def test_strategy_valid_path_keeps_non_alert_independent_breakout():
    """A fully evidenced independent breakout can consume available loss budget."""
    cfg = default_engine_config()
    books = [(0, '300308', 'atr_channel', 44_300, 100.)]
    signal = Signal(
        '603986', 'atr_channel', 'buy', 14_400, 190.,
        stop_loss=160., signal_date='2025-09-12',
        fusion_votes=2, fusion_label='two_strategy_confirmation',
    )
    receipt, actions = budget.plan_account_risk_budget(
        9_020_000., 9_772_000., cfg, books,
        [(0, signal, signal.target_shares * signal.price)], lambda _: 1.,
        date_str='2025-09-12', preserve_strategy_valid_holdings=True,
        protection_by_book=complete_protection(books, stop_ratio=0.80),
    )
    assert receipt['ordinary_allocator_active'] is True
    assert 0.0 < receipt['buy_scales'][0] <= 1.0 + 1e-12
    assert receipt['quality_prioritized_buy_indexes'] == [0]
    # Envelope scale < 1.0 prevents full quality admission even for evidenced breakouts.
    assert receipt['quality_admitted_buy_indexes'] == []
    assert actions == []


def test_quality_exception_cannot_add_risk_in_level3_when_books_exceed_capacity():
    """Catch a fresh confirmed breakout turning a severe-risk book into a breach."""
    cfg = default_engine_config()
    books = [(0, '300308', 'atr_channel', 8_000, 10.)]
    signal = Signal(
        '603986', 'atr_channel', 'buy', 1_000, 10.,
        signal_date='2025-10-13',
        fusion_votes=2, fusion_label='two_strategy_confirmation',
    )
    receipt, _ = budget.plan_account_risk_budget(
        87_000., 100_000., cfg, books,
        [(0, signal, signal.target_shares * signal.price)], lambda _: 1.,
        date_str='2025-10-13', preserve_strategy_valid_holdings=True,
    )

    assert receipt['gross_before'] > receipt['gross_cap']
    assert receipt['quality_admission_capacity_available'] is False
    assert receipt['quality_admission_risk_state_available'] is False
    assert receipt['quality_admitted_buy_indexes'] == []
    assert receipt['buy_scales'] == [0.]


def test_shock_reduced_live_cycle_blocks_only_its_pyramid_add():
    """A shock exit must not be silently reversed inside the same live cycle."""
    cfg = default_engine_config()
    books = [(0, '300308', 'turtle_breakout', 800, 400.)]
    pyramid = Signal(
        '300308', 'turtle_breakout', 'buy', 500, 400.,
        signal_date='2025-09-10', reason='Turtle pyramid add (unit 2)',
    )
    fresh = Signal(
        '603986', 'turtle_breakout', 'buy', 500, 400.,
        signal_date='2025-09-10', reason='Turtle breakout(ADX=30.0)',
    )
    receipt, _ = budget.plan_account_risk_budget(
        1_000_000., 1_000_000., cfg, books,
        [(0, pyramid, 200_000.), (0, fresh, 200_000.)], lambda _: 1.,
        date_str='2025-09-10',
        shock_reduced_book_ids={(0, '300308', 'turtle_breakout')},
        confirmed_shock_reduced_book_ids={
            (0, '300308', 'turtle_breakout'),
        },
    )

    assert receipt['shock_reduced_pyramid_buy_indexes'] == [0]
    assert receipt['shock_reduced_pyramid_book_ids'] == [
        (0, '300308', 'turtle_breakout'),
    ]
    assert receipt['buy_scales'] == [0., 1.]


def test_crowded_portfolio_reserves_last_slot_after_single_strategy_shock_trim():
    """A same-symbol pyramid cannot consume the last diversification slot."""
    cfg = default_engine_config()
    books = [
        (0, symbol, 'turtle_breakout', 100, 10.)
        for symbol in ('300308', '300502', '300394', '603986', '688072')
    ]
    pyramid = Signal(
        '300308', 'turtle_breakout', 'buy', 100, 10.,
        signal_date='2025-11-06', reason='Turtle pyramid add (unit 2)',
    )
    receipt, _ = budget.plan_account_risk_budget(
        100_000., 100_000., cfg, books, [(0, pyramid, 1_000.)],
        lambda _: 1., date_str='2025-11-06',
        shock_reduced_book_ids={(0, '300308', 'turtle_breakout')},
        crowded_shock_reduced_book_ids={
            (0, '300308', 'turtle_breakout'),
        },
    )

    assert receipt['crowded_portfolio'] is True
    assert receipt['confirmed_shock_reduced_book_ids'] == []
    assert receipt['crowded_shock_reduced_book_ids'] == [
        (0, '300308', 'turtle_breakout'),
    ]
    assert receipt['shock_reduced_pyramid_buy_indexes'] == [0]
    assert receipt['buy_scales'] == [0.]


def test_single_vote_independent_breakout_keeps_ordinary_budget_scale():
    """Decision-quality admission requires independent strategy confirmation."""
    cfg = default_engine_config()
    books = [(0, '300308', 'atr_channel', 44_300, 100.)]
    signal = Signal(
        '603986', 'turtle_breakout', 'buy', 14_400, 190.,
        signal_date='2025-09-12', fusion_votes=1,
    )
    receipt, _ = budget.plan_account_risk_budget(
        9_020_000., 9_772_000., cfg, books,
        [(0, signal, signal.target_shares * signal.price)], lambda _: 1.,
        date_str='2025-09-12', preserve_strategy_valid_holdings=True,
    )
    assert receipt['buy_scales'] == [min(
        receipt['buy_gross_scale'], receipt['buy_gap_scale'],
    )]
    assert receipt['buy_scale'] < 1.


def test_confirmed_independent_turtle_keeps_ordinary_budget_scale():
    """Shorter-lived Turtle entries do not inherit durable ATR admission."""
    cfg = default_engine_config()
    books = [(0, '300308', 'atr_channel', 44_300, 100.)]
    signal = Signal(
        '603986', 'turtle_breakout', 'buy', 14_400, 190.,
        signal_date='2025-09-12', fusion_votes=2,
        fusion_label='two_strategy_confirmation',
    )
    receipt, _ = budget.plan_account_risk_budget(
        9_020_000., 9_772_000., cfg, books,
        [(0, signal, signal.target_shares * signal.price)], lambda _: 1.,
        date_str='2025-09-12', preserve_strategy_valid_holdings=True,
    )
    assert receipt['quality_admitted_buy_indexes'] == []
    assert receipt['buy_scale'] < 1.


def test_repeated_proven_low_gap_reentry_admits_paired_turtle_with_durable_atr():
    """A repeatedly proven reentry keeps priority when its debit fits."""
    cfg = default_engine_config()
    books = [(0, '300308', 'atr_channel', 44_300, 100.)]
    signal = Signal(
        '603986', 'turtle_breakout', 'buy', 14_400, 190.,
        stop_loss=160., signal_date='2026-01-07', fusion_votes=2,
        fusion_label='two_strategy_confirmation',
    )
    receipt, _ = budget.plan_account_risk_budget(
        9_020_000., 9_772_000., cfg, books,
        [(0, signal, signal.target_shares * signal.price)], lambda _: 1.,
        date_str='2026-01-07', preserve_strategy_valid_holdings=True,
        repeated_proven_reentry_symbols={'603986'},
        protection_by_book=complete_protection(books, stop_ratio=0.80),
    )
    assert receipt['ordinary_allocator_active'] is True
    assert 0.0 < receipt['buy_scales'][0] < 1.0
    assert receipt['quality_prioritized_buy_indexes'] == [0]
    # Scarce incumbent envelope blocks full repeated-reentry admission.
    assert receipt['repeated_reentry_admitted_buy_indexes'] == []


def test_high_gap_repeated_reentry_keeps_ordinary_turtle_budget():
    """Prior success cannot bypass stress that exceeds the two-day account model."""
    cfg = default_engine_config()
    books = [(0, '603986', 'atr_channel', 44_300, 100.)]
    signal = Signal(
        '300394', 'turtle_breakout', 'buy', 14_400, 190.,
        signal_date='2026-01-27', fusion_votes=2,
        fusion_label='two_strategy_confirmation',
    )
    receipt, _ = budget.plan_account_risk_budget(
        9_020_000., 9_772_000., cfg, books,
        [(0, signal, signal.target_shares * signal.price)], lambda _: 1.,
        date_str='2026-01-27', preserve_strategy_valid_holdings=True,
        repeated_proven_reentry_symbols={'300394'},
    )
    assert receipt['buy_scale'] < 1.
    assert receipt['repeated_reentry_admitted_buy_indexes'] == []


def test_same_day_strategy_handoff_gets_priority_without_bypassing_budget():
    """A filled fast-strategy exit prioritizes, but does not exempt, new risk."""
    cfg = default_engine_config()
    signal = Signal(
        '300308', 'dual_ma', 'buy', 1_000, 100.,
        stop_loss=90., signal_date='2026-04-01', fusion_votes=1,
    )
    receipt, actions = budget.plan_account_risk_budget(
        90_000., 100_000., cfg, [], [(0, signal, 100_000.)], lambda _: 1.,
        date_str='2026-04-01', preserve_strategy_valid_holdings=True,
        strategy_handoff_symbols={'300308'},
    )
    assert 0. < receipt['buy_scales'][0] < 1.
    assert receipt['quality_prioritized_buy_indexes'] == [0]
    assert receipt['handoff_admitted_buy_indexes'] == []
    assert receipt['ordinary_total_loss_debit'] <= receipt['remaining_loss_budget']
    assert actions == []


def test_fresh_dual_ma_without_strategy_handoff_keeps_ordinary_budget_scale():
    """A slow signal alone must not bypass the account loss budget."""
    cfg = default_engine_config()
    signal = Signal(
        '300308', 'dual_ma', 'buy', 1_000, 100.,
        signal_date='2026-04-01', fusion_votes=1,
    )
    receipt, _ = budget.plan_account_risk_budget(
        90_000., 100_000., cfg, [], [(0, signal, 100_000.)], lambda _: 1.,
        date_str='2026-04-01', preserve_strategy_valid_holdings=True,
    )
    assert receipt['buy_scale'] < 1.
    assert receipt['quality_admitted_buy_indexes'] == []


def test_proven_early_dual_transition_keeps_full_decision_size():
    """A completed durable trend can support an early cross-strategy transition."""
    cfg = default_engine_config()
    books = [(0, '603986', 'atr_channel', 44_300, 100.)]
    signal = Signal(
        '300308', 'dual_ma', 'buy', 1_000, 100.,
        signal_date='2026-04-01', fusion_votes=1,
    )
    receipt, actions = budget.plan_account_risk_budget(
        9_020_000., 9_772_000., cfg, books,
        [(0, signal, signal.target_shares * signal.price)], lambda _: 1.,
        date_str='2026-04-01', preserve_strategy_valid_holdings=True,
        proven_early_dual_book_ids={(0, '300308', 'dual_ma')},
    )
    assert receipt['buy_scales'] == [1.]
    assert receipt['proven_dual_admitted_buy_indexes'] == [0]
    assert actions == []


def test_alert_proven_dual_transition_uses_only_remaining_gross_capacity():
    """A proven re-entry can survive an alert without bypassing its gross cap."""
    cfg = default_engine_config()
    books = [(0, '603986', 'atr_channel', 2_650, 100.)]
    signal = Signal(
        '300308', 'dual_ma', 'buy', 1_000, 100.,
        signal_date='2026-04-01', fusion_votes=1,
    )

    receipt, actions = budget.plan_account_risk_budget(
        891_600., 1_038_745., cfg, books,
        [(0, signal, signal.target_shares * signal.price)], lambda _: 1.,
        date_str='2026-04-01', preserve_strategy_valid_holdings=True,
        risk_alert_active=True,
        proven_early_dual_book_ids={(0, '300308', 'dual_ma')},
    )

    assert 0. < receipt['buy_scales'][0] < 1.
    assert receipt['buy_scales'][0] == receipt['buy_gross_scale']
    assert receipt['alert_proven_dual_buy_indexes'] == [0]
    assert (
        receipt['gross_before']
        + receipt['buy_scales'][0] * signal.target_shares * signal.price
        <= receipt['gross_cap'] + 1e-8
    )
    assert actions == []


def test_drawdown_alert_blocks_buys_and_exits_weak_losing_book():
    """Catch an alert leaving a losing, below-trend book exposed until lock."""
    cfg = default_engine_config()
    books = [(0, '603986', 'turtle_breakout', 5_000, 100.)]
    signal = Signal(
        '300308', 'dual_ma', 'buy', 1_000, 100.,
        signal_date='2026-03-26',
    )
    receipt, actions = budget.plan_account_risk_budget(
        900_000., 1_000_000., cfg, books, [(0, signal, 100_000.)],
        lambda _: 1., date_str='2026-03-26',
        preserve_strategy_valid_holdings=True,
        risk_alert_active=True,
        weak_book_ids={(0, '603986', 'turtle_breakout')},
        shock_floor=860_000.,
    )
    assert receipt['buy_scale'] == 0.
    assert [(a.symbol, a.strategy_name, a.shares) for a in actions] == [
        ('603986', 'turtle_breakout', 5_000),
    ]


def test_drawdown_alert_readmits_fresh_risk_after_reductions_have_filled():
    """Catch a recovered empty book being frozen merely because alert remains on."""
    cfg = default_engine_config()
    signal = Signal(
        '300308', 'dual_ma', 'buy', 4_000, 100.,
        signal_date='2026-04-08',
    )
    receipt, actions = budget.plan_account_risk_budget(
        900_000., 1_000_000., cfg, [], [(0, signal, 400_000.)],
        lambda _: 1., date_str='2026-04-08',
        preserve_strategy_valid_holdings=True,
        risk_alert_active=True,
        shock_floor=860_000.,
    )
    assert 0. < receipt['buy_scale'] < 1.
    admitted = receipt['buy_scale'] * 400_000.
    assert 0. < admitted <= receipt['gross_cap']
    assert actions == []


def test_drawdown_alert_does_not_treat_cash_as_symbol_concentration():
    """The symbol limit is an account-equity weight, not an invested-book share."""
    cfg = default_engine_config()
    books = [(0, '300408', 'atr_channel', 2_500, 100.),
             (0, '300408', 'turtle_breakout', 2_500, 100.)]
    receipt, actions = budget.plan_account_risk_budget(
        900_000., 1_000_000., cfg, books, [], lambda _: 1.,
        date_str='2025-11-10', preserve_strategy_valid_holdings=True,
        risk_alert_active=True, shock_floor=820_000.,
    )
    assert receipt['alert_dominant_symbol'] is None
    assert receipt['alert_dominant_fraction'] == pytest.approx(500_000. / 900_000.)
    assert actions == []


def test_drawdown_alert_trims_material_overcap_invested_concentration():
    """A severely over-cap alert book must shed its dominant invested risk."""
    cfg = default_engine_config()
    books = [
        (0, '002384', 'atr_channel', 16_250, 100.),
        (0, '002384', 'turtle_breakout', 16_250, 100.),
        (0, '300308', 'atr_channel', 7_200, 100.),
        (0, '603986', 'atr_channel', 7_100, 100.),
        (0, '688300', 'atr_channel', 7_100, 100.),
    ]
    receipt, actions = budget.plan_account_risk_budget(
        8_360_000., 9_746_000., cfg, books, [], lambda _: 1.,
        date_str='2026-03-17', preserve_strategy_valid_holdings=True,
        risk_alert_active=True, shock_floor=8_041_000.,
    )

    assert receipt['alert_dominant_symbol'] == '002384'
    assert receipt['alert_dominant_equity_weight_breach'] is False
    assert receipt['alert_dominant_invested_concentration'] is True
    assert receipt['gross_before'] - receipt['gross_cap'] > (
        receipt['equity'] * cfg['daily_loss_limit']
    )
    assert actions
    assert {action.symbol for action in actions} == {'002384'}


def test_drawdown_alert_trims_actual_symbol_weight_breach_to_configured_limit():
    """A genuine equity-weight breach is reduced to max_symbol_weight."""
    cfg = default_engine_config()
    books = [(0, '300408', 'atr_channel', 3_500, 100.),
             (0, '300408', 'turtle_breakout', 3_500, 100.)]
    receipt, actions = budget.plan_account_risk_budget(
        900_000., 1_000_000., cfg, books, [], lambda _: 1.,
        date_str='2025-11-10', preserve_strategy_valid_holdings=True,
        risk_alert_active=True, shock_floor=820_000.,
    )

    assert receipt['alert_dominant_symbol'] == '300408'
    assert receipt['alert_dominant_equity_weight_breach'] is True
    assert receipt['alert_dominant_fraction'] == pytest.approx(700_000. / 900_000.)
    assert sum(action.shares for action in actions) == 1_600
    assert {action.symbol for action in actions} == {'300408'}
