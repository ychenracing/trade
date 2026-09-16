"""Apply the reviewed reliability refactor without replacing large source files."""

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise RuntimeError(f"expected one replacement in {path}, found {text.count(old)}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


# Production route controller: strict leader evidence + owned weak-strategy lifecycle.
replace_once(
    "quantfusion/engine/replay.py",
    "from quantfusion.engine.universe import BacktestEngine, SleeveBacktestEngine\n",
    "from quantfusion.engine.universe import BacktestEngine, SleeveBacktestEngine\n"
    "from quantfusion.engine.strategy_lifecycle import StrategyLifecycleRegistry\n",
)
replace_once(
    "quantfusion/engine/replay.py",
    '''        self._leader_cache: dict[str, tuple[str, ...]] = {}\n        self._weak_strategies: dict[\n            tuple[str, str], PositiveMomentumHoldStrategy\n        ] = {}\n''',
    '''        self._leader_cache: dict[str, LeaderSelection] = {}\n        self._weak_strategy_registry = StrategyLifecycleRegistry()\n''',
)
replace_once(
    "quantfusion/engine/replay.py",
    '''    def _leaders(self, symbols: Sequence[str], date_str: str) -> tuple[str, ...]:\n        cached = self._leader_cache.get(date_str)\n        if cached is not None:\n            return cached\n        try:\n            selected = select_positive_momentum_leaders(\n                tuple(symbols),\n                data_dir=self.leader_data_dir,\n                as_of=date_str,\n            ).selected_symbols\n        except (OSError, RuntimeError, ValueError):\n            selected = ()\n        self._leader_cache[date_str] = tuple(selected)\n        return tuple(selected)\n''',
    '''    def _leaders(self, symbols: Sequence[str], date_str: str) -> tuple[str, ...]:\n        selection = self._leader_cache.get(date_str)\n        if selection is None:\n            selection = select_positive_momentum_leaders(\n                tuple(symbols),\n                data_dir=self.leader_data_dir,\n                as_of=date_str,\n            )\n            self._leader_cache[date_str] = selection\n        if selection.status != "valid":\n            self.events.append(\n                {\n                    "date": date_str,\n                    "event": "leader_selection_failure",\n                    "status": selection.status,\n                    "unavailable_symbols": list(selection.unavailable_symbols),\n                    "invalid_symbols": list(selection.invalid_symbols),\n                    "health": selection.health.as_dict(),\n                }\n            )\n        selection.require_valid("production route")\n        return tuple(selection.selected_symbols)\n''',
)
replace_once(
    "quantfusion/engine/replay.py",
    '''            current_assets = state.sleeve._total_assets(state.data_map, date)\n            for symbol in symbols_dict:\n                key = (str(state.sleeve.sleeve_name), symbol)\n                strategy = self._weak_strategies.get(key)\n                if strategy is None:\n                    cfg = dict(state.sleeve.cfg)\n                    cfg.update(_weak_regime_config(max(len(leaders), 1)))\n                    strategy = PositiveMomentumHoldStrategy(cfg)\n                    self._weak_strategies[key] = strategy\n                # Dynamic weak-route strategies own real positions and therefore\n                # must participate in every liquidation path. Keep them in the\n                # external registry so the sleeve risk/sector/route controls can\n                # find them without the core signal loop evaluating them a second\n                # time on the same close.\n                registered = state.sleeve.external_strategy_instances.setdefault(\n                    symbol, []\n                )\n                if strategy not in registered:\n                    registered.append(strategy)\n                if symbol not in leaders and strategy.position is None:\n                    continue\n''',
    '''            current_assets = state.sleeve._total_assets(state.data_map, date)\n            sleeve_name = str(state.sleeve.sleeve_name)\n            for symbol in symbols_dict:\n                entry = self._weak_strategy_registry.get(sleeve_name, symbol)\n                if symbol not in leaders and (\n                    entry is None or entry.strategy.position is None\n                ):\n                    continue\n                if entry is None:\n                    cfg = dict(state.sleeve.cfg)\n                    cfg.update(_weak_regime_config(max(len(leaders), 1)))\n                    entry = self._weak_strategy_registry.acquire(\n                        sleeve_name,\n                        symbol,\n                        lambda cfg=cfg: PositiveMomentumHoldStrategy(cfg),\n                    )\n                strategy = entry.strategy\n                self._weak_strategy_registry.activate(state.sleeve, entry)\n''',
)
replace_once(
    "quantfusion/engine/replay.py",
    '''        sleeve_rows = []\n        for state in states:\n''',
    '''        active_weak_symbols = (\n            self._weak_episode_leaders\n            if route in {\n                RegimeRoute.WEAK.value,\n                RegimeRoute.TRANSITION_TO_TREND.value,\n            } and not self._carry_trend_book\n            else ()\n        )\n        for state in states:\n            self._weak_strategy_registry.reconcile(\n                state.sleeve,\n                current_symbols=symbols_dict,\n                active_symbols=active_weak_symbols,\n                pending=state.pending,\n                date=date_str,\n            )\n\n        sleeve_rows = []\n        for state in states:\n''',
)
replace_once(
    "quantfusion/engine/replay.py",
    '''        cooldowns = {\n            f"{sleeve}:{symbol}": {\n                "cooldown_end": strategy._cooldown_end,\n                "exit_reason": strategy._exit_reason,\n                "failures": strategy._failures,\n            }\n            for (sleeve, symbol), strategy in sorted(self._weak_strategies.items())\n        }\n''',
    '''        cooldowns = {\n            f"{entry.sleeve_name}:{entry.symbol}": {\n                "cooldown_end": entry.strategy._cooldown_end,\n                "exit_reason": entry.strategy._exit_reason,\n                "failures": entry.strategy._failures,\n            }\n            for entry in self._weak_strategy_registry.entries()\n        }\n''',
)
replace_once(
    "quantfusion/engine/replay.py",
    '''            "weak_cooldowns": cooldowns,\n        }\n''',
    '''            "weak_cooldowns": cooldowns,\n            "weak_strategy_lifecycle": self._weak_strategy_registry.snapshot(),\n        }\n''',
)
replace_once(
    "quantfusion/engine/replay.py",
    '''        name = (\n            "positive_momentum_hold" if leaders.selected_symbols else "cash_preservation"\n        )\n''',
    '''        leaders.require_valid("current production decision")\n        name = (\n            "positive_momentum_hold" if leaders.selected_symbols else "cash_preservation"\n        )\n''',
)
replace_once(
    "quantfusion/engine/replay.py",
    '''        selection_boundary: str | None = None,\n    ) -> DeploymentDecision:\n''',
    '''        selection_boundary: str | None = None,\n        allow_degraded_leaders: bool = False,\n    ) -> DeploymentDecision:\n''',
)
replace_once(
    "quantfusion/engine/replay.py",
    '''        name = "positive_momentum_hold" if leaders.selected_symbols else "cash_preservation"\n        reason = (\n''',
    '''        if not allow_degraded_leaders:\n            leaders.require_valid("deployment decision")\n        name = "positive_momentum_hold" if leaders.selected_symbols else "cash_preservation"\n        reason = (\n''',
)
replace_once(
    "quantfusion/engine/replay.py",
    '''            leader_data_dir=leader_data_dir,\n            selection_boundary=selection_boundary,\n        )\n''',
    '''            leader_data_dir=leader_data_dir,\n            selection_boundary=selection_boundary,\n            allow_degraded_leaders=allow_unavailable_symbols,\n        )\n''',
)
replace_once(
    "quantfusion/engine/replay.py",
    '''            decision = replace(\n                decision,\n                name=(\n''',
    '''            if not allow_unavailable_symbols:\n                leaders.require_valid("forced weak deployment")\n            decision = replace(\n                decision,\n                name=(\n''',
)

# Allocation scoring keeps the exact numeric fallback while making degradation observable.
replace_once(
    "quantfusion/engine/ensemble_allocation.py",
    "from dataclasses import replace\n",
    "from dataclasses import dataclass, replace\n",
)
replace_once(
    "quantfusion/engine/ensemble_allocation.py",
    '''\n\nclass EnsembleAllocationMixin:\n''',
    '''\n\n@dataclass(frozen=True, slots=True)\nclass AllocationScoreFailure:\n    sleeve: str\n    error_type: str\n    message: str\n\n    def as_dict(self) -> dict[str, str]:\n        return {\n            "sleeve": self.sleeve,\n            "error_type": self.error_type,\n            "message": self.message,\n        }\n\n\n@dataclass(frozen=True, slots=True)\nclass AllocationScoreView:\n    """Callable allocation scores with structured degradation metadata."""\n\n    sleeve_scores: tuple[dict[str, float], ...]\n    failures: tuple[AllocationScoreFailure, ...] = ()\n\n    @property\n    def status(self) -> str:\n        return "degraded" if self.failures else "valid"\n\n    def __call__(self, symbol: str) -> float:\n        samples = [\n            float(scores.get(symbol, 0.0)) for scores in self.sleeve_scores\n        ]\n        return float(np.mean(samples)) if samples else 0.0\n\n    def as_event(self, date: str) -> dict[str, Any]:\n        return {\n            "date": date,\n            "event": "allocation_score_degraded",\n            "status": self.status,\n            "failed_sleeves": [failure.sleeve for failure in self.failures],\n            "failures": [failure.as_dict() for failure in self.failures],\n        }\n\n\nclass EnsembleAllocationMixin:\n''',
)
replace_once(
    "quantfusion/engine/ensemble_allocation.py",
    '''    @staticmethod\n    def _overlay_allocation_score(states: list[_PreparedSleeveRun], date: pd.Timestamp):\n        """Mean held-book score across sleeves, used to rank laggards for trim.\n\n        A risk exit compares live account holdings only.  Letting an unheld\n        add-one universe member into this cross-section can change the selected\n        trim without adding any position or risk, which makes the exit depend\n        on irrelevant candidate-pool composition.\n        """\n        held = EnsembleAllocationMixin._held_portfolio_symbols(states)\n        sleeve_scores: list[dict[str, float]] = []\n        for state in states:\n            ranked_data = (\n                {\n                    symbol: frame\n                    for symbol, frame in state.data_map.items()\n                    if symbol in held\n                }\n                if held\n                else state.data_map\n            )\n            try:\n                sleeve_scores.append(\n                    state.sleeve._allocation_scores(ranked_data, date)\n                )\n            except Exception:\n                sleeve_scores.append({})\n\n        def _score(symbol: str) -> float:\n            samples = [float(scores.get(symbol, 0.0)) for scores in sleeve_scores]\n            return float(np.mean(samples)) if samples else 0.0\n\n        return _score\n''',
    '''    @staticmethod\n    def _overlay_allocation_score(\n        states: list[_PreparedSleeveRun], date: pd.Timestamp\n    ) -> AllocationScoreView:\n        """Mean held-book score with explicit, stable degradation semantics.\n\n        The legacy zero-score fallback is retained so valid economic behavior\n        and degraded execution remain stable.  Failures are now represented as\n        data and emitted through each sleeve's existing risk-event channel.\n        """\n        held = EnsembleAllocationMixin._held_portfolio_symbols(states)\n        sleeve_scores: list[dict[str, float]] = []\n        failures: list[AllocationScoreFailure] = []\n        failed_states: list[tuple[Any, AllocationScoreFailure]] = []\n        for state in states:\n            ranked_data = (\n                {\n                    symbol: frame\n                    for symbol, frame in state.data_map.items()\n                    if symbol in held\n                }\n                if held\n                else state.data_map\n            )\n            try:\n                sleeve_scores.append(\n                    state.sleeve._allocation_scores(ranked_data, date)\n                )\n            except Exception as exc:\n                failure = AllocationScoreFailure(\n                    sleeve=str(getattr(state.sleeve, "sleeve_name", "unknown")),\n                    error_type=type(exc).__name__,\n                    message=str(exc),\n                )\n                failures.append(failure)\n                failed_states.append((state, failure))\n                sleeve_scores.append({})\n\n        view = AllocationScoreView(tuple(sleeve_scores), tuple(failures))\n        date_str = date.strftime("%Y-%m-%d")\n        for state, failure in failed_states:\n            risk_events = getattr(state.sleeve, "risk_events", None)\n            if not isinstance(risk_events, list):\n                continue\n            already_recorded = any(\n                item.get("event") == "allocation_score_degraded"\n                and item.get("date") == date_str\n                and item.get("sleeve_name") == failure.sleeve\n                for item in risk_events\n            )\n            if not already_recorded:\n                risk_events.append(\n                    {\n                        "date": date_str,\n                        "event": "allocation_score_degraded",\n                        "sleeve_name": failure.sleeve,\n                        "error_type": failure.error_type,\n                        "message": failure.message,\n                    }\n                )\n        return view\n''',
)
