import os
import sys
import logging
import requests
import re
import json
from typing import Dict, Any, List, Optional
from google import genai
from google.genai import types
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# --- Configuration Constants ---
NOTION_API_VERSION = "2025-09-03"
NOTION_BASE_URL = "https://api.notion.com/v1"
TELEGRAM_BASE_URL = "https://api.telegram.org"

# Notion Property Names
PROP_SOURCE_PROCESSED = "Processed by AI"
PROP_SOURCE_TITLE = "Topic"

# Gemini Configuration
GEMINI_SYSTEM_INSTRUCTION = """
   You are an elite Islamic Sciences tutor and synthesis engine. Your objective is to transform raw class notes into a mobile-friendly Telegram study session.

Read the provided notes and adhere to these strict constraints:
1. STRICT GROUNDING: Base your entire response ONLY on the provided text. Do not invent rulings, hallucinate hadith, or bring in external theological views.
2. NO LIMITS ON QUESTIONS: Generate as many challenging questions as necessary to comprehensively test all the core concepts found in the notes. Do not skip important material.

Execute the following two tasks:

TASK 1: TELEGRAM-FRIENDLY SUMMARY
Write a brief, highly digestible "Executive Brief" of the main concepts. Use short sentences, bullet points, strategic bolding, and emojis. It must be easy to read on a mobile phone screen.

TASK 2: CHALLENGING MULTIPLE-CHOICE QUIZ
Generate a comprehensive list of challenging multiple-choice questions (MCQs). These should NOT be basic rote-memorization questions. Create scenario-based questions (for Fiqh), cause-and-effect questions (for Seerah), or deep conceptual questions. 

OUTPUT FORMAT (STRICT JSON):
You are communicating with a Python script that will push this to the Telegram API. You MUST return a valid JSON object matching exactly this structure, with no markdown formatting outside the JSON:

{
  "summary": "Your Telegram-friendly Markdown summary here. Use \n for line breaks.",
  "polls": [
    {
      "question": "Challenging conceptual question text?",
      "options": [
        "Option A",
        "Option B",
        "Option C",
        "Option D"
      ],
      "correct_option_index": 2,
      "explanation": "Brief explanation of why the correct option is right and the others are wrong."
    }
  ]
}
   """

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

class StudyGuideBotError(Exception):
    """Custom exception for the Study Guide Bot."""
    pass

class Config:
    def __init__(self):
        self.notion_token = os.getenv("NOTION_TOKEN")
        self.source_id = os.getenv("SOURCE_DATABASE_ID") 
        self.target_id = os.getenv("TARGET_DATABASE_ID")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        
        model_env = os.getenv("GEMINI_MODEL")
        self.gemini_model = model_env if model_env and model_env.strip() else "gemini-1.5-flash"

    def validate(self):
        missing = []
        if not self.notion_token: missing.append("NOTION_TOKEN")
        if not self.source_id: missing.append("SOURCE_DATABASE_ID")
        if not self.target_id: missing.append("TARGET_DATABASE_ID")
        if not self.gemini_api_key: missing.append("GEMINI_API_KEY")
        
        if not self.telegram_bot_token or not self.telegram_chat_id:
            logger.warning("Telegram configuration missing. Notifications will be skipped.")
        
        if missing:
            raise StudyGuideBotError(f"Missing required environment variables: {', '.join(missing)}")

class NotionClient:
    def __init__(self, token: str):
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_API_VERSION,
            "Content-Type": "application/json"
        }

    def fetch_unprocessed_pages(self, data_source_id: str) -> List[Dict[str, Any]]:
        url = f"{NOTION_BASE_URL}/data_sources/{data_source_id}/query"
        payload = {
            "filter": {
                "property": PROP_SOURCE_PROCESSED,
                "checkbox": {"equals": False}
            }
        }
        try:
            response = requests.post(url, headers=self.headers, json=payload, timeout=15)
            response.raise_for_status()
            return response.json().get("results", [])
        except Exception as e:
            logger.error(f"Failed to query source data source: {e}")
            raise StudyGuideBotError("Source Data Source query failed") from e

    def get_page_text_content(self, page_id: str) -> str:
        url = f"{NOTION_BASE_URL}/blocks/{page_id}/children"
        text_parts = []
        has_more = True
        start_cursor = None

        while has_more:
            params = {"page_size": 100}
            if start_cursor:
                params["start_cursor"] = start_cursor
            
            response = requests.get(url, headers=self.headers, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            
            for block in data.get("results", []):
                block_type = block.get("type")
                if block_type in ["paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item", "numbered_list_item"]:
                    rich_text = block.get(block_type, {}).get("rich_text", [])
                    text_parts.append("".join(rt.get("plain_text", "") for rt in rich_text))
            
            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")
        
        return "\n".join(text_parts)

    def extract_title(self, page: Dict[str, Any]) -> str:
        properties = page.get("properties", {})
        title_prop = None
        for prop_name, prop_val in properties.items():
            if prop_val.get("type") == "title":
                title_prop = prop_val
                break
        
        if not title_prop:
            return "Untitled"
            
        title_list = title_prop.get("title", [])
        if not title_list:
            return "Untitled"
        return "".join(t.get("plain_text", "") for t in title_list)

    def create_target_page(self, data_source_id: str, title: str, blocks: List[Dict[str, Any]]) -> Optional[str]:
        url = f"{NOTION_BASE_URL}/pages"
        payload = {
            "parent": {"data_source_id": data_source_id},
            "properties": {
                "Name": {
                    "title": [{"text": {"content": title}}]
                }
            },
            "children": blocks[:100]
        }
        try:
            response = requests.post(url, headers=self.headers, json=payload, timeout=20)
            response.raise_for_status()
            data = response.json()
            logger.info(f"Successfully created target page: {title}")
            return data.get("url")
        except Exception as e:
            logger.error(f"Failed to create target page: {e}")
            return None

    def mark_as_processed(self, page_id: str):
        url = f"{NOTION_BASE_URL}/pages/{page_id}"
        payload = {"properties": {PROP_SOURCE_PROCESSED: {"checkbox": True}}}
        try:
            response = requests.patch(url, headers=self.headers, json=payload, timeout=15)
            response.raise_for_status()
            logger.info(f"Marked source page {page_id} as processed.")
        except Exception as e:
            logger.error(f"Failed to mark source page as processed: {e}")

class TelegramClient:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"{TELEGRAM_BASE_URL}/bot{self.bot_token}"

    def send_message(self, text: str):
        if not self.bot_token or not self.chat_id:
            return
            
        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown"
        }
        
        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            logger.info("Telegram message sent.")
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")

    def send_poll(self, poll_data: Dict[str, Any]):
        if not self.bot_token or not self.chat_id:
            return
            
        url = f"{self.base_url}/sendPoll"
        payload = {
            "chat_id": self.chat_id,
            "question": poll_data["question"],
            "options": poll_data["options"],
            "is_anonymous": False,
            "type": "quiz",
            "correct_option_id": poll_data["correct_option_index"],
            "explanation": poll_data.get("explanation", "")
        }
        
        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            logger.info("Telegram poll sent.")
        except Exception as e:
            logger.error(f"Failed to send Telegram poll: {e}")

