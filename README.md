# Notion Study Guide Bot

A Python bot that reads raw Islamic Studies notes from a Notion database, generates a structured study guide and multiple-choice quiz via Google Gemini, writes the guide back to a second Notion database, and pushes the summary + quiz polls to a Telegram chat/group/channel — all on a daily GitHub Actions schedule.

---

## How It Works (End-to-End Flow)

```
GitHub Actions (cron: 13:00 UTC daily)
  │
  ▼
1. Query SOURCE Notion database for pages where "Processed by AI" = false
2. Sort by created_time, pick the oldest unprocessed page
3. Fetch the full block content of that page (paginated)
4. Send content to Google Gemini → returns JSON:
     {
       "summary": "Markdown-formatted executive brief",
       "polls": [{ "question", "options", "correct_option_index", "explanation" }]
     }
5. Convert summary Markdown → Notion block objects
6. Create a new page in TARGET Notion database titled "Study Guide: <topic>"
7. Send summary as an HTML message to Telegram
8. Send each poll as a Telegram quiz poll
9. Mark the source page as "Processed by AI" = true
```

One page is processed per run. The queue drains oldest-first.

---

## Architecture

Everything lives in a **single file** (`bot.py`). There is no framework, no database, no state beyond the `Processed by AI` checkbox in Notion.

### Classes

| Class | Responsibility |
|---|---|
| `Config` | Load + validate all environment variables. Logs partial token values (first 4–5 chars) so you can confirm secrets are set without exposing them. |
| `NotionClient` | All Notion API calls: query source DB, paginate block children, create target page, mark processed. Uses **Notion API v2025-09-03** with `data_sources` endpoints. |
| `GeminiClient` | Call Gemini with `response_mime_type="application/json"` to get structured output. Retries on rate limits (429 / RESOURCE_EXHAUSTED) with exponential backoff, up to 5 attempts. |
| `TelegramClient` | Send HTML messages and quiz polls. Enforces Telegram's field length limits. Converts Markdown `**bold**` / `*italic*` to HTML tags before sending. |

### Standalone function

`markdown_to_notion_blocks(md_text)` — converts the Gemini summary (Markdown string) into a list of Notion block objects (`heading_1/2/3`, `bulleted_list_item`, `numbered_list_item`, `paragraph`).

### Gemini JSON contract

The system prompt hard-codes this output schema:

```json
{
  "summary": "Telegram-friendly Markdown string",
  "polls": [
    {
      "question": "...",
      "options": ["A", "B", "C", "D"],
      "correct_option_index": 2,
      "explanation": "..."
    }
  ]
}
```

If you change the prompt, keep this structure or update the `main()` consumers accordingly.

---

## Setup

### Prerequisites

- Python 3.13+
- [`uv`](https://docs.astral.sh/uv/) (package manager)
- A GitHub repository (for Actions)

### 1. Notion

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations) → **New integration** → save the **Internal Integration Secret** (`NOTION_TOKEN`).
2. Create two databases: one for raw notes (source), one for generated guides (target).
3. Share both databases with your integration: open the database → `...` menu → **Add connections** → select your integration.
4. **Source database requirements:**
   - A **Checkbox** property named exactly `Processed by AI`
   - Any **Title**-type property (the bot finds it by type, not by name)
5. Get the database IDs from the Notion URL (the UUID after the last `/` before the `?`).

### 2. Google Gemini

1. Go to [aistudio.google.com](https://aistudio.google.com) → **Get API key**.
2. Save this as `GEMINI_API_KEY`.
3. Default model is `gemini-1.5-flash`. Override with the `GEMINI_MODEL` secret/env var.

### 3. Telegram

#### Create a bot
1. Message [@BotFather](https://t.me/botfather) on Telegram.
2. Send `/newbot`, choose a name and username.
3. Copy the **API Token** → `TELEGRAM_BOT_TOKEN`.

#### Get the Chat ID

| Destination | How to get the ID | ID format |
|---|---|---|
| **Private chat** | Message your bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` and find `"chat": {"id": ...}` | Positive integer, e.g. `123456789` |
| **Group** | Add bot to group, send a message, check `getUpdates` | Negative integer, e.g. `-987654321` |
| **Supergroup** (groups with topics enabled, or upgraded groups) | Same as group | `-100` prefix, e.g. `-100987654321` |
| **Channel** | Open [web.telegram.org](https://web.telegram.org), click your channel, read the URL: `.../#-100123456789` | `-100` prefix, e.g. `-100123456789` |

> **Channel requirement:** The bot must be an **Administrator** of the channel to post.

### 4. GitHub Actions Secrets

Go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret | Value |
|---|---|
| `NOTION_TOKEN` | Notion integration secret |
| `SOURCE_DATABASE_ID` | UUID of the raw notes database |
| `TARGET_DATABASE_ID` | UUID of the generated guides database |
| `GEMINI_API_KEY` | Google AI Studio API key |
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather |
| `TELEGRAM_CHAT_ID` | Chat/group/channel ID (see table above) |
| `GEMINI_MODEL` | *(optional)* e.g. `gemini-1.5-flash`, `gemini-3-flash-preview` |

---

## Running

### Automated (GitHub Actions)

The workflow runs daily at **13:00 UTC**. Each run processes exactly one note (the oldest unprocessed one). To work through your backlog faster, trigger runs manually.

### Manual trigger

Go to **Actions → your workflow → Run workflow**.

### Local development

```bash
# Set env vars first (or use a .env loader)
export NOTION_TOKEN=...
export SOURCE_DATABASE_ID=...
export TARGET_DATABASE_ID=...
export GEMINI_API_KEY=...
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...

uv run bot.py
```

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `Missing required environment variables` | A required secret is unset or empty | Check GitHub secrets; run locally with all vars exported |
| `Source Data Source query failed` | Wrong `SOURCE_DATABASE_ID` or integration not connected to that database | Re-share the database with the integration |
| `Extra data: line N column 1` | Gemini returned extra content after the JSON object | Fixed in code via `raw_decode`; if it persists, try a different `GEMINI_MODEL` |
| `chat not found` (Telegram 400) | Wrong `TELEGRAM_CHAT_ID` format | Channels/supergroups need the `-100` prefix; verify via `getUpdates` |
| `Forbidden` (Telegram 403) | Bot is not an admin of the channel | Promote the bot to Administrator in the channel settings |
| Rate limit / `RESOURCE_EXHAUSTED` | Gemini free tier quota hit | The bot retries automatically (up to 5×, exponential backoff). Switch to a paid key or different model if persistent. |
