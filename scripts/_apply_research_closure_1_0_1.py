"""One-shot source repair for the 1.0.1 research reproducibility closure.

This file is used only by the temporary validation workflow and is removed before
constructing the final main tree.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace(path: str, old: str, new: str, *, count: int = 1) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    actual = text.count(old)
    if actual != count:
        raise RuntimeError(f"{path}: expected {count} occurrence(s), found {actual}: {old[:80]!r}")
    target.write_text(text.replace(old, new, count), encoding="utf-8")


def replace_block(path: str, start_marker: str, end_marker: str, new_block: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    target.write_text(text[:start] + new_block + text[end:], encoding="utf-8")


# Research symbol identity: retain exact pool membership and add only listing-date
# facts needed to distinguish legitimate pre-listing N/A from missing data.
replace(
    "quantfusion/config/research_universes.py",
    "    }\n)\n\n_NAME_TO_SYMBOL",
    """    }
)

# Official first-trading dates are used only to identify windows that end before
# a late-listed symbol could possibly have market data. They never create or
# forward-fill pre-listing observations.
RESEARCH_FIRST_TRADING_DATES: Mapping[str, str] = MappingProxyType(
    {
        "688535": "2023-04-04",  # 华海诚科
        "688249": "2023-05-05",  # 晶合集成
        "688361": "2023-05-19",  # 中科飞测
        "688347": "2023-08-07",  # 华虹公司
        "920045": "2025-12-31",  # 蘅东光
        "688825": "2026-07-27",  # 长鑫科技
    }
)

_NAME_TO_SYMBOL""",
)

# Downloader: explicit pre-listing N/A is an attested manifest state, not a fake
# CSV and not a silent omission.
replace(
    "scripts/download_eastmoney_qfq.py",
    """    DEFAULT_RESEARCH_START_DATE,
    RESEARCH_SYMBOL_NAMES,
    UNIVERSE_POOLS,
""",
    """    DEFAULT_RESEARCH_START_DATE,
    RESEARCH_FIRST_TRADING_DATES,
    RESEARCH_SYMBOL_NAMES,
    UNIVERSE_POOLS,
""",
)
replace(
    "scripts/download_eastmoney_qfq.py",
    "\n\ndef _market_id(symbol: str) -> str:\n",
    """

def prelisting_not_applicable(symbol: str, end_date: str) -> bool:
    """Return whether the requested window ends before a known first trade."""
    first_trading = RESEARCH_FIRST_TRADING_DATES.get(symbol)
    if first_trading is None:
        return False
    end = pd.Timestamp(end_date)
    if pd.isna(end):
        raise ValueError("end_date must resolve to a valid timestamp")
    return end.normalize() < pd.Timestamp(first_trading)


