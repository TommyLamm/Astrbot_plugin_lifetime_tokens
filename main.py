from __future__ import annotations

import colorsys
from datetime import datetime
from html import escape
from typing import Any

from sqlalchemy import text

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


class LifetimeTokenStatsPlugin(Star):
    """Read AstrBot provider_stats and report lifetime token usage.

    Commands:
      /token_text
      /token

    Optional plugin config:
      provider_limit: how many top providers to display (default 10)
    """

    AGENT_TYPE = "internal"
    DEFAULT_PROVIDER_LIMIT = 10
    MAX_PROVIDER_LIMIT = 50
    VIEWPORT_WIDTH = 1200
    VIEWPORT_HEIGHT = 680
    PLUGIN_SOURCE_PREFIX = "plugin:"
    LEGACY_SOURCE = "legacy_unattributed"
    PLUGIN_SOURCE_LABELS = {
        "heartflow": "Heartflow",
        "gemini_search": "Gemini Search",
        "livingmemory": "LivingMemory",
        "daily_analysis": "Daily Analysis",
    }

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.plugin_config = config or {}
        self.provider_limit = self._resolve_provider_limit(self.plugin_config)

    async def initialize(self) -> None:
        logger.info(
            "Lifetime Token Stats plugin loaded. provider_limit=%s",
            self.provider_limit,
        )

    @filter.command("token_text")
    async def lifetime_report(self, event: AstrMessageEvent):
        """Show a unified lifetime token report in text."""
        try:
            summary, rows, _plugin_rows = await self._fetch_report_data()
            yield event.plain_result(self._format_unified_report(summary, rows))
        except Exception as exc:
            logger.exception("Failed to query unified lifetime token report.")
            yield event.plain_result(
                "查詢 unified lifetime token report 失敗，詳細錯誤已記錄到日誌。\n"
                f"錯誤類型：{type(exc).__name__}"
            )

    @filter.command("token")
    async def lifetime_report_img(self, event: AstrMessageEvent):
        """Render a unified lifetime token report as a T2I image."""
        try:
            summary, rows, plugin_rows = await self._fetch_report_data()
            html = self._build_unified_report_html(summary, rows, plugin_rows)
            fallback_text = self._format_unified_report(summary, rows)
            image = await self._render_stats_image(html, fallback_text)
            yield event.image_result(image)
        except Exception as exc:
            logger.exception("Failed to render unified lifetime token report image.")
            yield event.plain_result(
                "生成 unified lifetime token report 圖片失敗，詳細錯誤已記錄到日誌。\n"
                f"錯誤類型：{type(exc).__name__}"
            )

    async def terminate(self) -> None:
        logger.info("Lifetime Token Stats plugin unloaded.")

    async def _fetch_report_data(
        self,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        """Fetch provider totals and plugin-source breakdown rows.

        優化重點 / Optimization: 舊版分別呼叫 `_query_lifetime_summary`（全表
        COUNT/SUM）與 `_query_provider_lifetime`（全表 GROUP BY + LIMIT），
        兩者都各自對 `provider_stats` 做一次完整掃描，且是序列 await。
        Provider 的種類數通常遠小於原始記錄數，所以改成只用一次不加
        LIMIT 的 GROUP BY 查詢撈出「所有」provider 分組，lifetime summary
        再用 Python 對這份結果做 sum/min/max 彙總即可，不需要第二次掃表。

        Returns (summary, rows, plugin_rows) where `rows` is already truncated to
        `self.provider_limit` for display, while `summary` reflects the
        totals across *all* providers (not just the shown ones).
        """
        all_rows = await self._query_all_provider_stats()
        plugin_rows = await self._query_plugin_source_stats()
        summary = self._summarize_provider_rows(all_rows)
        rows = all_rows[: self.provider_limit]
        return summary, rows, plugin_rows

    async def _query_all_provider_stats(self) -> list[dict[str, Any]]:
        """Group by provider identity across the whole table, no LIMIT.

        This is now the *only* query the plugin runs per report. It used
        to be paired with a separate whole-table aggregate query; see
        `_fetch_report_data` for why that was removed.
        """
        db = getattr(self.context, "_db", None)
        if db is None or not hasattr(db, "get_db"):
            raise RuntimeError("Cannot access AstrBot database from plugin context.")

        sql = text(
            """
            SELECT
                COALESCE(provider_id, '<unknown>') AS provider_id,
                COALESCE(provider_model, '<unknown>') AS provider_model,
                COUNT(*) AS calls,
                COALESCE(SUM(token_input_other), 0) AS input_other_tokens,
                COALESCE(SUM(token_input_cached), 0) AS input_cached_tokens,
                COALESCE(SUM(token_input_other + token_input_cached), 0) AS input_tokens,
                COALESCE(SUM(token_output), 0) AS output_tokens,
                COALESCE(SUM(token_input_other + token_input_cached + token_output), 0)
                    AS total_tokens,
                MIN(created_at) AS first_record_at,
                MAX(created_at) AS last_record_at
            FROM provider_stats
            WHERE agent_type = :agent_type
            GROUP BY provider_id, provider_model
            ORDER BY total_tokens DESC
            """
        )

        async with db.get_db() as session:
            result = await session.execute(sql, {"agent_type": self.AGENT_TYPE})
            rows = result.mappings().all()

        return [dict(row) for row in rows]

    async def _query_plugin_source_stats(self) -> list[dict[str, Any]]:
        """Group source-tagged and historical untagged provider stats."""
        db = getattr(self.context, "_db", None)
        if db is None or not hasattr(db, "get_db"):
            raise RuntimeError("Cannot access AstrBot database from plugin context.")

        sql = text(
            """
            SELECT
                CASE
                    WHEN conversation_id LIKE :plugin_pattern
                        THEN SUBSTR(conversation_id, :source_start)
                    ELSE :legacy_source
                END AS source,
                COUNT(*) AS calls,
                COALESCE(SUM(token_input_other), 0) AS input_other_tokens,
                COALESCE(SUM(token_input_cached), 0) AS input_cached_tokens,
                COALESCE(SUM(token_input_other + token_input_cached), 0) AS input_tokens,
                COALESCE(SUM(token_output), 0) AS output_tokens,
                COALESCE(SUM(token_input_other + token_input_cached + token_output), 0)
                    AS total_tokens,
                MIN(created_at) AS first_record_at,
                MAX(created_at) AS last_record_at
            FROM provider_stats
            WHERE agent_type = :agent_type
              AND (conversation_id LIKE :plugin_pattern OR conversation_id IS NULL)
            GROUP BY source
            ORDER BY total_tokens DESC
            """
        )
        params = {
            "agent_type": self.AGENT_TYPE,
            "plugin_pattern": f"{self.PLUGIN_SOURCE_PREFIX}%",
            "source_start": len(self.PLUGIN_SOURCE_PREFIX) + 1,
            "legacy_source": self.LEGACY_SOURCE,
        }

        async with db.get_db() as session:
            result = await session.execute(sql, params)
            rows = result.mappings().all()

        return [dict(row) for row in rows]

    def _summarize_provider_rows(self, all_rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Roll the full per-provider result set up into lifetime totals.

        Equivalent to the old dedicated summary query's COUNT/SUM/MIN/MAX,
        but computed in Python from data we already fetched, instead of
        re-scanning `provider_stats` a second time.
        """
        if not all_rows:
            return {
                "lifetime_calls": 0,
                "input_other_tokens": 0,
                "input_cached_tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "first_record_at": None,
                "last_record_at": None,
                "provider_count": 0,
            }

        # Parsed (not raw) datetimes are compared here, via the same
        # `_parse_datetime` used for display, so mixed timestamp formats
        # across rows still sort correctly (a plain string min/max would
        # not be guaranteed to match chronological order).
        first_dts = [
            dt
            for dt in (self._parse_datetime(row.get("first_record_at")) for row in all_rows)
            if dt is not None
        ]
        last_dts = [
            dt
            for dt in (self._parse_datetime(row.get("last_record_at")) for row in all_rows)
            if dt is not None
        ]

        return {
            "lifetime_calls": sum(int(row.get("calls") or 0) for row in all_rows),
            "input_other_tokens": sum(int(row.get("input_other_tokens") or 0) for row in all_rows),
            "input_cached_tokens": sum(int(row.get("input_cached_tokens") or 0) for row in all_rows),
            "input_tokens": sum(int(row.get("input_tokens") or 0) for row in all_rows),
            "output_tokens": sum(int(row.get("output_tokens") or 0) for row in all_rows),
            "total_tokens": sum(int(row.get("total_tokens") or 0) for row in all_rows),
            "first_record_at": min(first_dts) if first_dts else None,
            "last_record_at": max(last_dts) if last_dts else None,
            "provider_count": len(all_rows),
        }

    async def _render_stats_image(self, html: str, fallback_text: str) -> str:
        try:
            return await self.html_render(
                html,
                {},
                return_url=True,
                options={
                    "full_page": True,
                    "type": "png",
                    "omit_background": False,
                    "animations": "disabled",
                    "caret": "hide",
                    "scale": "device",
                    "viewport_width": self.VIEWPORT_WIDTH,
                    "viewport_height": self.VIEWPORT_HEIGHT,
                    "device_scale_factor_level": "ultra",
                },
            )
        except Exception as exc:
            logger.warning(
                "Custom HTML T2I render failed, falling back to text_to_image: %s",
                exc,
            )
            return await self.text_to_image(fallback_text, return_url=True)

    def _compute_summary_metrics(self, summary: dict[str, Any]) -> dict[str, Any]:
        """Derive every percentage/average both report renderers need.

        優化重點 / Optimization: `_format_unified_report`（文字報表）與
        `_build_unified_report_html`（圖片報表）過去各自重算一份完全相同的
        衍生數值（百分比、平均值⋯），兩份公式各自維護，容易在其中一邊修改
        時忘記同步另一邊（v0.6.3 修的 Data Range 重複顯示問題即屬此類）。
        統一抽到這裡之後，兩個 renderer 只需要從回傳的 dict 讀值。
        """
        calls = int(summary.get("lifetime_calls") or 0)
        total = int(summary.get("total_tokens") or 0)
        input_tokens = int(summary.get("input_tokens") or 0)
        input_other = int(summary.get("input_other_tokens") or 0)
        input_cached = int(summary.get("input_cached_tokens") or 0)
        output_tokens = int(summary.get("output_tokens") or 0)
        provider_count = int(summary.get("provider_count") or 0)

        first_dt = self._parse_datetime(summary.get("first_record_at"))
        last_dt = self._parse_datetime(summary.get("last_record_at"))
        active_days = max((last_dt - first_dt).days, 1) if (first_dt and last_dt) else None

        return {
            "calls": calls,
            "total": total,
            "input_tokens": input_tokens,
            "input_other": input_other,
            "input_cached": input_cached,
            "output_tokens": output_tokens,
            "provider_count": provider_count,
            "avg_total": (total / calls) if calls else 0,
            "first_record": self._safe_datetime(summary.get("first_record_at")),
            "last_record": self._safe_datetime(summary.get("last_record_at")),
            "input_pct": (input_tokens / total * 100) if total else 0,
            "output_pct": (output_tokens / total * 100) if total else 0,
            "input_other_pct": (input_other / total * 100) if total else 0,
            "input_cached_pct": (input_cached / total * 100) if total else 0,
            "cached_pct_of_input": (input_cached / input_tokens * 100) if input_tokens else 0,
            "active_days": active_days,
            "avg_tokens_per_day": (total / active_days) if active_days else None,
            "avg_calls_per_day": (calls / active_days) if active_days else None,
        }

    def _format_unified_report(
        self,
        summary: dict[str, Any],
        rows: list[dict[str, Any]],
    ) -> str:
        m = self._compute_summary_metrics(summary)
        calls = m["calls"]
        total = m["total"]
        input_tokens = m["input_tokens"]
        input_other = m["input_other"]
        input_cached = m["input_cached"]
        output_tokens = m["output_tokens"]
        provider_count = m["provider_count"]
        avg_total = m["avg_total"]
        first_record = m["first_record"]
        last_record = m["last_record"]
        input_pct = m["input_pct"]
        output_pct = m["output_pct"]
        cached_pct_of_input = m["cached_pct_of_input"]
        input_other_pct = m["input_other_pct"]
        input_cached_pct = m["input_cached_pct"]

        if calls == 0:
            return (
                "Lifetime Token Report\n\n"
                "目前沒有 provider_stats 記錄。\n"
                "注意：此插件只統計 agent_type = internal 的既有記錄。"
            )

        lines = [
            "Lifetime Token Report",
            "",
            f"Agent Type: {self.AGENT_TYPE}",
            f"Configured Provider Limit: {self.provider_limit}",
            f"Provider Count: {self._fmt(provider_count)}",
            f"Total Calls: {self._fmt(calls)}",
            f"Total Tokens: {self._fmt(total)}",
            f"Input Tokens: {self._fmt(input_tokens)} ({input_pct:.2f}%)",
            f"  - Input Other: {self._fmt(input_other)} ({input_other_pct:.2f}%)",
            f"  - Input Cached: {self._fmt(input_cached)} ({input_cached_pct:.2f}% total, {cached_pct_of_input:.2f}% of input)",
            f"Output Tokens: {self._fmt(output_tokens)} ({output_pct:.2f}%)",
            f"Average Tokens / Call: {avg_total:,.2f}",
            f"First Record: {first_record}",
            f"Last Record: {last_record}",
            "",
            f"Top {min(len(rows), self.provider_limit)} Providers:",
        ]

        if not rows:
            lines.append("No provider rows.")
        else:
            for index, row in enumerate(rows, start=1):
                name, model = self._provider_display_name(row.get("provider_id"), row.get("provider_model"))
                provider_label = f"{name} / {model}" if model else name
                provider_calls = int(row.get("calls") or 0)
                provider_total = int(row.get("total_tokens") or 0)
                provider_input = int(row.get("input_tokens") or 0)
                provider_output = int(row.get("output_tokens") or 0)
                share = (provider_total / total * 100) if total else 0
                provider_first = self._safe_datetime(row.get("first_record_at"))
                provider_last = self._safe_datetime(row.get("last_record_at"))
                lines.extend(
                    [
                        "",
                        f"{index}. {provider_label}",
                        f"   Calls: {self._fmt(provider_calls)}",
                        f"   Total Tokens: {self._fmt(provider_total)} ({share:.2f}%)",
                        f"   Input: {self._fmt(provider_input)}",
                        f"   Output: {self._fmt(provider_output)}",
                        f"   First Record: {provider_first}",
                        f"   Last Record: {provider_last}",
                    ]
                )

        lines.extend(
            [
                "",
                "範圍：provider_stats 表內所有 agent_type = internal 的記錄。",
            ]
        )

        return "\n".join(lines)

    def _build_unified_report_html(
        self,
        summary: dict[str, Any],
        rows: list[dict[str, Any]],
        plugin_rows: list[dict[str, Any]] | None = None,
    ) -> str:
        m = self._compute_summary_metrics(summary)
        calls = m["calls"]
        total = m["total"]
        input_other = m["input_other"]
        input_cached = m["input_cached"]
        output_tokens = m["output_tokens"]
        provider_count = m["provider_count"]
        avg_total = m["avg_total"]
        first_record = m["first_record"]
        last_record = m["last_record"]
        active_days = m["active_days"]
        avg_tokens_per_day = m["avg_tokens_per_day"]
        avg_calls_per_day = m["avg_calls_per_day"]

        if calls == 0:
            body = """
              <div class="empty">
                <div class="empty-title">No provider_stats records</div>
                <div class="empty-subtitle">No lifetime token records available yet.</div>
              </div>
            """
            return self._wrap_html(
                title="Lifetime Token Report",
                subtitle=f"agent_type = {escape(self.AGENT_TYPE)}",
                body=body,
                generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
            )

        input_pct = m["input_pct"]
        output_pct = m["output_pct"]
        input_other_pct = m["input_other_pct"]
        input_cached_pct = m["input_cached_pct"]
        cached_pct_of_input = m["cached_pct_of_input"]

        token_pie_bg = (
            f"conic-gradient("
            f"#2563eb 0% {input_other_pct:.4f}%, "
            f"#7c3aed {input_other_pct:.4f}% {(input_other_pct + input_cached_pct):.4f}%, "
            f"#10b981 {(input_other_pct + input_cached_pct):.4f}% 100%)"
        )

        # Merge the old separate pie-legend and detailed-breakdown sections into
        # a single one (dot + label + value/percent + bar) so the same three
        # numbers are no longer duplicated across two sections.
        token_segments = [
            ("Input · Other", input_other, input_other_pct, "#2563eb"),
            ("Input · Cached", input_cached, input_cached_pct, "#7c3aed"),
            ("Output", output_tokens, output_pct, "#10b981"),
        ]

        token_rows_html = []
        for label, value, pct, color in token_segments:
            token_rows_html.append(
                f"""
                <div class="breakdown-row">
                  <div class="breakdown-head">
                    <span><i class="pie-dot" style="background:{color};"></i>{escape(label)}</span>
                    <b>{self._fmt(value)} · {pct:.2f}%</b>
                  </div>
                  <div class="breakdown-bar">
                    <div class="breakdown-fill" style="width: {pct:.2f}%; background: {color};"></div>
                  </div>
                </div>
                """
            )

        plugin_rows = plugin_rows or []
        max_plugin_tokens = max(
            (int(row.get("total_tokens") or 0) for row in plugin_rows),
            default=0,
        )
        plugin_cards = []
        for index, row in enumerate(plugin_rows):
            source = str(row.get("source") or self.LEGACY_SOURCE)
            is_legacy = source == self.LEGACY_SOURCE
            source_label = self._plugin_source_display_name(source)
            source_code = "No source tag" if is_legacy else f"plugin:{source}"
            source_calls = int(row.get("calls") or 0)
            source_input = int(row.get("input_tokens") or 0)
            source_cached = int(row.get("input_cached_tokens") or 0)
            source_output = int(row.get("output_tokens") or 0)
            source_total = int(row.get("total_tokens") or 0)
            source_share = (source_total / total * 100) if total else 0
            source_width = (
                source_total / max_plugin_tokens * 100 if max_plugin_tokens else 0
            )
            source_first = escape(self._safe_datetime(row.get("first_record_at")))
            source_last = escape(self._safe_datetime(row.get("last_record_at")))
            color = "#d97706" if is_legacy else self._provider_color(index + 2)
            legacy_class = " legacy" if is_legacy else ""
            legacy_note = (
                '<div class="plugin-note">Includes earlier rows without a reliable source tag.</div>'
                if is_legacy
                else ""
            )
            plugin_cards.append(
                f"""
                <div class="plugin-card{legacy_class}" style="--source-color:{color};">
                  <div class="plugin-card-head">
                    <div>
                      <div class="plugin-eyebrow">API SOURCE</div>
                      <div class="plugin-name">{escape(source_label)}</div>
                    </div>
                    <div class="plugin-total">{self._fmt(source_total)}</div>
                  </div>
                  <div class="plugin-source">{escape(source_code)}</div>
                  {legacy_note}
                  <div class="plugin-bar"><div style="width:{source_width:.2f}%"></div></div>
                  <div class="plugin-metrics">
                    <span>Calls <b>{self._fmt(source_calls)}</b></span>
                    <span>Input <b>{self._fmt(source_input)}</b></span>
                    <span>Cached <b>{self._fmt(source_cached)}</b></span>
                    <span>Output <b>{self._fmt(source_output)}</b></span>
                    <span>Share <b>{source_share:.2f}%</b></span>
                  </div>
                  <div class="plugin-dates">{source_first} → {source_last}</div>
                </div>
                """
            )

        if plugin_cards:
            plugin_section_body = f'<div class="plugin-ledger">{"".join(plugin_cards)}</div>'
        else:
            plugin_section_body = (
                '<div class="plugin-empty">No source-tagged plugin calls yet. '
                "New calls will appear after plugins using recorder v0.2.0 are active.</div>"
            )

        # Color is assigned by each provider's identity (provider_id +
        # provider_model, sorted alphabetically) rather than by its current
        # rank/position in `rows` (which is sorted by total_tokens and can
        # reorder from one report to the next as usage shifts). This keeps
        # a given provider's color stable across reports even when its
        # rank changes, while still giving every currently-shown provider
        # a distinct color (indices are still consecutive 0..N-1).
        identity_keys = sorted(
            {
                (str(row.get("provider_id") or "<unknown>"), str(row.get("provider_model") or "<unknown>"))
                for row in rows
            }
        )
        color_index = {key: i for i, key in enumerate(identity_keys)}

        def _color_for_row(row: dict[str, Any]) -> str:
            key = (str(row.get("provider_id") or "<unknown>"), str(row.get("provider_model") or "<unknown>"))
            return self._provider_color(color_index[key])

        max_tokens = max((int(row.get("total_tokens") or 0) for row in rows), default=0)
        provider_cards = []
        for index, row in enumerate(rows, start=1):
            # 修復 / Fix: 之前這裡直接用原始 provider_id / provider_model，
            # 沒有像文字報表跟 legend 一樣呼叫 _provider_display_name 去重，
            # 導致 provider_id 尾端已包含 model 名稱時（例如
            # "google_gemini/gemini-3.1-flash-lite" + "gemini-3.1-flash-lite"）
            # 卡片上會把同一個 model 名稱重複顯示兩次。
            name, model = self._provider_display_name(row.get("provider_id"), row.get("provider_model"))
            provider_id = escape(name)
            provider_model = escape(model)
            provider_calls = int(row.get("calls") or 0)
            provider_total = int(row.get("total_tokens") or 0)
            provider_input = int(row.get("input_tokens") or 0)
            provider_output = int(row.get("output_tokens") or 0)
            share = (provider_total / total * 100) if total else 0
            width = (provider_total / max_tokens * 100) if max_tokens else 0
            provider_first = escape(self._safe_datetime(row.get("first_record_at")))
            provider_last = escape(self._safe_datetime(row.get("last_record_at")))
            color = _color_for_row(row)
            rank_text_color = self._readable_text_color(color)
            # model 為空字串代表 provider_id 已經包含 model 名稱，此時整行省略，
            # 不留下一個空的 .provider-model div。
            model_line = f'<div class="provider-model">{provider_model}</div>' if provider_model else ""

            provider_cards.append(
                f"""
                <div class="provider-row" style="border-left: 4px solid {color};">
                  <div class="provider-head">
                    <div class="provider-name">
                      <span class="rank" style="background:{color}; color:{rank_text_color};">#{index}</span>
                      <span>{provider_id}</span>
                    </div>
                    <div class="provider-total">{self._fmt(provider_total)}</div>
                  </div>
                  {model_line}
                  <div class="bar"><div class="bar-fill" style="width: {width:.2f}%; background: {color};"></div></div>
                  <div class="provider-meta">
                    <span>Calls {self._fmt(provider_calls)}</span>
                    <span>Input {self._fmt(provider_input)}</span>
                    <span>Output {self._fmt(provider_output)}</span>
                    <span>Share {share:.2f}%</span>
                  </div>
                  <div class="provider-dates">
                    <span>First Record: {provider_first}</span>
                    <span>Last Record: {provider_last}</span>
                  </div>
                </div>
                """
            )

        providers_caption = f"Top {self.provider_limit} shown" if provider_count > len(rows) else ""

        body = f"""
          <div class="grid">
            <div class="metric hero">
              <div class="label">TOTAL TOKENS</div>
              <div class="value">{self._fmt(total)}</div>
            </div>
            <div class="metric">
              <div class="label">TOTAL CALLS</div>
              <div class="value">{self._fmt(calls)}</div>
            </div>
            <div class="metric">
              <div class="label">CACHE HIT RATE</div>
              <div class="value">{cached_pct_of_input:.2f}%</div>
            </div>
            <div class="metric">
              <div class="label">PROVIDERS</div>
              <div class="value">{self._fmt(provider_count)}</div>
              {f'<div class="caption">{escape(providers_caption)}</div>' if providers_caption else ''}
            </div>
          </div>

          <div class="section">
            <div class="section-title">Token Composition</div>
            <div class="pie-layout">
              <div class="pie-chart" style="background: {token_pie_bg};">
                <div class="pie-hole">
                  <div class="pie-total-label">Total</div>
                  <div class="pie-total-value">{self._fmt(total)}</div>
                </div>
              </div>
              <div class="pie-side">
                <div class="stack">
                  <div class="stack-input" style="width: {input_pct:.2f}%"></div>
                  <div class="stack-output" style="width: {output_pct:.2f}%"></div>
                </div>
                <div class="legend">
                  <span><i class="dot input"></i>Input {input_pct:.2f}%</span>
                  <span><i class="dot output"></i>Output {output_pct:.2f}%</span>
                </div>
                <div class="breakdown-list">
                  {''.join(token_rows_html)}
                </div>
              </div>
            </div>
          </div>

          <div class="details">
            <div><span>Avg Tokens / Call</span><b>{avg_total:,.2f}</b></div>
            <div><span>Avg Calls / Day</span><b>{f"{avg_calls_per_day:,.1f}" if avg_calls_per_day is not None else "N/A"}</b></div>
            <div><span>Active Period</span><b>{f"{active_days:,} days" if active_days is not None else "N/A"}</b></div>
            <div><span>Avg Tokens / Day</span><b>{f"{avg_tokens_per_day:,.0f}" if avg_tokens_per_day is not None else "N/A"}</b></div>
          </div>

          <div class="section plugin-section">
            <div class="section-head">
              <div>
                <div class="section-title">Plugin API Calls</div>
                <div class="section-subtitle">Source-tagged model calls · Legacy keeps earlier untagged rows visible</div>
              </div>
            </div>
            {plugin_section_body}
          </div>

          <div class="section">
            <div class="section-head">
              <div>
                <div class="section-title">Provider Lifetime Ranking</div>
                <div class="section-subtitle">Shown: {len(rows)} / {provider_count}</div>
              </div>
            </div>
            <div class="providers">
              {''.join(provider_cards)}
            </div>
          </div>
        """

        return self._wrap_html(
            title="Lifetime Token Report",
            subtitle=f"Data Range: {escape(first_record)} \u2192 {escape(last_record)}",
            body=body,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )

    def _wrap_html(
        self,
        title: str,
        subtitle: str,
        body: str,
        generated_at: str = "",
    ) -> str:
        width = self.VIEWPORT_WIDTH
        height = self.VIEWPORT_HEIGHT
        footer_generated = (
            f" · Generated at: {escape(generated_at)}" if generated_at else ""
        )
        return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width={width}, height={height}, initial-scale=1.0">
<style>
  * {{ box-sizing: border-box; }}
  html {{ width: {width}px; margin: 0; padding: 0; background: #eef2f7; }}
  body {{ width: {width}px; margin: 0; padding: 0; overflow-x: hidden; background: #eef2f7; font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK TC", "Noto Sans TC", "Microsoft JhengHei", Arial, sans-serif; color: #111827; }}
  .page {{ width: {width}px; max-width: {width}px; margin: 0; padding: 24px; }}
  .card {{ background: #ffffff; border-radius: 24px; padding: 24px; box-shadow: 0 20px 56px rgba(15, 23, 42, 0.14); border: 1px solid rgba(148, 163, 184, 0.35); }}
  .header {{ display: flex; justify-content: space-between; gap: 18px; align-items: flex-start; margin-bottom: 18px; }}
  .title {{ font-size: 28px; font-weight: 800; letter-spacing: -0.03em; margin: 0; }}
  .subtitle {{ margin-top: 6px; font-size: 14px; color: #64748b; }}
  .badge {{ padding: 10px 14px; border-radius: 999px; background: #f1f5f9; color: #334155; font-size: 15px; font-weight: 700; white-space: nowrap; }}
  .grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 16px; }}
  .metric {{ border-radius: 16px; padding: 14px; background: linear-gradient(180deg, #f8fafc, #eef2f7); border: 1px solid #e2e8f0; }}
  .metric.hero {{ background: linear-gradient(135deg, #eff6ff, #eef2ff); border: 1px solid #bfdbfe; }}
  .metric .label {{ font-size: 13px; color: #64748b; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; }}
  .metric .value {{ margin-top: 8px; font-size: 24px; font-weight: 800; letter-spacing: -0.035em; }}
  .metric.hero .value {{ color: #1d4ed8; font-size: 27px; }}
  .metric .caption {{ margin-top: 4px; font-size: 12px; color: #94a3b8; font-weight: 700; }}
  .section {{ margin-top: 14px; padding: 16px; border-radius: 18px; background: #f8fafc; border: 1px solid #e2e8f0; }}
  .section-title {{ font-size: 17px; font-weight: 800; margin-bottom: 10px; }}
  .section-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; margin-bottom: 14px; }}
  .section-subtitle {{ color: #64748b; font-size: 14px; font-weight: 700; }}
  .pie-layout {{ display: grid; grid-template-columns: 240px 1fr; gap: 16px; align-items: center; }}
  .pie-chart {{ width: 200px; height: 200px; border-radius: 50%; position: relative; margin: 0 auto; }}
  .pie-hole {{ position: absolute; inset: 24px; background: #ffffff; border-radius: 50%; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; box-shadow: inset 0 0 0 1px #e2e8f0; }}
  .pie-total-label {{ color: #64748b; font-size: 14px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; }}
  .pie-total-value {{ margin-top: 6px; font-size: 22px; font-weight: 900; letter-spacing: -0.04em; }}
  .pie-side {{ display: grid; gap: 10px; }}
  .pie-dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; flex-shrink: 0; }}
  .stack {{ display: flex; overflow: hidden; height: 24px; border-radius: 999px; background: #e2e8f0; }}
  .stack-input {{ background: linear-gradient(90deg, #2563eb, #60a5fa); }}
  .stack-output {{ background: linear-gradient(90deg, #10b981, #34d399); }}
  .legend {{ display: flex; gap: 14px; margin-top: 2px; font-size: 13px; color: #475569; font-weight: 700; flex-wrap: wrap; }}
  .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 7px; }}
  .dot.input {{ background: #2563eb; }}
  .dot.output {{ background: #10b981; }}
  .details {{ margin-top: 14px; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }}
  .details div {{ display: flex; justify-content: space-between; gap: 12px; padding: 12px 14px; border-radius: 12px; background: #f8fafc; border: 1px solid #e2e8f0; font-size: 14px; }}
  .details span {{ color: #64748b; font-weight: 700; }}
  .details b {{ text-align: right; overflow-wrap: anywhere; }}
  .providers {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }}
  .provider-row {{ border: 1px solid #e2e8f0; border-radius: 16px; padding: 14px; background: #fbfdff; }}
  .provider-head {{ display: flex; align-items: center; justify-content: space-between; gap: 18px; }}
  .provider-name {{ display: flex; align-items: center; gap: 8px; min-width: 0; font-size: 16px; font-weight: 800; overflow-wrap: anywhere; }}
  .rank {{ display: inline-flex; align-items: center; justify-content: center; min-width: 30px; height: 22px; padding: 0 8px; border-radius: 999px; font-size: 12px; font-weight: 800; flex-shrink: 0; }}
  .provider-total {{ font-size: 18px; font-weight: 900; white-space: nowrap; }}
  .provider-model {{ margin-top: 4px; color: #64748b; font-size: 13px; font-weight: 700; overflow-wrap: anywhere; }}
  .bar {{ height: 12px; border-radius: 999px; overflow: hidden; background: #e2e8f0; margin-top: 12px; }}
  .bar-fill {{ height: 100%; border-radius: 999px; }}
  .provider-meta {{ margin-top: 8px; display: flex; gap: 10px; flex-wrap: wrap; color: #475569; font-size: 12px; font-weight: 700; }}
  .provider-dates {{ margin-top: 8px; display: flex; gap: 12px; flex-wrap: wrap; color: #64748b; font-size: 11px; font-weight: 700; }}
  .plugin-section {{ background: linear-gradient(180deg, #f8fafc, #f1f5f9); }}
  .plugin-ledger {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }}
  .plugin-card {{ position: relative; overflow: hidden; padding: 15px; border-radius: 16px; background: #ffffff; border: 1px solid #dbe4ee; box-shadow: inset 4px 0 0 var(--source-color); }}
  .plugin-card.legacy {{ background: #fffbeb; border-color: #fde68a; }}
  .plugin-card-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }}
  .plugin-eyebrow {{ color: var(--source-color); font-size: 10px; line-height: 1; font-weight: 900; letter-spacing: 0.14em; }}
  .plugin-name {{ margin-top: 5px; font-size: 17px; font-weight: 900; letter-spacing: -0.02em; overflow-wrap: anywhere; }}
  .plugin-total {{ font-size: 20px; line-height: 1; font-weight: 900; white-space: nowrap; }}
  .plugin-source {{ display: inline-flex; margin-top: 8px; padding: 4px 8px; border-radius: 7px; background: #eef2f7; color: #475569; font: 700 11px/1.2 ui-monospace, SFMono-Regular, Consolas, monospace; overflow-wrap: anywhere; }}
  .plugin-note {{ margin-top: 7px; color: #92400e; font-size: 11px; font-weight: 700; }}
  .plugin-bar {{ height: 8px; margin-top: 11px; overflow: hidden; border-radius: 999px; background: #e2e8f0; }}
  .plugin-bar div {{ height: 100%; min-width: 3px; border-radius: inherit; background: var(--source-color); }}
  .plugin-metrics {{ margin-top: 9px; display: flex; flex-wrap: wrap; gap: 6px 13px; color: #64748b; font-size: 11px; font-weight: 700; }}
  .plugin-metrics b {{ margin-left: 3px; color: #1e293b; }}
  .plugin-dates {{ margin-top: 8px; color: #94a3b8; font-size: 10px; font-weight: 700; }}
  .plugin-empty {{ padding: 20px; border-radius: 14px; border: 1px dashed #cbd5e1; background: #ffffff; color: #64748b; font-size: 13px; font-weight: 700; text-align: center; }}
  .breakdown-list {{ display: grid; grid-template-columns: 1fr; gap: 8px; }}
  .breakdown-row {{ padding: 10px 12px; border-radius: 14px; background: #ffffff; border: 1px solid #e2e8f0; }}
  .breakdown-head {{ display: flex; justify-content: space-between; gap: 10px; align-items: center; font-size: 14px; font-weight: 800; margin-bottom: 8px; }}
  .breakdown-head span {{ color: #334155; display: flex; align-items: center; gap: 6px; }}
  .breakdown-head b {{ color: #111827; white-space: nowrap; }}
  .breakdown-bar {{ height: 12px; border-radius: 999px; overflow: hidden; background: #e2e8f0; }}
  .breakdown-fill {{ height: 100%; border-radius: 999px; min-width: 3px; }}
  .empty {{ padding: 72px 20px; text-align: center; border-radius: 22px; background: #f8fafc; border: 1px dashed #cbd5e1; }}
  .empty-title {{ font-size: 30px; font-weight: 900; }}
  .empty-subtitle {{ margin-top: 10px; color: #64748b; font-size: 18px; line-height: 1.6; }}
  .footer {{ margin-top: 16px; color: #94a3b8; font-size: 12px; font-weight: 700; }}
</style>
</head>
<body>
  <div class="page">
    <div class="card">
      <div class="header">
        <div>
          <h1 class="title">{title}</h1>
          <div class="subtitle">{escape(subtitle)}</div>
        </div>
        <div class="badge">AstrBot · T2I</div>
      </div>
      {body}
      <div class="footer">Source: provider_stats · Lifetime means all existing records in the database{footer_generated}</div>
    </div>
  </div>
</body>
</html>"""

    @classmethod
    def _resolve_provider_limit(cls, config: dict[str, Any]) -> int:
        raw = config.get("provider_limit", cls.DEFAULT_PROVIDER_LIMIT)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = cls.DEFAULT_PROVIDER_LIMIT
        if value < 1:
            value = cls.DEFAULT_PROVIDER_LIMIT
        if value > cls.MAX_PROVIDER_LIMIT:
            value = cls.MAX_PROVIDER_LIMIT
        return value

    @staticmethod
    def _provider_display_name(provider_id: Any, provider_model: Any) -> tuple[str, str]:
        """Return (id, model) for display, dropping model if already embedded in id.

        Some AstrBot providers store provider_id as a compound value that
        already ends with the model name (e.g. provider_id=
        "google_gemini/gemini-3.1-flash-lite", provider_model=
        "gemini-3.1-flash-lite"). Naively joining them as "id / model"
        then repeats the model name. This detects that overlap and returns
        an empty model string so callers can skip the duplicate part.
        """
        pid = str(provider_id) if provider_id else "<unknown>"
        pmodel = str(provider_model) if provider_model else "<unknown>"
        if pmodel != "<unknown>" and pid != "<unknown>" and pid.endswith(pmodel):
            return pid, ""
        return pid, pmodel

    @classmethod
    def _plugin_source_display_name(cls, source: str) -> str:
        if source == cls.LEGACY_SOURCE:
            return "Legacy / Unattributed"
        return cls.PLUGIN_SOURCE_LABELS.get(
            source,
            source.replace("_", " ").replace("-", " ").title(),
        )

    @staticmethod
    def _provider_color(index: int) -> str:
        """Generate a stable, well-spread hex color for provider index.

        以黃金角（golden angle）旋轉色相，讓最多 50 個供應商也能各自取得
        視覺上可分辨的顏色，取代舊版固定 11 色陣列。
        Uses the golden-angle hue rotation so up to MAX_PROVIDER_LIMIT (50)
        providers each get a visually distinct color instead of the old
        fixed 11-color palette repeating.
        """
        hue = (index * 137.508) % 360
        r, g, b = colorsys.hls_to_rgb(hue / 360, 0.54, 0.62)
        return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))

    @staticmethod
    def _readable_text_color(hex_color: str) -> str:
        """Pick black or white text for the best contrast against hex_color.

        部分黃色系色相若固定使用白色文字，對比度會過低難以閱讀；
        改為依背景亮度動態選擇黑／白文字，確保排行徽章在任何顏色下都清晰可讀。
        Some yellow-ish hues have poor contrast with fixed white text;
        pick black or white per-badge based on background luminance so the
        ranking badge stays legible regardless of the generated hue.
        """
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)

        def _lin(c: float) -> float:
            c = c / 255
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

        luminance = 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)
        contrast_white = (1.0 + 0.05) / (luminance + 0.05)
        contrast_dark = (luminance + 0.05) / (0.0088 + 0.05)
        return "#ffffff" if contrast_white >= contrast_dark else "#111827"

    @staticmethod
    def _fmt(value: int | float | None) -> str:
        if value is None:
            return "0"
        return f"{int(value):,}"

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        """Parse value into a datetime object, trying several common formats.

        Shared by `_safe_datetime` (for display) and by callers that need
        to do date math (e.g. computing an active-day span).
        """
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        text = str(value).strip()
        if not text:
            return None
        normalized = text.replace("T", " ")
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        for candidate in (normalized, text):
            try:
                return datetime.fromisoformat(candidate)
            except ValueError:
                pass
        for fmt in (
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
        ):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None

    @staticmethod
    def _safe_datetime(value: Any) -> str:
        if value is None:
            return "N/A"
        dt = LifetimeTokenStatsPlugin._parse_datetime(value)
        if dt is not None:
            return dt.strftime("%Y-%m-%d %H:%M")
        text = str(value).strip()
        return text if text else "N/A"
