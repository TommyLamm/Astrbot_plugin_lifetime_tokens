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
    # "Others" 分類固定使用中性灰色，不與供應商配色混淆 / fixed neutral for the "Others" bucket
    OTHERS_COLOR = "#94a3b8"

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.plugin_config = config or {}
        self.provider_limit = self._resolve_provider_limit(self.plugin_config)

    async def initialize(self) -> None:
        logger.info(
            "Lifetime Token Stats plugin loaded. provider_limit=%s",
            self.provider_limit,
        )

    @filter.command("lifetime_report")
    async def lifetime_report(self, event: AstrMessageEvent):
        """Show a unified lifetime token report in text."""
        try:
            summary = await self._query_lifetime_summary()
            rows = await self._query_provider_lifetime()
            yield event.plain_result(self._format_unified_report(summary, rows))
        except Exception as exc:
            logger.exception("Failed to query unified lifetime token report.")
            yield event.plain_result(
                "查詢 unified lifetime token report 失敗。\n"
                f"錯誤：{type(exc).__name__}: {exc}"
            )

    @filter.command("lifetime_report_img")
    async def lifetime_report_img(self, event: AstrMessageEvent):
        """Render a unified lifetime token report as a T2I image."""
        try:
            summary = await self._query_lifetime_summary()
            rows = await self._query_provider_lifetime()
            html = self._build_unified_report_html(summary, rows)
            fallback_text = self._format_unified_report(summary, rows)
            image = await self._render_stats_image(html, fallback_text)
            yield event.image_result(image)
        except Exception as exc:
            logger.exception("Failed to render unified lifetime token report image.")
            yield event.plain_result(
                "生成 unified lifetime token report 圖片失敗。\n"
                f"錯誤：{type(exc).__name__}: {exc}"
            )

    async def terminate(self) -> None:
        logger.info("Lifetime Token Stats plugin unloaded.")

    async def _query_lifetime_summary(self) -> dict[str, Any]:
        db = getattr(self.context, "_db", None)
        if db is None or not hasattr(db, "get_db"):
            raise RuntimeError("Cannot access AstrBot database from plugin context.")

        sql = text(
            """
            SELECT
                COUNT(*) AS lifetime_calls,
                COALESCE(SUM(token_input_other), 0) AS input_other_tokens,
                COALESCE(SUM(token_input_cached), 0) AS input_cached_tokens,
                COALESCE(SUM(token_input_other + token_input_cached), 0) AS input_tokens,
                COALESCE(SUM(token_output), 0) AS output_tokens,
                COALESCE(SUM(token_input_other + token_input_cached + token_output), 0)
                    AS total_tokens,
                MIN(created_at) AS first_record_at,
                MAX(created_at) AS last_record_at,
                COUNT(DISTINCT COALESCE(provider_id, '<unknown>') || '|' || COALESCE(provider_model, '<unknown>'))
                    AS provider_count
            FROM provider_stats
            WHERE agent_type = :agent_type
            """
        )

        async with db.get_db() as session:
            result = await session.execute(sql, {"agent_type": self.AGENT_TYPE})
            row = result.mappings().first()

        return dict(row or {})

    async def _query_provider_lifetime(self) -> list[dict[str, Any]]:
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
            LIMIT :limit
            """
        )

        async with db.get_db() as session:
            result = await session.execute(
                sql,
                {
                    "agent_type": self.AGENT_TYPE,
                    "limit": self.provider_limit,
                },
            )
            rows = result.mappings().all()

        return [dict(row) for row in rows]

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

    def _format_unified_report(
        self,
        summary: dict[str, Any],
        rows: list[dict[str, Any]],
    ) -> str:
        calls = int(summary.get("lifetime_calls") or 0)
        total = int(summary.get("total_tokens") or 0)
        input_tokens = int(summary.get("input_tokens") or 0)
        input_other = int(summary.get("input_other_tokens") or 0)
        input_cached = int(summary.get("input_cached_tokens") or 0)
        output_tokens = int(summary.get("output_tokens") or 0)
        provider_count = int(summary.get("provider_count") or 0)
        avg_total = total / calls if calls else 0
        first_record = self._safe_datetime(summary.get("first_record_at"))
        last_record = self._safe_datetime(summary.get("last_record_at"))
        input_pct = (input_tokens / total * 100) if total else 0
        output_pct = (output_tokens / total * 100) if total else 0
        cached_pct_of_input = (input_cached / input_tokens * 100) if input_tokens else 0
        input_other_pct = (input_other / total * 100) if total else 0
        input_cached_pct = (input_cached / total * 100) if total else 0

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
    ) -> str:
        calls = int(summary.get("lifetime_calls") or 0)
        total = int(summary.get("total_tokens") or 0)
        input_tokens = int(summary.get("input_tokens") or 0)
        input_other = int(summary.get("input_other_tokens") or 0)
        input_cached = int(summary.get("input_cached_tokens") or 0)
        output_tokens = int(summary.get("output_tokens") or 0)
        provider_count = int(summary.get("provider_count") or 0)
        avg_total = total / calls if calls else 0
        first_record = self._safe_datetime(summary.get("first_record_at"))
        last_record = self._safe_datetime(summary.get("last_record_at"))
        first_dt = self._parse_datetime(summary.get("first_record_at"))
        last_dt = self._parse_datetime(summary.get("last_record_at"))
        active_days = max((last_dt - first_dt).days, 1) if (first_dt and last_dt) else None
        avg_tokens_per_day = (total / active_days) if active_days else None

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

        output_pct = (output_tokens / total * 100) if total else 0
        input_other_pct = (input_other / total * 100) if total else 0
        input_cached_pct = (input_cached / total * 100) if total else 0
        cached_pct_of_input = (input_cached / input_tokens * 100) if input_tokens else 0

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

        shown_tokens = sum(int(row.get("total_tokens") or 0) for row in rows)
        others_tokens = max(total - shown_tokens, 0)

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

        provider_segments = []
        for row in rows:
            provider_total = int(row.get("total_tokens") or 0)
            pct = (provider_total / total * 100) if total else 0.0
            color = _color_for_row(row)
            name, model = self._provider_display_name(row.get("provider_id"), row.get("provider_model"))
            label = f"{name} / {model}" if model else name
            provider_segments.append((label, provider_total, pct, color))
        if others_tokens > 0:
            provider_segments.append(
                (
                    "Others",
                    others_tokens,
                    (others_tokens / total * 100) if total else 0.0,
                    self.OTHERS_COLOR,
                )
            )

        provider_pie_parts = []
        provider_legend_items = []
        cumulative = 0.0
        for label, value, pct, color in provider_segments:
            next_cumulative = cumulative + pct
            provider_pie_parts.append(f"{color} {cumulative:.4f}% {next_cumulative:.4f}%")
            cumulative = next_cumulative
            provider_legend_items.append(
                f"""
                <div class="pie-legend-item small">
                  <i class="pie-dot" style="background:{color};"></i>
                  <span>{escape(label)}</span>
                  <b>{pct:.2f}%</b>
                </div>
                """
            )
        provider_pie_bg = f"conic-gradient({', '.join(provider_pie_parts)})" if provider_pie_parts else "#e2e8f0"

        max_tokens = max((int(row.get("total_tokens") or 0) for row in rows), default=0)
        provider_cards = []
        for index, row in enumerate(rows, start=1):
            provider_id = escape(str(row.get("provider_id") or "<unknown>"))
            provider_model = escape(str(row.get("provider_model") or "<unknown>"))
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
                  <div class="provider-model">{provider_model}</div>
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
              <div class="label">INPUT TOKENS</div>
              <div class="value">{self._fmt(input_tokens)}</div>
            </div>
            <div class="metric">
              <div class="label">OUTPUT TOKENS</div>
              <div class="value">{self._fmt(output_tokens)}</div>
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
                <div class="breakdown-list">
                  {''.join(token_rows_html)}
                </div>
              </div>
            </div>
          </div>

          <div class="details">
            <div><span>Providers</span><b>{self._fmt(provider_count)} · Top {self.provider_limit} shown</b></div>
            <div><span>Avg Tokens / Call</span><b>{avg_total:,.2f}</b></div>
            <div><span>Active Period</span><b>{f"{active_days:,} days" if active_days is not None else "N/A"}</b></div>
            <div><span>Avg Tokens / Day</span><b>{f"{avg_tokens_per_day:,.0f}" if avg_tokens_per_day is not None else "N/A"}</b></div>
          </div>

          <div class="section">
            <div class="section-head">
              <div>
                <div class="section-title">Provider Lifetime Ranking</div>
                <div class="section-subtitle">Shown: {len(rows)} / {provider_count}</div>
              </div>
            </div>
            <div class="provider-pie-strip">
              <div class="provider-pie-chart" style="background: {provider_pie_bg};">
                <div class="provider-pie-hole">
                  <div class="pie-total-label">Total</div>
                  <div class="pie-total-value">{self._fmt(total)}</div>
                </div>
              </div>
              <div class="pie-legend compact">
                {''.join(provider_legend_items)}
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
  .pie-legend {{ display: grid; gap: 8px; }}
  .pie-legend.compact {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; flex: 1; min-width: 220px; }}
  .pie-legend-item {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 8px 10px; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; font-size: 13px; }}
  .pie-legend-item.small {{ font-size: 12px; padding: 6px 10px; }}
  .pie-legend-item span {{ flex: 1; color: #334155; font-weight: 700; overflow-wrap: anywhere; }}
  .pie-legend-item b {{ white-space: nowrap; }}
  .pie-dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; flex-shrink: 0; }}
  .details {{ margin-top: 14px; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }}
  .details div {{ display: flex; justify-content: space-between; gap: 12px; padding: 12px 14px; border-radius: 12px; background: #f8fafc; border: 1px solid #e2e8f0; font-size: 14px; }}
  .details span {{ color: #64748b; font-weight: 700; }}
  .details b {{ text-align: right; overflow-wrap: anywhere; }}
  .providers {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }}
  .provider-pie-strip {{ display: flex; align-items: center; gap: 16px; margin-bottom: 12px; padding: 14px; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 16px; flex-wrap: wrap; }}
  .provider-pie-chart {{ width: 130px; height: 130px; border-radius: 50%; position: relative; flex-shrink: 0; }}
  .provider-pie-hole {{ position: absolute; inset: 16px; background: #ffffff; border-radius: 50%; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; box-shadow: inset 0 0 0 1px #e2e8f0; }}
  .provider-pie-hole .pie-total-label {{ font-size: 11px; }}
  .provider-pie-hole .pie-total-value {{ margin-top: 3px; font-size: 15px; }}
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
