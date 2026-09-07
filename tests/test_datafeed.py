"""Tests for the parts of the data path where a silent bug corrupts results.

These cover the logic that is easy to get subtly wrong and impossible to notice
downstream: candle aggregation, timestamp conventions, and symbol mapping. Two
real bugs found while building this would have been caught here -- a resample
that straddled the session open, and a timestamp convention that shifted every
bar by 5h30m.

No network access; everything is exercised on synthetic bars.
"""

import calendar
from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.datafeed import bars
from src.datafeed.dhan_feed import DhanFeed
from src.datafeed.symbols import is_index_token, to_yf_ticker
from src.datafeed.instruments import normalise
from src.utils.timezone import IST


def make_session(date="2026-09-04", minutes=375, start=(9, 15), base=100.0):
    """One session of 1-minute bars, rising 1 paisa a minute."""
    day = datetime.strptime(date, "%Y-%m-%d").replace(hour=start[0], minute=start[1])
    idx = pd.DatetimeIndex(
        [day + timedelta(minutes=i) for i in range(minutes)]
    ).tz_localize(IST)
    return pd.DataFrame(
        {
            "Open": [base + i * 0.01 for i in range(minutes)],
            "High": [base + i * 0.01 + 0.05 for i in range(minutes)],
            "Low": [base + i * 0.01 - 0.05 for i in range(minutes)],
            "Close": [base + i * 0.01 + 0.02 for i in range(minutes)],
            "Volume": [100 + i for i in range(minutes)],
        },
        index=idx,
    )


class TestResample:
    def test_three_minute_buckets_start_at_session_open(self):
        out = bars.resample(make_session(), "3min")
        stamps = [t.strftime("%H:%M") for t in out.index[:4]]
        assert stamps == ["09:15", "09:18", "09:21", "09:24"]

    def test_bucket_count_matches_session_length(self):
        # 375 one-minute bars -> 125 three-minute bars.
        assert len(bars.resample(make_session(), "3min")) == 125

    def test_ohlcv_aggregates_correctly(self):
        src = make_session()
        out = bars.resample(src, "3min")
        first_three = src.iloc[0:3]
        row = out.iloc[0]
        assert row["Open"] == pytest.approx(first_three["Open"].iloc[0])
        assert row["High"] == pytest.approx(first_three["High"].max())
        assert row["Low"] == pytest.approx(first_three["Low"].min())
        assert row["Close"] == pytest.approx(first_three["Close"].iloc[-1])
        assert row["Volume"] == first_three["Volume"].sum()

    def test_sessions_never_share_a_bucket(self):
        two_days = pd.concat([make_session("2026-09-03"), make_session("2026-09-04")])
        out = bars.resample(two_days, "3min")
        per_day = out.groupby(out.index.date).size()
        assert list(per_day) == [125, 125]

    def test_uneven_interval_anchors_to_the_open(self):
        # 09:15 is 555 minutes past midnight: divisible by 3, but not by 10.
        # Without per-session anchoring a 10-minute bucket straddles the open.
        out = bars.resample(make_session(), "10min")
        assert out.index[0].strftime("%H:%M") == "09:15"

    def test_empty_input_returns_empty(self):
        empty = make_session().iloc[0:0]
        assert bars.resample(empty, "3min").empty


class TestCandleConversion:
    def test_timestamps_carry_ist_offset(self):
        candles = bars.to_candles(make_session(minutes=3))
        assert candles[0]["timestamp"] == "2026-09-04T09:15:00+05:30"

    def test_datetime_can_round_trip_the_timestamp(self):
        candles = bars.to_candles(make_session(minutes=3))
        parsed = datetime.fromisoformat(candles[0]["timestamp"])
        assert (parsed.hour, parsed.minute) == (9, 15)

    def test_volume_is_an_int(self):
        candles = bars.to_candles(make_session(minutes=3))
        assert all(isinstance(c["volume"], int) for c in candles)


