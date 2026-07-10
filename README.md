# astrbot_plugin_lifetime_tokens

A small AstrBot plugin that reads the existing `provider_stats` table and reports lifetime token usage.

This version uses a unified design with only two commands.

## Preview

![Preview](assets/preview.png)

## Commands

```text
/token_text
/token
```

## Optional config

```yaml
provider_limit: 10
```

- `provider_limit`: how many top providers to show
- default: `10`
- max: `50`

## Output design

### Unified text report

Shows all of the following in one message:

- summary
- token breakdown
- provider ranking
- each provider's first / last record

### Unified image report

Shows all of the following in one image:

- summary cards
- percentage pie chart style token composition
- detailed token breakdown
- provider lifetime ranking
- provider token share pie chart
- each provider's first / last record

## Scope

This plugin only reads records where:

```sql
agent_type = 'internal'
```

It does not modify AstrBot's built-in Dashboard Token Statistics page, and it does not write or delete database records.

## T2I behavior

The image command uses `self.html_render()` to render a styled HTML card through AstrBot T2I.

If custom HTML rendering fails, the plugin falls back to `self.text_to_image()` with the plain text result.

## Install

Put this folder into:

```text
AstrBot/data/plugins/astrbot_plugin_lifetime_tokens/
```

Then reload plugins in AstrBot WebUI.

## Notes

- Lifetime means all existing records in `provider_stats`.
- It cannot recover records that were never saved.
- It does not count external HTTP APIs or plugin-owned direct API calls unless those calls were already recorded in `provider_stats`.


## Rendering updates

- Output image format remains `PNG`
- T2I rendering now uses higher-density settings for sharper output
- Layout spacing is compressed to reduce final image height

## v0.6.0 — T2I image UI overhaul

- All section titles, metric labels, and field names in the image report are
  now bilingual (Traditional Chinese + English), e.g. "Total Tokens 總 Token
  數". Text report (`/lifetime_report`) is unchanged.
- Merged the old "Token Composition" pie section and the separate "Detailed
  Token Breakdown" grid into a single section — the same three numbers were
  previously shown twice.
- Replaced the fixed 11-color provider palette with a golden-angle HSL
  color generator, so up to `MAX_PROVIDER_LIMIT` (50) providers each get a
  visually distinct color instead of repeating.
- Reworked "Provider Lifetime Ranking" from a tall sticky sidebar + list
  layout (which left blank space when the list was longer than the pie
  card) into a horizontal pie + wrapping legend strip above a full-width
  ranked list.
- Each provider row now has a colored left accent and a colored rank badge
  that match its slice in the pie chart above, making the chart-to-list
  mapping clearer at a glance.
- Rank badge text color (black or white) is now chosen automatically based
  on contrast against its background color, so it stays readable for every
  generated hue.
- The "Total Tokens" metric card is now visually emphasized as the primary
  KPI. The header subtitle now shows the record date range instead of
  repeating the `agent_type` filter (still noted in the footer).
- Footer now includes the report generation time.

## v0.6.1

- Reverted the image report back to English-only labels (no bilingual
  Chinese/English text). The text report (`/lifetime_report`) already was
  and remains English/Chinese mixed as before.
- "Provider Lifetime Ranking" now lays out 2 provider cards per row instead
  of 1 (e.g. row 1 = #1 and #2, row 2 = #3 and #4, ...), which roughly
  halves the vertical height of that section for the same provider count.

## v0.6.2

- Fixed a label bug where the pie-chart legend (and the text report's
  provider line) could show the model name twice, e.g.
  `google_gemini/gemini-3.1-flash-lite / gemini-3.1-flash-lite`. This
  happened when `provider_id` was already a compound value ending with the
  model name. Both places now detect that overlap and show it once, e.g.
  `google_gemini/gemini-3.1-flash-lite`.
- The pie-chart legend inside "Provider Lifetime Ranking" now shows exactly
  2 entries per row (grid layout) instead of auto-wrapping 2-3 per row
  depending on label length.

## v0.6.3

- Removed a duplicate-info issue where the header subtitle ("Data Range")
  and the Details grid ("First Record" / "Last Record") showed the exact
  same two dates. The Details grid now shows "Active Period" (day span
  between first and last record) and "Avg Tokens / Day" instead — new
  information rather than a repeat of the header.
- Provider colors (pie slices, legend dots, card accents, rank badges) are
  now assigned based on each provider's identity (`provider_id` +
  `provider_model`, sorted alphabetically), not its position in the
  current total-tokens ranking. Previously, if two providers swapped rank
  between reports (e.g. due to normal usage fluctuation), their colors
  would swap too. Now a given provider keeps the same color across reports
  as long as it stays within the shown set, independent of its rank.