def _market_id(symbol: str) -> str:
""",
)
replace(
    "scripts/download_eastmoney_qfq.py",
    """    entry: dict[str, object] = {
        "name": name,
        "provider": provider,
""",
    """    entry: dict[str, object] = {
        "name": name,
        "status": "observed",
        "provider": provider,
""",
)
replace(
    "scripts/download_eastmoney_qfq.py",
    """        for offset, symbol in enumerate(pending):
            try:
                frame, name, provider = _fetch_research_symbol(symbol, start, end)
""",
    """        for offset, symbol in enumerate(pending):
            if prelisting_not_applicable(symbol, end):
                first_trading = RESEARCH_FIRST_TRADING_DATES[symbol]
                symbol_manifest[symbol] = {
                    "name": RESEARCH_SYMBOL_NAMES.get(symbol, symbol),
                    "status": "not_applicable_pre_listing",
                    "provider": None,
                    "rows": 0,
                    "first_date": None,
                    "last_date": None,
                    "first_trading_date": first_trading,
                }
                print(
                    f"{symbol} {RESEARCH_SYMBOL_NAMES.get(symbol, symbol)}: "
                    f"not applicable before first trade {first_trading}"
                )
                continue
            try:
                frame, name, provider = _fetch_research_symbol(symbol, start, end)
""",
)
replace(
    "scripts/download_eastmoney_qfq.py",
    """                "downloaded_symbols": [],
                "missing_symbols": requested_symbols,
""",
    """                "downloaded_symbols": [],
                "not_applicable_symbols": [],
                "missing_symbols": requested_symbols,
""",
)
replace(
    "scripts/download_eastmoney_qfq.py",
    """        except Exception as exc:
            downloaded = list(symbol_manifest)
            manifest.update(
                {
                    "downloaded_symbols": downloaded,
                    "missing_symbols": [code for code in symbols if code not in symbol_manifest],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
""",
    """        except Exception as exc:
            downloaded = [
                code
                for code, entry in symbol_manifest.items()
                if isinstance(entry, dict) and entry.get("status") == "observed"
            ]
            not_applicable = [
                code
                for code, entry in symbol_manifest.items()
                if isinstance(entry, dict)
                and entry.get("status") == "not_applicable_pre_listing"
            ]
            manifest.update(
                {
                    "downloaded_symbols": downloaded,
                    "not_applicable_symbols": not_applicable,
                    "missing_symbols": [code for code in symbols if code not in symbol_manifest],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
""",
)
replace(
    "scripts/download_eastmoney_qfq.py",
    """        manifest.update(
            {
                "complete": True,
                "downloaded_symbols": list(symbol_manifest),
                "missing_symbols": [],
            }
        )
""",
    """        downloaded = [
            code
            for code, entry in symbol_manifest.items()
            if isinstance(entry, dict) and entry.get("status") == "observed"
        ]
        not_applicable = [
            code
            for code, entry in symbol_manifest.items()
            if isinstance(entry, dict)
            and entry.get("status") == "not_applicable_pre_listing"
        ]
        manifest.update(
            {
                "complete": True,
                "downloaded_symbols": downloaded,
                "not_applicable_symbols": not_applicable,
                "missing_symbols": [],
            }
        )
""",
)

# Comparison: require a complete manifest, accept only attested pre-listing N/A,
# filter those members only for the research replay, and opt in to the research
# index-provider fallback without changing production defaults.
replace(
    "scripts/compare_universes.py",
    """    DEFAULT_RESEARCH_START_DATE,
    UNIVERSE_POOLS,
""",
    """    DEFAULT_RESEARCH_START_DATE,
    RESEARCH_FIRST_TRADING_DATES,
    UNIVERSE_POOLS,
""",
)
replace_block(
    "scripts/compare_universes.py",
    "def _manifest_payload(path: Path)",
    "def _latest_observation_on_or_before",
    """def _manifest_payload(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(f"research market-data manifest is required: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid research market-data manifest: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid research market-data manifest: expected object")
    return payload


""",
)
replace_block(
    "scripts/compare_universes.py",
    "def validate_market_data_directory(",
    "def validate_regime_data_directory(",
    """def _manifest_entry_is_prelisting_na(
    code: str,
    entry: dict,
    *,
    end_date: str | None,
) -> bool:
    if entry.get("status") != "not_applicable_pre_listing":
        return False
    expected_first = RESEARCH_FIRST_TRADING_DATES.get(code)
    declared_first = entry.get("first_trading_date")
    if expected_first is None or declared_first != expected_first:
        raise ValueError(
            f"invalid pre-listing manifest identity for {code}: {declared_first!r}"
        )
    if end_date is None:
        raise ValueError("pre-listing manifest entries require an explicit end_date")
    end = pd.Timestamp(end_date)
    if pd.isna(end):
        raise ValueError("research market-data end_date must be valid")
    if end.normalize() >= pd.Timestamp(expected_first):
        raise ValueError(
            f"pre-listing N/A is invalid for {code}: first trade {expected_first} "
            f"is not after requested end {end.date()}"
        )
    return True


def active_symbols_for_pool(
    pool_name: str,
    data_dir: Path,
    *,
    end_date: str,
) -> dict[str, str]:
    """Return configured pool members that can exist inside this research window."""
    manifest = _manifest_payload(data_dir / "manifest.json")
    if manifest.get("complete") is not True:
        raise ValueError("incomplete research market-data manifest")
    entries = manifest.get("symbols")
    if not isinstance(entries, dict):
        raise ValueError("invalid research market-data manifest: symbols must be an object")
    active: dict[str, str] = {}
    for code, name in symbols_for_pool(pool_name).items():
        entry = entries.get(code)
        if not isinstance(entry, dict):
            raise ValueError(f"research market-data manifest omits required symbol: {code}")
        if _manifest_entry_is_prelisting_na(code, entry, end_date=end_date):
            continue
        active[code] = name
    if not active:
        raise ValueError(f"{pool_name} has no listed symbols in the requested window")
    return active


def validate_market_data_directory(
    data_dir: Path,
    pools: tuple[str, ...],
    *,
    end_date: str | None = None,
) -> None:
    """Require source-attested complete research inputs before any comparison."""
    if not data_dir.is_dir():
        raise ValueError(
            f"Research market-data directory does not exist: {data_dir}. "
            "Run scripts.download_eastmoney_qfq with the same pool selection first."
        )
    required = required_market_symbols(pools)
    manifest = _manifest_payload(data_dir / "manifest.json")
    if manifest.get("complete") is not True:
        raise ValueError("incomplete research market-data manifest")
    entries = manifest.get("symbols")
    if not isinstance(entries, dict):
        raise ValueError("invalid research market-data manifest: symbols must be an object")
    absent = [code for code in required if code not in entries]
    if absent:
        raise ValueError(
            "research market-data manifest omits required symbols: " + ", ".join(absent)
        )

    attested_na: list[str] = []
    observed: list[str] = []
    for code in required:
        entry = entries.get(code)
        if not isinstance(entry, dict):
            raise ValueError(f"invalid research market-data manifest entry for {code}")
        path = data_dir / f"{code}.csv"
        if _manifest_entry_is_prelisting_na(code, entry, end_date=end_date):
            if path.exists():
                raise ValueError(
                    f"pre-listing N/A symbol must not carry a market-data CSV: {code}"
                )
            attested_na.append(code)
            continue
        if not path.is_file():
            raise ValueError(f"missing required research market-data file: {code}")
        expected_hash = entry.get("sha256")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise ValueError(f"research market-data manifest lacks sha256 for {code}")
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            raise ValueError(f"research market-data sha256 mismatch for {code}")
        observed.append(code)

    declared_na = manifest.get("not_applicable_symbols", [])
    if not isinstance(declared_na, list) or set(declared_na) != set(attested_na):
        raise ValueError("research market-data manifest pre-listing identity is inconsistent")

    if end_date is not None:
        end = pd.Timestamp(end_date)
        if pd.isna(end):
            raise ValueError("research market-data end_date must be valid")
        stale: list[str] = []
        for code in observed:
            latest = _latest_observation_on_or_before(data_dir / f"{code}.csv", end)
            if (end - latest).days > MAX_EVIDENCE_STALENESS_DAYS:
                stale.append(f"{code}:{latest.date()}")
        if stale:
            raise ValueError(
                "research market-data coverage is stale at requested end: "
                + ", ".join(stale)
            )


""",
)
replace_block(
    "scripts/compare_universes.py",
    "def _run_pool(",
    "def build_argument_parser()",
    """def _run_pool(
    pool_name: str,
    *,
    start_date: str,
    end_date: str,
    data_dir: Path,
    regime_data_dir: Path,
    initial_capital: float,
    indicator_state: str,
    warmup_calendar_days: int,
) -> dict:
    configured_symbols = symbols_for_pool(pool_name)
    symbols = active_symbols_for_pool(pool_name, data_dir, end_date=end_date)
    engine = ProductionReplayEngine(initial_capital)
    with contextlib.redirect_stdout(io.StringIO()):
        result = engine.run(
            symbols,
            start_date,
            end_date,
            data_dir=str(data_dir),
            regime_data_dir=str(regime_data_dir),
            indicator_state=indicator_state,
            warmup_calendar_days=warmup_calendar_days,
        )
        market_frames = {
            code: DataFetcher.load_stock_data(
                code,
                start_date,
                end_date,
                data_dir=str(data_dir),
            )
            for code in symbols
        }

    expected = sorted(symbols)
    if result.get("requested_symbols") != expected:
        raise ValueError(f"{pool_name} replay changed the requested universe identity")
    if result.get("selected_symbols") != expected:
        raise ValueError(f"{pool_name} replay silently changed the selected universe")
    if result.get("unavailable_symbols") not in ([], ()):
        raise ValueError(f"{pool_name} replay reported unavailable symbols")

    row = summarize_universe_result(
        pool_name,
        configured_symbols,
        start_date,
        end_date,
        result,
        market_frames=market_frames,
        active_symbols=symbols,
    )
    requested_start = pd.Timestamp(start_date)
    requested_end = pd.Timestamp(end_date)
    observed_start = pd.Timestamp(row["observed_start_date"])
    observed_end = pd.Timestamp(row["observed_end_date"])
    if (observed_start - requested_start).days > MAX_EVIDENCE_STALENESS_DAYS:
        raise ValueError(
            f"{pool_name} observed replay begins too late: "
            f"{observed_start.date()} vs requested {requested_start.date()}"
        )
    if (requested_end - observed_end).days > MAX_EVIDENCE_STALENESS_DAYS:
        raise ValueError(
            f"{pool_name} observed replay ends too early: "
            f"{observed_end.date()} vs requested {requested_end.date()}"
        )
    return row


""",
)
replace(
    "scripts/compare_universes.py",
    """    market_data_contracts.refresh_regime_indices(
        regime_data_dir,
        end_date=end_date,
        strict=True,
    )
""",
    """    market_data_contracts.refresh_regime_indices(
        regime_data_dir,
        end_date=end_date,
        strict=True,
        allow_provider_fallback=True,
    )
""",
)

# Reporting: distinguish portfolio calendar from per-member coverage and retain
# configured pool identity when some members are legitimately pre-listing N/A.
replace(
    "quantfusion/application/universe_comparison.py",
    "import pandas as pd\n",
    """import pandas as pd

from quantfusion.config.research_universes import RESEARCH_FIRST_TRADING_DATES
""",
)
replace_block(
    "quantfusion/application/universe_comparison.py",
    "def summarize_universe_result(",
    "def write_universe_comparison(",
    """def _member_observation_summary(
    symbols: Mapping[str, str],
    *,
    start_date: str,
    end_date: str,
    market_frames: Mapping[str, pd.DataFrame],
    active_symbols: Mapping[str, str],
) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    active_codes = set(active_symbols)
    observations: dict[str, dict[str, Any]] = {}
    late_listing: list[str] = []
    not_applicable: list[str] = []
    for code, name in symbols.items():
        first_trading = RESEARCH_FIRST_TRADING_DATES.get(code)
        if code not in active_codes:
            if first_trading is None or end.normalize() >= pd.Timestamp(first_trading):
                raise ValueError(f"inactive research member lacks valid pre-listing identity: {code}")
            observations[code] = {
                "name": name,
                "status": "not_applicable_pre_listing",
                "first_trading_date": first_trading,
                "first_observation": None,
                "last_observation": None,
            }
            not_applicable.append(code)
            continue
        frame = market_frames.get(code)
        if frame is None or frame.empty:
            raise ValueError(f"missing observation frame for active research member {code}")
        dates = pd.DatetimeIndex(pd.to_datetime(frame.index, errors="raise"))
        current = dates[(dates >= start) & (dates <= end)]
        if current.empty:
            raise ValueError(f"active research member has no observation in window: {code}")
        is_late = first_trading is not None and pd.Timestamp(first_trading) > start.normalize()
        status = "late_listing" if is_late else "observed"
        if is_late:
            late_listing.append(code)
        observations[code] = {
            "name": name,
            "status": status,
            "first_trading_date": first_trading,
            "first_observation": pd.Timestamp(current[0]).strftime("%Y-%m-%d"),
            "last_observation": pd.Timestamp(current[-1]).strftime("%Y-%m-%d"),
        }
    return observations, late_listing, not_applicable


def summarize_universe_result(
    pool_name: str,
    symbols: Mapping[str, str],
    start_date: str,
    end_date: str,
    result: Mapping[str, Any],
    *,
    market_frames: Mapping[str, pd.DataFrame] | None = None,
    active_symbols: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Derive comparable research metrics without changing engine decisions."""
    equity = _equity_frame(result)
    observed_dates = pd.DatetimeIndex(pd.to_datetime(equity.index, errors="raise"))
    if observed_dates.hasnans or not observed_dates.is_monotonic_increasing:
        raise ValueError("equity_curve index must be a monotonic finite date sequence")
    observed_start = observed_dates[0].strftime("%Y-%m-%d")
    observed_end = observed_dates[-1].strftime("%Y-%m-%d")

    assets = equity["assets"].astype(float)
    cash = equity["cash"].astype(float)
    position_value = equity["position_value"].astype(float)
    average_assets = float(assets.mean())
    if average_assets <= 0:
        raise ValueError("equity_curve average assets must be positive")

    trades = result.get("trades")
    if not isinstance(trades, list):
        raise ValueError("comparison result requires a trades list")
    total_trades = int(result["total_trades"])
    if total_trades != len(trades):
        raise ValueError("comparison result total_trades does not match trades list")
    gross_traded_value = sum(abs(_trade_gross_value(trade)) for trade in trades)

    positive_assets = assets > 0
    if not bool(positive_assets.all()):
        raise ValueError("equity_curve assets must stay positive for cash-ratio reporting")
    average_cash_ratio = float((cash / assets).mean())
    all_cash_day_ratio = float((position_value.abs() <= 1e-9).mean())

    max_concurrent = int(result.get("max_concurrent_symbols", 0))
    if max_concurrent < 0:
        raise ValueError("max_concurrent_symbols must be non-negative")
    active = dict(active_symbols or symbols)
    if market_frames is None:
        hhi_mean: float | None = None
        hhi_max: float | None = None
        member_observations: dict[str, dict[str, Any]] = {}
        late_listing_members: list[str] = []
        not_applicable_prelisting_symbols = [code for code in symbols if code not in active]
    else:
        hhi_mean, hhi_max = _holding_concentration_hhi(
            trades,
            observed_dates,
            market_frames,
        )
        (
            member_observations,
            late_listing_members,
            not_applicable_prelisting_symbols,
        ) = _member_observation_summary(
            symbols,
            start_date=start_date,
            end_date=end_date,
            market_frames=market_frames,
            active_symbols=active,
        )

    risk_events = result.get("risk_events")
    if not isinstance(risk_events, list):
        raise ValueError("comparison result requires a risk_events list")
    if any(not isinstance(item, Mapping) for item in risk_events):
        raise ValueError("comparison result risk_events must contain mapping records")
    event_types = Counter(str(item.get("event", "unknown")) for item in risk_events)

    return {
        "pool": pool_name,
        "symbol_count": len(symbols),
        "active_symbol_count": len(active),
        "symbols": list(symbols),
        "active_symbols": list(active),
        "symbol_names": list(symbols.values()),
        "start_date": start_date,
        "end_date": end_date,
        "observed_start_date": observed_start,
        "observed_end_date": observed_end,
        "member_observations": member_observations,
        "late_listing_members": late_listing_members,
        "not_applicable_prelisting_symbols": not_applicable_prelisting_symbols,
        "total_return": float(result["total_return"]),
        "annual_return": float(result["annual_return"]),
        "max_drawdown": float(result["max_drawdown"]),
        "total_trades": total_trades,
        "turnover_ratio": gross_traded_value / average_assets,
        "all_cash_day_ratio": all_cash_day_ratio,
        "average_cash_ratio": average_cash_ratio,
        "holding_concentration_hhi_mean": hhi_mean,
        "holding_concentration_hhi_max": hhi_max,
        "max_concurrent_symbols": max_concurrent,
        "risk_event_count": len(risk_events),
        "risk_event_types": dict(sorted(event_types.items())),
    }


""",
)
replace_block(
    "quantfusion/application/universe_comparison.py",
    "def write_universe_comparison(",
    "",
    """def _member_coverage_text(record: Mapping[str, Any]) -> str:
    observations = record.get("member_observations", {})
    if not isinstance(observations, Mapping) or not observations:
        return "N/A"
    parts: list[str] = []
    for code, details in observations.items():
        if not isinstance(details, Mapping):
            continue
        status = str(details.get("status", "unknown"))
        name = str(details.get("name", ""))
        first = details.get("first_observation")
        last = details.get("last_observation")
        if first and last:
            parts.append(f"{code} {name}: {status} {first}→{last}")
        else:
            first_trading = details.get("first_trading_date")
            parts.append(f"{code} {name}: {status} first_trade={first_trading}")
    return "; ".join(parts) or "N/A"


def write_universe_comparison(
    rows: Iterable[Mapping[str, Any]], output_dir: str | Path
) -> dict[str, Path]:
    """Write JSON, CSV and human-readable Markdown research reports."""
    records = [dict(row) for row in rows]
    if not records:
        raise ValueError("universe comparison requires at least one result")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    for record in records:
        record["members"] = _members_text(record)
        record.setdefault("active_symbol_count", record["symbol_count"])
        record.setdefault("active_symbols", list(record.get("symbols", [])))
        record.setdefault("member_observations", {})
        record.setdefault("late_listing_members", [])
        record.setdefault("not_applicable_prelisting_symbols", [])

    json_path = output / "comparison.json"
    json_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    scalar_fields: tuple[str, ...] = (
        "pool",
        "symbol_count",
        "active_symbol_count",
        "members",
        "start_date",
        "end_date",
        "observed_start_date",
        "observed_end_date",
        "late_listing_members",
        "not_applicable_prelisting_symbols",
        "member_observations",
        "total_return",
        "annual_return",
        "max_drawdown",
        "total_trades",
        "turnover_ratio",
        "all_cash_day_ratio",
        "average_cash_ratio",
        "holding_concentration_hhi_mean",
        "holding_concentration_hhi_max",
        "max_concurrent_symbols",
        "risk_event_count",
        "risk_event_types",
    )
    csv_path = output / "comparison.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(scalar_fields)
        for record in records:
            csv_record = {field: record[field] for field in scalar_fields}
            for field in (
                "late_listing_members",
                "not_applicable_prelisting_symbols",
                "member_observations",
                "risk_event_types",
            ):
                csv_record[field] = json.dumps(
                    record[field],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            writer.writerow([csv_record[field] for field in scalar_fields])

    markdown_path = output / "comparison.md"
    headers = (
        "Pool",
        "Configured",
        "Active",
        "Members",
        "Requested window",
        "Portfolio observed window",
        "Member coverage",
        "Return",
        "Max DD",
        "Annual",
        "Trades",
        "Turnover",
        "Cash days",
        "Avg cash",
        "Avg HHI",
        "Peak HHI",
        "Risk events",
        "Risk event types",
    )
    lines = [
        "# Universe comparison",
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for record in records:
        hhi_mean = record["holding_concentration_hhi_mean"]
        hhi_max = record["holding_concentration_hhi_max"]
        risk_types = _risk_event_types_text(record["risk_event_types"])
        lines.append(
            "| "
            + " | ".join(
                (
                    str(record["pool"]),
                    str(record["symbol_count"]),
                    str(record["active_symbol_count"]),
                    str(record["members"]),
                    f"{record['start_date']} → {record['end_date']}",
                    f"{record['observed_start_date']} → {record['observed_end_date']}",
                    _member_coverage_text(record),
                    f"{float(record['total_return']):.2%}",
                    f"{float(record['max_drawdown']):.2%}",
                    f"{float(record['annual_return']):.2%}",
                    str(record["total_trades"]),
                    f"{float(record['turnover_ratio']):.2f}",
                    f"{float(record['all_cash_day_ratio']):.2%}",
                    f"{float(record['average_cash_ratio']):.2%}",
                    "N/A" if hhi_mean is None else f"{float(hhi_mean):.2%}",
                    "N/A" if hhi_max is None else f"{float(hhi_max):.2%}",
                    str(record["risk_event_count"]),
                    risk_types or "none",
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "The portfolio observed window is the replay calendar, not a claim that every configured member had data for the full period. Member coverage records each symbol's actual first/last observation and explicit pre-listing N/A state.",
            "HHI is reconstructed from executed fills and the latest closing price known by each portfolio date; cash-only days are reported separately and excluded from the HHI average.",
            "Risk-event counts are descriptive replay evidence and do not change production decisions.",
            "",
        ]
    )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return {"json": json_path, "csv": csv_path, "markdown": markdown_path}
""",
)

# Version identity: 1.0.0 remains immutable history; this maintenance closure is
# the patch release requested by the user.
replace("quantfusion/__init__.py", '__version__ = "1.0.0"', '__version__ = "1.0.1"')
replace(
    "tests/contract/test_repository_hygiene.py",
    'self.assertEqual(getattr(quantfusion, "__version__", None), "1.0.0")',
    'self.assertEqual(getattr(quantfusion, "__version__", None), "1.0.1")',
)
replace(
    "tests/contract/test_repository_hygiene.py",
    'self.assertIn("1.0.0", (ROOT / relative).read_text(encoding="utf-8"))',
    'self.assertIn("1.0.1", (ROOT / relative).read_text(encoding="utf-8"))',
)

# Current user-facing docs: describe the actual 1.0.1 behavior and keep 1.0.0 as
# prior immutable release history rather than moving its tag.
replace("README.md", "当前版本：`1.0.0`。", "当前版本：`1.0.1`。")
replace(
    "README.md",
    "完整 manifest 为每个研究 CSV 保存 SHA-256；比较前会重新核验文件集合和哈希。",
    "Pool 比较强制要求 `complete=true` 的 manifest，并逐文件复核 SHA-256；缺失 manifest、身份不一致或文件漂移都会失败关闭。",
)
replace(
    "README.md",
    "`compare_universes` 使用 `ProductionReplayEngine` 的连续账户路由，不复制信号、费用、T+1、仓位或风险实现。",
    "`compare_universes` 使用 `ProductionReplayEngine` 的连续账户路由，不复制信号、费用、T+1、仓位或风险实现。研究比较刷新两只固定指数时显式允许 Eastmoney→Tencent→Sina 的独立提供方回退；生产日扫仍保留原默认路径，不因研究入口改变。",
)
replace(
    "README.md",
    "较晚上市标的在上市前没有观测，不补造历史；只有真实行情出现后才参与对应日期的策略计算。请求窗口与实际观察窗口分别记录，不能把从 2024 才有数据的结果标成完整 2023 回放。",
    "较晚上市标的在上市前没有观测，不补造历史；如果整个请求窗口都早于已核验的首个交易日，manifest 会明确记录 `not_applicable_pre_listing`，该标的不进入该窗口的研究回放。上市后才有数据的成员会继续参与其可观察日期。报告把组合回放日历与逐成员首末观测日期分开记录，不能把组合覆盖窗口误读为所有成员全程可观察。",
)
replace("docs/RELEASE.md", "# Quant Fusion 1.0.0 发布说明", "# Quant Fusion 1.0.1 发布说明")
replace("docs/RELEASE.md", "产品版本为 `1.0.0`", "产品版本为 `1.0.1`")
replace(
    "docs/RELEASE.md",
    "## 量化特性\n",
    """## 1.0.1 维护修订

`1.0.1` 是研究可复现性和发布身份修订，不调整生产 17 股核心策略、风险阈值、费用、冻结黄金指标或正式经济证据。它要求 Pool 比较使用完整 manifest/哈希身份，允许研究入口对固定指数使用独立提供方回退，明确记录上市前不适用与逐成员真实观测区间，并移除已结束的 no-waiver/actionable-boundary 临时研究 workflow。旧 `1.0.0` 标签保持原提交不移动。

## 量化特性
""",
)
replace(
    "data/README.md",
    "Pool 历史比较复用同一两只指数，但不能把指数缺失导致的 `CASH` 当成某股票池的主动防御效果。",
    "Pool 历史比较复用同一两只指数，但不能把指数缺失导致的 `CASH` 当成某股票池的主动防御效果。研究比较入口允许 Eastmoney 失败后改用 Tencent、Sina 获取同一指数身份；该回退只由研究入口显式启用，不改变生产日扫默认行为。",
)
replace(
    "data/README.md",
    "较晚上市标的保留真实首日，不补造上市前 K 线。",
    "较晚上市标的保留真实首日，不补造上市前 K 线；请求结束日早于已核验首个交易日时，manifest 明确记为 `not_applicable_pre_listing`，不创建占位 CSV。",
)
replace(
    "data/README.md",
    "`scripts.compare_universes` 拒绝 `complete=false`、缺少所选 Pool／固定参考／风险篮文件、完整 manifest 的哈希漂移以及明显陈旧的截止日覆盖。",
    "`scripts.compare_universes` 强制要求 manifest 存在且 `complete=true`，拒绝缺少所选 Pool／固定参考／风险篮身份、逐文件哈希漂移以及明显陈旧的截止日覆盖；只有经首个交易日事实证明的上市前 N/A 可以没有 CSV。",
)
replace(
    "docs/VALIDATION.md",
    "研究股票池、2023 年起窗口或 `scripts.compare_universes` 产生的结果均为非 canonical 研究证据，不能覆盖下方 958 场景正式计划、黄金值、固定 seed、成本口径或发布回执。",
    "研究股票池、2023 年起窗口或 `scripts.compare_universes` 产生的结果均为非 canonical 研究证据，不能覆盖下方 958 场景正式计划、黄金值、固定 seed、成本口径或发布回执。Pool 比较强制绑定完整 manifest 与逐文件 SHA-256；研究固定指数允许显式独立提供方回退。整个请求窗口早于已核验首个交易日的成员只能记为上市前 N/A，不能补造历史；报告另列逐成员实际首末观测日期。",
)

print("research closure source repair applied")