class TestDhanEpochConvention:
    """Dhan's docs say epoch seconds; the value shipped is ambiguous.

    Reading it wrong moves every bar by 5h30m and silently relocates the
    09:15-09:18 reference candle, so the feed infers the convention from the
    data. Both readings must land on the same IST wall-clock time.
    """

    @staticmethod
    def _session_naive():
        base = datetime(2026, 9, 4, 9, 15)
        return [base + timedelta(minutes=i) for i in range(375)]

    def test_ist_wallclock_epochs_resolve_to_0915(self):
        feed = DhanFeed(client_id="x", access_token="y")
        epochs = [calendar.timegm(t.timetuple()) for t in self._session_naive()]
        idx = feed._epoch_to_ist(epochs)
        assert idx[0].strftime("%Y-%m-%d %H:%M") == "2026-09-04 09:15"
        assert feed._epoch_is_ist_wallclock is True

    def test_true_utc_epochs_resolve_to_0915(self):
        feed = DhanFeed(client_id="x", access_token="y")
        epochs = [calendar.timegm(t.timetuple()) - 19800 for t in self._session_naive()]
        idx = feed._epoch_to_ist(epochs)
        assert idx[0].strftime("%Y-%m-%d %H:%M") == "2026-09-04 09:15"
        assert feed._epoch_is_ist_wallclock is False

    def test_both_conventions_agree(self):
        naive = self._session_naive()
        a = DhanFeed(client_id="x", access_token="y")._epoch_to_ist(
            [calendar.timegm(t.timetuple()) for t in naive]
        )
        b = DhanFeed(client_id="x", access_token="y")._epoch_to_ist(
            [calendar.timegm(t.timetuple()) - 19800 for t in naive]
        )
        assert list(a) == list(b)

    def test_convention_is_decided_once_and_reused(self):
        feed = DhanFeed(client_id="x", access_token="y")
        naive = self._session_naive()
        feed._epoch_to_ist([calendar.timegm(t.timetuple()) for t in naive])
        decided = feed._epoch_is_ist_wallclock
        # A later batch must not flip the verdict mid-session.
        feed._epoch_to_ist([calendar.timegm(t.timetuple()) for t in naive[:5]])
        assert feed._epoch_is_ist_wallclock is decided


class TestSecurityIds:
    def test_equity_token_passes_through_unmapped(self):
        # Dhan's securityId for NSE equities IS the exchange token.
        assert DhanFeed._resolve("SBIN-EQ", "3045") == ("3045", "NSE_EQ", "EQUITY")

    def test_index_is_mapped_to_its_own_id_and_segment(self):
        assert DhanFeed._resolve("NIFTY", "99926000") == ("13", "IDX_I", "INDEX")

    def test_missing_token_is_rejected(self):
        with pytest.raises(ValueError):
            DhanFeed._resolve("SBIN-EQ", "")


class TestSymbolMapping:
    @pytest.mark.parametrize("symbol,token,expected", [
        ("SBIN-EQ", "3045", "SBIN.NS"),
        ("RELIANCE", "", "RELIANCE.NS"),
        ("M&M-EQ", "2031", "M&M.NS"),
        ("NIFTY", "99926000", "^NSEI"),
    ])
    def test_yahoo_tickers(self, symbol, token, expected):
        assert to_yf_ticker(symbol, token) == expected

    def test_index_detection(self):
        assert is_index_token("99926000", "NIFTY") is True
        assert is_index_token("3045", "SBIN-EQ") is False

    def test_empty_symbol_is_rejected(self):
        with pytest.raises(ValueError):
            to_yf_ticker("", None)

    @pytest.mark.parametrize("raw,expected", [
        ("SBIN-EQ", "SBIN"), ("sbin", "SBIN"), ("  TCS-BE ", "TCS"), ("INFY", "INFY"),
    ])
    def test_series_suffixes_are_stripped(self, raw, expected):
        assert normalise(raw) == expected