## v0.6.4

- Replaced the "Input Tokens" / "Output Tokens" hero metric cards, which
  duplicated the "Token Composition" section directly below them, with:
  - **Cache Hit Rate** (`input_cached_tokens / input_tokens`) — computed
    before but never actually shown in the image report.
  - **Providers** (distinct provider count), promoted from a line of text
    inside the Details grid to its own hero card. Shows a small "Top N
    shown" caption underneath only when the count is actually truncated.
- Details grid: since "Providers" moved up to the hero row, added
  "Avg Calls / Day" in its place to keep the 2x2 grid balanced.

## v0.6.5 — Backend performance & de-duplication

Backend-focused changes; no visible UI changes.

- **Merged two full-table scans into one.** Previously each command ran a
  dedicated `COUNT`/`SUM` summary query *and* a separate `GROUP BY` +
  `LIMIT` provider query, both scanning the entire `provider_stats` table,
  sequentially. Now there is a single `GROUP BY` query (no `LIMIT`) that
  returns every provider group; the lifetime summary is derived in Python
  by summing/min/max-ing over that same result set, and the displayed
  rows are simply the first `provider_limit` entries of it. One query
  instead of two, and `provider_count` no longer needs its own
  `COUNT(DISTINCT ...)` expression.
- **Removed duplicate summary-metric calculations.** `_format_unified_report`
  (text) and `_build_unified_report_html` (image) used to each recompute
  the same set of derived numbers (percentages, averages, active-day
  span) from scratch. Both now call a single `_compute_summary_metrics()`
  helper, so the formulas only exist in one place — the same class of bug
  fixed for display in v0.6.3, now fixed for the underlying calculations.
- **Error messages no longer leak exception details to chat.** On failure,
  users now see a generic message pointing at the log; the exception
  type/message is still fully logged via `logger.exception(...)` for
  debugging, just not echoed back into the chat.
- **Fixed: provider cards in the image report skipped the id/model
  de-dup helper.** `_provider_display_name()` (already used by the text
  report and the ranking pie legend) strips the model name when
  `provider_id` already ends with it — e.g. `provider_id=
  "google_gemini/gemini-3.1-flash-lite"` with `provider_model=
  "gemini-3.1-flash-lite"`. The per-provider cards below the pie were
  built from the raw `provider_id`/`provider_model` fields directly and
  skipped this check, so that model name rendered twice on the card
  even though the legend right above it already showed it de-duplicated.
  Cards now call the same helper and omit the model line entirely when
  there's nothing left to show after de-duping.

## v0.6.6

- Removed the "Provider Ranking" pie chart + compact legend strip that
  sat above the per-provider card list. It showed the same three things
  (color, name, share %) that each card below it already shows via its
  rank-badge color, name, and `Share` line — a duplicate-info pattern in
  the same spirit as the ones fixed in earlier versions, just not caught
  until now. The section now goes straight from the "Provider Lifetime
  Ranking" header into the card grid. Removed the now-dead
  `provider_segments`/`provider_pie_bg`/`provider_legend_items`
  computation and the corresponding `.provider-pie-*` / `.pie-legend*`
  CSS, along with the now-unused `OTHERS_COLOR` constant (its only
  consumer, the pie's "Others" slice, no longer exists).
