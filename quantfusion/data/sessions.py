"""Finite, source-labelled exchange calendar and daily evidence coverage.

The bundled calendar is reviewed input, not a permanently correct holiday
algorithm. A new year's exchange notices must be reviewed before extending it.
Market-data tails never establish which sessions ought to exist.
"""
from __future__ import annotations

import hashlib
import io
import json
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pandas as pd

from quantfusion.config.paths import DATA_ROOT
from quantfusion.config.regime import REGIME_INDEX_FILES
from quantfusion.data.contracts import _normalize_index_frame

DEFAULT_CALENDAR_FILE = DATA_ROOT / 'trading_calendar.json'
MARKET_TIMEZONE = ZoneInfo('Asia/Shanghai')


def market_now() -> datetime:
    return datetime.now(MARKET_TIMEZONE)


def _day(value: str) -> date:
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError('date must use YYYY-MM-DD')
    return result


@dataclass(frozen=True)
class TradingCalendar:
    sessions: tuple[str, ...]
    coverage_start: str
    coverage_end: str
    sha256: str
    sources: tuple[str, ...]


def load_calendar(path: str | Path = DEFAULT_CALENDAR_FILE) -> TradingCalendar:
    """Validate explicit annual source coverage; never extrapolate weekdays."""
    try:
        raw = Path(path).read_bytes()
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise ValueError('calendar must be an object')
        if obj['market'] != 'SSE_SZSE_A_SHARES' or obj['timezone'] != 'Asia/Shanghai':
            raise ValueError('wrong calendar market/timezone')
        start, end = _day(obj['coverage_start']), _day(obj['coverage_end'])
        if start > end or (start.month, start.day) != (1, 1) or (end.month, end.day) != (12, 31):
            raise ValueError('calendar must explicitly cover complete sourced years')
        years = obj['years']
        if not isinstance(years, dict):
            raise ValueError('calendar years must be an object')
        if set(years) != {str(y) for y in range(start.year, end.year + 1)}:
            raise ValueError('calendar source coverage has gaps')
        closed: set[date] = set()
        sources: list[str] = []
        for year, entry in years.items():
            if not isinstance(entry, dict):
                raise ValueError('annual calendar must be an object')
            urls = entry['sources']
            if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
                raise ValueError('calendar source URLs must be strings')
            hosts = {urlparse(u).hostname for u in urls}
            if not {'www.sse.com.cn'} <= hosts or not hosts.intersection({'www.szse.cn', 'investor.szse.cn'}):
                raise ValueError('both exchange notice sources are required for each year')
            if any(urlparse(u).scheme != 'https' for u in urls) or not entry['closed']:
                raise ValueError('missing verified annual closures/source')
            sources.extend(urls)
            for first, last in entry['closed']:
                begin, finish = _day(f'{year}-{first}'), _day(f'{year}-{last}')
                if begin > finish or begin < start or finish > end:
                    raise ValueError('invalid closure interval')
                closed.update(begin + timedelta(days=i) for i in range((finish-begin).days+1))
        days = (start + timedelta(days=i) for i in range((end-start).days+1))
        sessions = tuple(day.isoformat() for day in days if day.weekday() < 5 and day not in closed)
        return TradingCalendar(sessions, start.isoformat(), end.isoformat(),
                               hashlib.sha256(raw).hexdigest(), tuple(sources))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ValueError(f'CALENDAR_UNAVAILABLE: {exc}') from exc


def resolve_scan_dates(
    as_of: str, *, calendar_file: str | Path = DEFAULT_CALENDAR_FILE,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resolve requested/required/next dates in Shanghai, including after-hours.

    15:30 is a conservative complete-bar boundary for a pool containing STAR
    and ChiNext after-hours fixed-price trading. It does not certify delivery.
    """
    day = _day(as_of)
    clock = now if now is not None else market_now()
    if clock.tzinfo is None:
        raise ValueError('market clock must be timezone-aware')
    clock = clock.astimezone(MARKET_TIMEZONE)
    if day > clock.date():
        raise ValueError(f'FUTURE_REQUEST: {as_of}')
    calendar = load_calendar(calendar_file)
    if not calendar.coverage_start <= as_of <= calendar.coverage_end:
        raise ValueError(f'CALENDAR_OUT_OF_RANGE: {as_of}; {calendar.coverage_start}..{calendar.coverage_end}')
    index = bisect_right(calendar.sessions, as_of)
    if not index or index == len(calendar.sessions):
        raise ValueError('CALENDAR_OUT_OF_RANGE: previous or next session is unknown')
    required = calendar.sessions[index-1]
    if day == clock.date() and required == as_of and clock.time() < time(15, 30):
        raise ValueError(f'CLOSE_NOT_READY: 当日收盘数据未就绪 ({as_of}, Asia/Shanghai 15:30)')
    return {'requested_as_of': as_of, 'required_evidence_date': required,
            'next_trading_date': calendar.sessions[index], 'market_timezone': 'Asia/Shanghai',
            'calendar_sha256': calendar.sha256, 'calendar_coverage_start': calendar.coverage_start,
            'calendar_coverage_end': calendar.coverage_end, 'calendar_sources': list(calendar.sources)}


def require_frame_coverage(frame: pd.DataFrame, dates: dict[str, Any], code: str) -> str:
    """Require the actual target bar; unknown suspension is not a holiday."""
    if frame.empty:
        raise ValueError(f'MARKET_DATA_MISSING:{code}')
    observed = pd.DatetimeIndex(frame.index)
    if observed.hasnans or not observed.is_monotonic_increasing or observed.has_duplicates:
        raise ValueError(f'INVALID_EVIDENCE_DATE:{code}')
    if observed.tz is not None or not (observed == observed.normalize()).all():
        raise ValueError(f'INVALID_EVIDENCE_DATE:{code}: daily dates must be timezone-naive')
    if observed[-1] > pd.Timestamp(dates['requested_as_of']):
        raise ValueError(f'FUTURE_EVIDENCE:{code}')
    actual = observed[-1].date().isoformat()
    if actual != dates['required_evidence_date']:
        raise ValueError(f"TRADING_DAY_COVERAGE:{code}: expected={dates['required_evidence_date']}; observed={actual}; missing bar or unverified suspension")
    return actual


def index_coverage(data_dir: str | Path, dates: dict[str, Any]) -> dict[str, Any]:
    """Validate the same finite index inputs and return byte identities."""
    evidence = {}
    for code in REGIME_INDEX_FILES.values():
        try:
            raw = (Path(data_dir) / f'{code}.csv').read_bytes()
            frame = _normalize_index_frame(pd.read_csv(io.BytesIO(raw)), end_date=dates['requested_as_of'])
            frame.index = pd.DatetimeIndex(frame['date'])
            observed = require_frame_coverage(frame, dates, f'INDEX:{code}')
            evidence[code] = {'evidence_date': observed, 'sha256': hashlib.sha256(raw).hexdigest()}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError(f'INDEX_EVIDENCE_UNAVAILABLE:{code}: {exc}') from exc
    return evidence
