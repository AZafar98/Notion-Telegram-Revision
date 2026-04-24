# Notion Dual-Database Study Guide Generator (v2025-09-03)

This bot automates the creation of study guides using the **Notion API v2025-09-03**. It reads raw notes from a **Source Data Source** and generates summarized guides in a **Target Data Source** using Google Gemini.

## ⚠️ Important: Notion API 2025-09-03
This version of the Notion API introduces a major architectural change. You must now use **Data Source IDs** instead of Database IDs for most operations.

## 🏗 Architecture
1. **Source Data Source**: The specific data table containing your raw notes.
   - Requirement: A Checkbox property named `Processed by AI`.
   - Requirement: A Title property (default `Name`).
2. **Gemini AI**: Processes raw text into a Markdown-formatted study guide.
3. **Target Data Source**: The specific data table where the new guides are created.

---

## 🛠 Setup Instructions

### 1. Notion Setup (Finding your Data Source IDs)
1. **Create Integration**: Go to [Notion My Integrations](https://www.notion.so/my-integrations) and create a new integration. Save the **Internal Integration Secret** (`NOTION_TOKEN`).
2. **Databases**: Create your Source and Target databases in Notion.
3. **Connections**: Share both databases with your integration.
4. **Get Data Source IDs**: 
   - Since the `2025-09-03` version requires **Data Source IDs**, the easiest way to find them is to check the URL of your database view or use a `GET /v1/databases/{database_id}` call to see the `data_sources` array. 
   - For most native Notion tables, the Database ID and the primary Data Source ID are similar but distinct. Ensure you use the **Data Source ID**.

### 2. Google Gemini Setup
1. Get an API key from [Google AI Studio](https://aistudio.google.com/).
2. This is your `GEMINI_API_KEY`.

### 3. GitHub Secrets
Add these to your repository:
- `NOTION_TOKEN`: Your Notion integration secret.
- `SOURCE_DATABASE_ID`: The **Data Source ID** for your raw notes.
- `TARGET_DATABASE_ID`: The **Data Source ID** for your generated guides.
- `GEMINI_API_KEY`: Your Google AI API key.
- `GEMINI_MODEL`: (Optional) e.g., `gemini-1.5-flash`.

---

## ⚙️ Configuration
Customize properties or instructions in `bot.py`:

```python
PROP_SOURCE_PROCESSED = "Processed by AI"
PROP_SOURCE_TITLE = "Name"
GEMINI_SYSTEM_INSTRUCTION = "..."
```

## 🚀 Usage
- **Automated**: Runs via GitHub Actions.
- **Manual**: `uv run bot.py` locally.

## 🧑‍💻 Local Development
```bash
export NOTION_TOKEN="secret_..."
export SOURCE_DATABASE_ID="data_source_id_..."
export TARGET_DATABASE_ID="data_source_id_..."
export GEMINI_API_KEY="..."

uv run bot.py
```
