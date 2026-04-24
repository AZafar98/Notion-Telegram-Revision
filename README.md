# Notion Dual-Database Study Guide Generator (v2025-09-03)

This bot automates the creation of study guides using the **Notion API v2025-09-03**. It reads raw notes from a **Source Data Source**, generates summarized guides in a **Target Data Source** using Google Gemini, and sends a notification to **Telegram** daily.

## 🏗 Architecture
1. **Source Data Source**: The specific data table containing your raw notes.
   - Requirement: A Checkbox property named `Processed by AI`.
2. **Gemini AI**: Summarizes the oldest unprocessed note into a structured study guide.
3. **Target Data Source**: The data table where the new guide is created.
4. **Telegram Notification**: Sends a link to the new guide to your private chat, group, or channel.

---

## 🛠 Setup Instructions

### 1. Notion Setup (Finding your Data Source IDs)
1. **Create Integration**: Go to [Notion My Integrations](https://www.notion.so/my-integrations) and create a new integration. Save the **Internal Integration Secret** (`NOTION_TOKEN`).
2. **Databases**: Create your Source and Target databases in Notion.
3. **Connections**: Share both databases with your integration (`...` menu -> `Add connections`).
4. **Get Data Source IDs**: 
   - Open your database in Notion.
   - Use the `GET /v1/databases/{database_id}` API call to find the `data_source_id`.
   - Alternatively, for most tables, the ID in the URL is the correct one to start with.

### 2. Google Gemini Setup
1. Get an API key from [Google AI Studio](https://aistudio.google.com/).
2. This is your `GEMINI_API_KEY`.

### 3. Telegram Setup (Detailed)

#### A. Create your Bot
1. Open Telegram and search for [@BotFather](https://t.me/botfather).
2. Send `/newbot`, give it a name and a username.
3. Copy the **API Token** (`TELEGRAM_BOT_TOKEN`).

#### B. Get the Chat ID
To send messages, the bot needs a `TELEGRAM_CHAT_ID`.
- **For Private Chat**: 
  1. Message your bot anything (e.g., "Hello").
  2. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`.
  3. Find the `"chat": {"id": 123456789}` block. The number is your ID.
- **For a Group**:
  1. Add the bot to the group and message it.
  2. Visit the `getUpdates` URL above and find the ID (it will start with a `-`, e.g., `-987654321`).
- **For a Channel (Easiest Method)**:
  1. Open [web.telegram.org](https://web.telegram.org) and click on your channel.
  2. Look at the URL. It will look like `https://web.telegram.org/k/#-100123456789`.
  3. The full ID is `-100123456789`.
  4. Ensure the bot is an **Administrator** in the channel.

### 4. GitHub Secrets
Add these in **Settings > Secrets and variables > Actions**:
- `NOTION_TOKEN`: Notion integration secret.
- `SOURCE_DATABASE_ID`: Data Source ID for raw notes.
- `TARGET_DATABASE_ID`: Data Source ID for generated guides.
- `GEMINI_API_KEY`: Google AI API key.
- `TELEGRAM_BOT_TOKEN`: Your Telegram Bot Token.
- `TELEGRAM_CHAT_ID`: Your Chat/Group/Channel ID.

---

## 🚀 Usage
- **Pacing**: The bot is now configured to process exactly **one page per run** (the oldest unprocessed note).
- **Daily Automations**: It runs automatically every day at 13:00 UTC.
- **Manual Trigger**: You can trigger it anytime via the **Actions** tab to process the "next" note in the queue.

## 🧑‍💻 Local Development
```bash
uv run bot.py
```
