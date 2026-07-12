import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from main import LifetimeTokenStatsPlugin


def _summary():
    return {
        "lifetime_calls": 12,
        "input_other_tokens": 700,
        "input_cached_tokens": 100,
        "input_tokens": 800,
        "output_tokens": 200,
        "total_tokens": 1000,
        "first_record_at": "2026-01-01 00:00:00",
        "last_record_at": "2026-07-12 00:00:00",
        "provider_count": 1,
    }


def _provider_rows():
    return [
        {
            "provider_id": "gemini_chat",
            "provider_model": "gemma-4-31b-it",
            "calls": 12,
            "input_tokens": 800,
            "output_tokens": 200,
            "total_tokens": 1000,
            "first_record_at": "2026-01-01 00:00:00",
            "last_record_at": "2026-07-12 00:00:00",
        }
    ]


def _plugin_rows():
    return [
        {
            "source": "heartflow",
            "calls": 3,
            "input_tokens": 240,
            "input_cached_tokens": 40,
            "output_tokens": 60,
            "total_tokens": 300,
            "first_record_at": "2026-07-10 00:00:00",
            "last_record_at": "2026-07-12 00:00:00",
        },
        {
            "source": "legacy_unattributed",
            "calls": 2,
            "input_tokens": 100,
            "input_cached_tokens": 0,
            "output_tokens": 20,
            "total_tokens": 120,
            "first_record_at": "2026-01-01 00:00:00",
            "last_record_at": "2026-07-09 00:00:00",
        },
    ]


def test_plugin_source_html_contains_metrics_and_legacy_notice():
    plugin = LifetimeTokenStatsPlugin(context=object())

    html = plugin._build_unified_report_html(
        _summary(), _provider_rows(), _plugin_rows()
    )

    assert "Plugin API Calls" in html
    assert "Heartflow" in html
    assert "plugin:heartflow" in html
    assert "Calls <b>3</b>" in html
    assert "Cached <b>40</b>" in html
    assert "Share <b>30.00%</b>" in html
    assert "Legacy / Unattributed" in html
    assert "Includes earlier rows without a reliable source tag." in html


def test_unknown_plugin_source_is_escaped_and_humanized():
    plugin = LifetimeTokenStatsPlugin(context=object())
    rows = _plugin_rows()[:1]
    rows[0] = {**rows[0], "source": "custom_<script>"}

    html = plugin._build_unified_report_html(_summary(), _provider_rows(), rows)

    assert "<script>" not in html
    assert "Custom &lt;Script&gt;" in html
    assert "plugin:custom_&lt;script&gt;" in html


def test_empty_plugin_source_section_explains_when_data_will_appear():
    plugin = LifetimeTokenStatsPlugin(context=object())
    html = plugin._build_unified_report_html(_summary(), _provider_rows(), [])
    assert "No source-tagged plugin calls yet." in html
    assert "recorder v0.2.0" in html


def test_fetch_report_data_returns_provider_and_plugin_rows():
    plugin = LifetimeTokenStatsPlugin(context=object(), config={"provider_limit": 1})
    provider_rows = _provider_rows() + [
        {**_provider_rows()[0], "provider_id": "second", "total_tokens": 10}
    ]
    plugin._query_all_provider_stats = AsyncMock(return_value=provider_rows)
    plugin._query_plugin_source_stats = AsyncMock(return_value=_plugin_rows())

    summary, rows, plugin_rows = asyncio.run(plugin._fetch_report_data())

    assert summary["provider_count"] == 2
    assert len(rows) == 1
    assert plugin_rows == _plugin_rows()


class _Mappings:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return _Mappings(self._rows)


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.sql = None
        self.params = None

    async def execute(self, sql, params):
        self.sql = str(sql)
        self.params = params
        return _Result(self.rows)


class _SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return None


def test_plugin_source_query_uses_reserved_prefix_and_legacy_bucket():
    rows = [{"source": "heartflow", "calls": 1, "total_tokens": 10}]
    session = _Session(rows)
    db = SimpleNamespace(get_db=lambda: _SessionContext(session))
    plugin = LifetimeTokenStatsPlugin(context=SimpleNamespace(_db=db))

    result = asyncio.run(plugin._query_plugin_source_stats())

    assert result == rows
    assert "conversation_id LIKE" in session.sql
    assert "conversation_id IS NULL" in session.sql
    assert session.params == {
        "agent_type": "internal",
        "plugin_pattern": "plugin:%",
        "source_start": 8,
        "legacy_source": "legacy_unattributed",
    }

