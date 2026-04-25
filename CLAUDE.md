# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Bot

```bash
uv run bot.py
```

Required environment variables (all must be set, except `GEMINI_MODEL`):

| Variable | Description |
|---|---|
| `NOTION_TOKEN` | Notion internal integration secret |
| `SOURCE_DATABASE_ID` | Notion data source ID for raw notes |
| `TARGET_DATABASE_ID` | Notion data source ID for generated guides |
| `GEMINI_API_KEY` | Google AI Studio API key |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from BotFather |
| `TELEGRAM_CHAT_ID` | Telegram chat/group/channel ID |
| `GEMINI_MODEL` | (optional) defaults to `gemini-1.5-flash` |

## Code Architecture

Single file: `bot.py`. No framework, no state store — state is tracked entirely via the `Processed by AI` Notion checkbox.

### Pipeline (runs once per invocation, one page per run)

```
Config.validate()
NotionClient.fetch_unprocessed_pages(source_id)
  → sort by created_time, take pages[0]
  → NotionClient.get_page_text_content(page_id)   # paginated block fetch
  → GeminiClient.generate_study_guide(content)    # → {summary, polls[]}
  → markdown_to_notion_blocks(summary)
  → NotionClient.create_target_page(target_id, title, blocks)
  → TelegramClient.send_message(summary)
  → TelegramClient.send_poll(poll) × len(polls)
  → NotionClient.mark_as_processed(page_id)
```

### Key design decisions

**Notion API version:** Uses `2025-09-03` with `data_sources` endpoints (`/v1/data_sources/{id}/query`, `/v1/pages` with `parent: {data_source_id: ...}`). This is distinct from the older `databases` API — do not mix them.

**Gemini JSON parsing:** Uses `json.JSONDecoder().raw_decode()` rather than `json.loads()`. This handles cases where the model appends extra content after the JSON object, which `json.loads()` would reject as "Extra data".

**Gemini output contract:** The system prompt enforces this exact JSON schema. Any prompt changes must preserve it or update `main()`:
```json
{
  "summary": "Markdown string",
  "polls": [{"question", "options[]", "correct_option_index", "explanation"}]
}
```

**Telegram HTML mode:** Telegram's `sendMessage` uses `parse_mode: HTML`. Gemini returns Markdown, so `TelegramClient._sanitize_html()` escapes `&`, `<`, `>` then converts `**bold**` → `<b>` and `*italic*` → `<i>`. Raw Markdown sent to Telegram renders as literal asterisks.

**Telegram poll limits** (enforced in `send_poll`):
- Question: 300 chars
- Each option: 100 chars (max 10 options)
- Explanation: 200 chars

**Title extraction:** `NotionClient.extract_title()` finds the title property by checking `prop_val.get("type") == "title"` rather than looking for a property named "Name" or "Title". This is intentional — the property can be named anything.

**Retry logic:** `GeminiClient.generate_study_guide` uses `tenacity` with `stop_after_attempt(5)`, `wait_exponential(multiplier=2, min=10, max=60)`. Only 429/RESOURCE_EXHAUSTED errors are explicitly re-raised to trigger retries; other errors raise `StudyGuideBotError` immediately.

## Deployment

GitHub Actions cron at 13:00 UTC daily. All secrets stored as GitHub repository secrets. See README.md for full setup instructions.