def markdown_to_notion_blocks(md_text: str) -> List[Dict[str, Any]]:
    blocks = []
    lines = md_text.split("\n")
    for line in lines:
        line = line.strip()
        if not line: continue
        if line.startswith("### "):
            blocks.append({"object": "block", "type": "heading_3", "heading_3": {"rich_text": [{"text": {"content": line[4:]}}]}})
        elif line.startswith("## "):
            blocks.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"text": {"content": line[3:]}}]}})
        elif line.startswith("# "):
            blocks.append({"object": "block", "type": "heading_1", "heading_1": {"rich_text": [{"text": {"content": line[2:]}}]}})
        elif line.startswith("- ") or line.startswith("* "):
            blocks.append({"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"text": {"content": line[2:]}}]}})
        elif re.match(r"^\d+\.\s", line):
            content = re.sub(r"^\d+\.\s", "", line)
            blocks.append({"object": "block", "type": "numbered_list_item", "numbered_list_item": {"rich_text": [{"text": {"content": content}}]}})
        else:
            content = line.replace("**", "").replace("__", "")
            blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": content}}]}})
    return blocks

class GeminiClient:
    def __init__(self, api_key: str, model_name: str):
        self.client = genai.Client(api_key=api_key)
        self.model_name = model_name

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=10, max=60),
        retry=retry_if_exception_type(Exception),
        reraise=True
    )
    def generate_study_guide(self, text: str) -> Dict[str, Any]:
        if not text.strip(): return {"summary": "No content provided.", "polls": []}
        try:
            logger.info(f"Requesting study guide from Gemini ({self.model_name})...")
            full_prompt = f"{GEMINI_SYSTEM_INSTRUCTION}\n\nNotes to process:\n\n{text}"
            
            response = self.client.models.generate_content(
                model=self.model_name, 
                contents=full_prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json"
                )
            )
            return json.loads(response.text)
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                logger.warning(f"Rate limit hit. Retrying...")
                raise 
            logger.error(f"Gemini API error: {e}")
            raise StudyGuideBotError("Gemini generation failed") from e

def main():
    try:
        config = Config()
        config.validate()
        
        notion = NotionClient(config.notion_token)
        gemini = GeminiClient(config.gemini_api_key, config.gemini_model)
        telegram = TelegramClient(config.telegram_bot_token, config.telegram_chat_id)
        
        pages = notion.fetch_unprocessed_pages(config.source_id)
        if not pages:
            logger.info("No unprocessed notes found.")
            return

        pages.sort(key=lambda x: x.get("created_time", ""))
        page_to_process = pages[0]

        source_id = page_to_process.get("id")
        source_title = notion.extract_title(page_to_process)
        logger.info(f"Processing: {source_title}")

        content = notion.get_page_text_content(source_id)
        ai_data = gemini.generate_study_guide(content)
        
        summary = ai_data.get("summary", "")
        polls = ai_data.get("polls", [])

        # Write to Notion
        blocks = markdown_to_notion_blocks(summary)
        target_title = f"Study Guide: {source_title}"
        notion.create_target_page(config.target_id, target_title, blocks)

        # Push to Telegram
        telegram.send_message(f"📚 *Topic: {source_title}*\n\n{summary}")
        
        for poll in polls:
            telegram.send_poll(poll)

        # Mark as done
        notion.mark_as_processed(source_id)

        logger.info("Daily study guide and polls processing complete.")
            
    except Exception as e:
        logger.exception(f"Unhandled Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
