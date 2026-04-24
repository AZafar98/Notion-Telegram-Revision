# Notion Dual-Database Study Guide Generator

This bot automates the creation of study guides by reading raw notes from a **Source Database** and generating a polished, AI-summarized version in a **Target Database** using Google Gemini.

## 🏗 Architecture
1. **Source Database**: Where you keep your raw notes.
   - Requirement: A Checkbox property named `Processed by AI`.
   - Requirement: A Title property (default `Name`).
2. **Gemini AI**: Processes the raw text into a Markdown-formatted study guide.
3. **Target Database**: Where the bot creates a new page for each guide.
   - Requirement: A Title property (default `Name`).
4. **State Management**: Once processed, the source note is marked `Processed by AI = True` so it isn't processed again.

---

## 🛠 Setup Instructions

### 1. Notion Setup
1. **Create Integration**: Go to [Notion My Integrations](https://www.notion.so/my-integrations) and create a new integration. Save the **Internal Integration Secret** (`NOTION_TOKEN`).
2. **Databases**:
   - Create/Identify your **Source Database**. Add the `Processed by AI` checkbox.
   - Create/Identify your **Target Database**.
3. **Connections**: Share **both** databases with your new integration (`...` menu -> `Add connections`).
4. **IDs**: Copy the IDs for both databases from their URLs.

### 2. Google Gemini Setup
1. Get an API key from [Google AI Studio](https://aistudio.google.com/).
2. This is your `GEMINI_API_KEY`.

### 3. GitHub Secrets
Add the following secrets to your GitHub repository:
- `NOTION_TOKEN`: Your Notion integration secret.
- `SOURCE_DATABASE_ID`: The ID of the database containing raw notes.
- `TARGET_DATABASE_ID`: The ID of the database where guides should be created.
- `GEMINI_API_KEY`: Your Google AI API key.
- `GEMINI_MODEL`: (Optional) e.g., `gemini-1.5-flash` or `gemini-1.5-pro`.

---

## ⚙️ Configuration
You can customize the property names or AI instructions at the top of `bot.py`:

```python
PROP_SOURCE_PROCESSED = "Processed by AI"
PROP_SOURCE_TITLE = "Name"
GEMINI_SYSTEM_INSTRUCTION = "..." # Define your study guide style here
```

## 🚀 Usage
- **Automated**: Runs via GitHub Actions (check `.github/workflows/study_guide.yml`).
- **Manual**: Run `uv run bot.py` locally with the environment variables set.

## 🧑‍💻 Local Development
```bash
export NOTION_TOKEN="secret_..."
export SOURCE_DATABASE_ID="..."
export TARGET_DATABASE_ID="..."
export GEMINI_API_KEY="..."

uv run bot.py
```
