import os
import sys
import logging
import requests
import re
import json
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
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

# --- Models ---
class PollModel(BaseModel):
    question: str = Field(..., description="Challenging conceptual question text", max_length=300)
    options: List[str] = Field(..., description="List of 2-10 options", min_length=2, max_length=10)
    correct_option_index: int = Field(..., description="0-based index of the correct option")
    explanation: str = Field(..., description="Brief explanation of the correct answer", max_length=200)

class StudyGuideModel(BaseModel):
    summary: str = Field(..., description="Telegram-friendly Markdown summary")
    polls: List[PollModel] = Field(..., description="List of multiple-choice questions")

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

OUTPUT FORMAT:
You MUST return a valid JSON object matching the provided schema.
   """

# --- Logging Setup ---
class ContextFormatter(logging.Formatter):
    def format(self, record):
        if hasattr(record, "page_id"):
            record.msg = f"[{record.page_id} | {record.topic}] {record.msg}"
        return super().format(record)

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(ContextFormatter("%(asctime)s - %(levelname)s - %(message)s"))

logging.basicConfig(
    level=logging.INFO,
    handlers=[handler]
)
logger = logging.getLogger(__name__)

class StudyGuideBotError(Exception):
    """Custom exception for the Study Guide Bot."""
    pass

class Config:
    def __init__(self):
        self.notion_token = os.getenv("NOTION_TOKEN", "").strip()
        self.source_id = os.getenv("SOURCE_DATABASE_ID", "").strip() 
        self.target_id = os.getenv("TARGET_DATABASE_ID", "").strip()
        self.gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        
        model_env = os.getenv("GEMINI_MODEL")
        self.gemini_model = model_env if model_env and model_env.strip() else "gemini-1.5-flash"

        # Safe Debugging Logs
        if self.telegram_chat_id:
            logger.info(f"Telegram Chat ID detected: {self.telegram_chat_id[:4]}...{self.telegram_chat_id[-2:]} (Length: {len(self.telegram_chat_id)})")
        if self.telegram_bot_token:
            logger.info(f"Telegram Bot Token detected: {self.telegram_bot_token[:5]}... (Length: {len(self.telegram_bot_token)})")

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

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.exceptions.RequestException),
        reraise=True
    )
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
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.exceptions.RequestException),
        reraise=True
    )
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

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.exceptions.RequestException),
        reraise=True
    )
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

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.exceptions.RequestException),
        reraise=True
    )
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

    def _sanitize_html(self, text: str) -> str:
        """Basic conversion of Markdown-style bold/italic to HTML for Telegram."""
        # Escape HTML special characters
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        # Bold: **text** -> <b>text</b>
        text = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", text)
        # Italic: *text* -> <i>text</i>
        text = re.sub(r"\*(.*?)\*", r"<i>\1</i>", text)
        return text

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type(requests.exceptions.RequestException),
        reraise=True
    )
    def send_message(self, text: str):
        if not self.bot_token or not self.chat_id:
            return
            
        url = f"{self.base_url}/sendMessage"
        # Sanitize and convert to HTML
        html_text = self._sanitize_html(text)
        
        payload = {
            "chat_id": self.chat_id,
            "text": html_text,
            "parse_mode": "HTML"
        }
        
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 429:
            retry_after = int(response.json().get("parameters", {}).get("retry_after", 5))
            logger.warning(f"Telegram Rate Limit. Retrying after {retry_after}s")
            import time
            time.sleep(retry_after)
            raise requests.exceptions.RequestException("Rate limited")
            
        if response.status_code != 200:
            logger.error(f"Telegram Message Error: {response.text}")
        response.raise_for_status()
        logger.info("Telegram message sent.")

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type(requests.exceptions.RequestException),
        reraise=True
    )
    def send_poll(self, poll_data: Dict[str, Any]):
        if not self.bot_token or not self.chat_id:
            return
            
        url = f"{self.base_url}/sendPoll"
        
        # Telegram Poll Limits:
        # Question: 300 chars
        # Options: 100 chars each (max 10 options)
        # Explanation: 200 chars
        
        question = poll_data.get("question", "Quiz Question")[:300]
        options = [str(opt)[:100] for opt in poll_data.get("options", [])[:10]]
        explanation = poll_data.get("explanation", "")[:200]
        correct_index = poll_data.get("correct_option_index", 0)
        
        # Ensure correct_index is within range
        if not (0 <= correct_index < len(options)):
            correct_index = 0

        payload = {
            "chat_id": self.chat_id,
            "question": question,
            "options": options,
            "is_anonymous": True,
            "type": "quiz",
            "correct_option_id": correct_index,
            "explanation": explanation
        }
        
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 429:
            retry_after = int(response.json().get("parameters", {}).get("retry_after", 5))
            logger.warning(f"Telegram Rate Limit. Retrying after {retry_after}s")
            import time
            time.sleep(retry_after)
            raise requests.exceptions.RequestException("Rate limited")

        if response.status_code != 200:
            logger.error(f"Telegram Poll Error: {response.text}")
        response.raise_for_status()
        logger.info("Telegram poll sent.")

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
    def generate_study_guide(self, text: str) -> StudyGuideModel:
        if not text.strip(): 
            return StudyGuideModel(summary="No content provided.", polls=[])
            
        try:
            logger.info(f"Requesting study guide from Gemini ({self.model_name})...")
            # Using Delimiters for Security (Pillar 4 preview)
            full_prompt = f"{GEMINI_SYSTEM_INSTRUCTION}\n\nNotes to process:\n[[[CONTENT]]]\n{text}\n[[[/CONTENT]]]"
            
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=full_prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=StudyGuideModel,
                )
            )
            return response.parsed
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
        
        # Structured Logging Context
        ctx_logger = logging.LoggerAdapter(logger, {"page_id": source_id, "topic": source_title})
        ctx_logger.info(f"Processing study guide")

        content = notion.get_page_text_content(source_id)
        ai_data = gemini.generate_study_guide(content)
        
        summary = ai_data.summary
        polls = ai_data.polls

        # Write to Notion
        blocks = markdown_to_notion_blocks(summary)
        target_title = f"Study Guide: {source_title}"
        notion.create_target_page(config.target_id, target_title, blocks)

        # Push to Telegram
        telegram.send_message(f"📚 *Topic: {source_title}*\n\n{summary}")
        
        for i, poll in enumerate(polls):
            ctx_logger.info(f"Sending poll {i+1}/{len(polls)}")
            telegram.send_poll(poll.model_dump())

        # Mark as done
        notion.mark_as_processed(source_id)

        ctx_logger.info("Daily study guide and polls processing complete.")
            
    except Exception as e:
        logger.exception(f"Unhandled Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
