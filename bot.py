import os
import sys
import time
import logging
import requests
import re
from datetime import datetime
from typing import Any, Optional
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

# --- Models ---
class PollModel(BaseModel):
    question: str = Field(..., description="Challenging conceptual question text", max_length=300)
    options: list[str] = Field(..., description="List of 2-10 options", min_length=2, max_length=10)
    correct_option_index: int = Field(..., description="0-based index of the correct option")
    explanation: str = Field(..., description="Brief explanation of the correct answer", max_length=200)

class StudyGuideModel(BaseModel):
    summary: str = Field(..., description="Telegram-friendly Markdown summary")
    polls: list[PollModel] = Field(..., description="List of multiple-choice questions")

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
Generate a comprehensive list of challenging multiple-choice questions (MCQs). These should NOT be basic rote-memorization questions, though you may include some to test for basic knowledge retention. Test all aspects, as well as deep conceptual questions. 

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

        if self.telegram_chat_id:
            logger.debug("Telegram Chat ID detected: %s...%s (Length: %d)", self.telegram_chat_id[:4], self.telegram_chat_id[-2:], len(self.telegram_chat_id))
        if self.telegram_bot_token:
            logger.debug("Telegram Bot Token detected: %s... (Length: %d)", self.telegram_bot_token[:5], len(self.telegram_bot_token))

    def validate(self):
        missing = []
        if not self.notion_token:
            missing.append("NOTION_TOKEN")
        if not self.source_id:
            missing.append("SOURCE_DATABASE_ID")
        if not self.target_id:
            missing.append("TARGET_DATABASE_ID")
        if not self.gemini_api_key:
            missing.append("GEMINI_API_KEY")
        
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
    def fetch_unprocessed_pages(self, data_source_id: str) -> list[dict[str, Any]]:
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
            logger.error("Failed to query source data source: %s", e)
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

    def extract_title(self, page: dict[str, Any]) -> str:
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
    def create_target_page(self, data_source_id: str, title: str, blocks: list[dict[str, Any]], url: Optional[str] = None) -> str:
        url_endpoint = f"{NOTION_BASE_URL}/pages"
        properties = {
            "Name": {
                "title": [{"text": {"content": title}}]
            }
        }
        if url:
            properties["Telegram Link"] = {"url": url}
            
        if len(blocks) > 100:
            logger.warning("Block count %d exceeds API limit; truncating to 100.", len(blocks))
        payload = {
            "parent": {"data_source_id": data_source_id},
            "properties": properties,
            "children": blocks[:100],
        }
        response = requests.post(url_endpoint, headers=self.headers, json=payload, timeout=20)
        response.raise_for_status()
        data = response.json()
        logger.info("Successfully created target page: %s", title)
        return data.get("url", "")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.exceptions.RequestException),
        reraise=True
    )
    def fetch_all_target_pages(self, data_source_id: str) -> list[dict[str, Any]]:
        url = f"{NOTION_BASE_URL}/data_sources/{data_source_id}/query"
        pages = []
        has_more = True
        start_cursor = None
        
        while has_more:
            payload = {}
            if start_cursor:
                payload["start_cursor"] = start_cursor
            
            response = requests.post(url, headers=self.headers, json=payload, timeout=15)
            response.raise_for_status()
            data = response.json()
            pages.extend(data.get("results", []))
            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")
        return pages

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
            logger.info("Marked source page %s as processed.", page_id)
        except Exception as e:
            logger.error("Failed to mark source page as processed: %s", e)

class TelegramClient:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"{TELEGRAM_BASE_URL}/bot{self.bot_token}"

    def get_message_link(self, message_id: int) -> str:
        # For channels: https://t.me/c/123456789/1
        # Strip -100 prefix if present
        clean_id = str(self.chat_id).replace("-100", "")
        return f"https://t.me/c/{clean_id}/{message_id}"

    def markdown_to_html(self, text: str) -> str:
        """Convert Markdown-style bold, italic, and links to Telegram-compatible HTML."""
        # 1. Escape existing HTML special characters
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        
        # 2. Convert Bold: **text** -> <b>text</b>
        text = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", text)
        
        # 3. Convert Italic: *text* -> <i>text</i> (supporting both * and _)
        text = re.sub(r"\*(.*?)\*", r"<i>\1</i>", text)
        
        # 4. Convert Links: [text](url) -> <a href="url">text</a>
        # This regex avoids capturing across multiple lines and ensures it finds valid pairs
        text = re.sub(r"\[(.*?)\]\((.*?)\)", r'<a href="\2">\1</a>', text)
        
        return text

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type(requests.exceptions.RequestException),
        reraise=True
    )
    def send_message(self, text: str) -> Optional[int]:
        if not self.bot_token or not self.chat_id:
            return None
            
        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": self.markdown_to_html(text),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 429:
            retry_after = int(response.json().get("parameters", {}).get("retry_after", 5))
            logger.warning("Telegram rate limit hit; retrying after %ds.", retry_after)
            time.sleep(retry_after)
            raise requests.exceptions.RequestException("Rate limited")

        response.raise_for_status()
        logger.info("Telegram message sent.")
        return response.json().get("result", {}).get("message_id")

    def get_pinned_message_id(self) -> Optional[int]:
        url = f"{self.base_url}/getChat"
        payload = {"chat_id": self.chat_id}
        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            pinned = response.json().get("result", {}).get("pinned_message")
            return pinned.get("message_id") if pinned else None
        except Exception as e:
            logger.error("Failed to get pinned message: %s", e)
            return None

    def edit_message(self, message_id: int, text: str):
        url = f"{self.base_url}/editMessageText"
        payload = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": self.markdown_to_html(text),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        requests.post(url, json=payload, timeout=10).raise_for_status()

    def pin_message(self, message_id: int):
        url = f"{self.base_url}/pinChatMessage"
        payload = {"chat_id": self.chat_id, "message_id": message_id, "disable_notification": True}
        requests.post(url, json=payload, timeout=10).raise_for_status()

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type(requests.exceptions.RequestException),
        reraise=True
    )
    def send_poll(self, poll: PollModel):
        if not self.bot_token or not self.chat_id:
            return

        url = f"{self.base_url}/sendPoll"

        # Pydantic already enforces max_length on question/options/explanation,
        # but Telegram's char limits are enforced here defensively at the wire level.
        options = [opt[:100] for opt in poll.options[:10]]
        correct_index = poll.correct_option_index if 0 <= poll.correct_option_index < len(options) else 0

        payload = {
            "chat_id": self.chat_id,
            "question": poll.question[:300],
            "options": options,
            "is_anonymous": True,
            "type": "quiz",
            "correct_option_id": correct_index,
            "explanation": poll.explanation[:200],
        }

        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 429:
            retry_after = int(response.json().get("parameters", {}).get("retry_after", 5))
            logger.warning("Telegram rate limit hit; retrying after %ds.", retry_after)
            time.sleep(retry_after)
            raise requests.exceptions.RequestException("Rate limited")

        if response.status_code != 200:
            logger.error("Telegram poll error: %s", response.text)
        response.raise_for_status()
        logger.info("Telegram poll sent.")

def markdown_to_notion_blocks(md_text: str) -> list[dict[str, Any]]:
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
        retry=retry_if_exception_type(requests.exceptions.RequestException),
        reraise=True
    )
    def generate_study_guide(self, text: str) -> StudyGuideModel:
        if not text.strip():
            return StudyGuideModel(summary="No content provided.", polls=[])

        try:
            logger.info("Requesting study guide from Gemini (%s)...", self.model_name)
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=f"Notes to process:\n[[[CONTENT]]]\n{text}\n[[[/CONTENT]]]",
                config=types.GenerateContentConfig(
                    system_instruction=GEMINI_SYSTEM_INSTRUCTION,
                    response_mime_type="application/json",
                    response_schema=StudyGuideModel,
                ),
            )
            return response.parsed
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                logger.warning("Gemini rate limit hit; retrying...")
                raise requests.exceptions.RequestException("Rate limited") from e
            logger.error("Gemini API error: %s", e)
            raise StudyGuideBotError("Gemini generation failed") from e

def _send_or_replace_pinned(telegram: TelegramClient, index_text: str, ctx_logger: logging.LoggerAdapter):
    """Edit the existing pinned message or send and pin a new one."""
    pinned_id = telegram.get_pinned_message_id()
    if pinned_id:
        try:
            telegram.edit_message(pinned_id, index_text)
            ctx_logger.info("Updated existing pinned index.")
            return
        except Exception as exc:
            ctx_logger.warning("Could not edit pinned message (%s); sending a new one.", exc)

    new_pinned = telegram.send_message(index_text)
    if new_pinned:
        telegram.pin_message(new_pinned)
        ctx_logger.info("Sent and pinned new index.")


def update_pinned_index(
    notion: NotionClient,
    telegram: TelegramClient,
    target_id: str,
    ctx_logger: logging.LoggerAdapter,
):
    """Rebuild and push the master study index to the pinned Telegram message."""
    ctx_logger.info("Updating pinned index...")
    all_pages = notion.fetch_all_target_pages(target_id)
    all_pages.sort(key=lambda x: x.get("created_time", ""), reverse=True)

    index_entries = []
    for p in all_pages[:50]:
        p_title = notion.extract_title(p)
        p_url = p.get("properties", {}).get("Telegram Link", {}).get("url")
        if p_url:
            clean_title = p_title.replace("Study Guide: ", "")
            index_entries.append(f"• [{clean_title}]({p_url})")

    if not index_entries:
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    archive_url = f"https://www.notion.so/{target_id.replace('-', '')}"
    index_text = (
        f"📚 **Master Study Index**\n"
        f"_Last Updated: {now_str}_\n\n"
        + "\n".join(index_entries)
        + f"\n\n[➜ View Full Archive in Notion]({archive_url})"
    )
    _send_or_replace_pinned(telegram, index_text, ctx_logger)


def process_page(
    notion: NotionClient,
    gemini: GeminiClient,
    telegram: TelegramClient,
    page: dict[str, Any],
    target_id: str,
):
    """Generate and publish a study guide for a single Notion page."""
    source_id = page.get("id")
    source_title = notion.extract_title(page)
    ctx_logger = logging.LoggerAdapter(logger, {"page_id": source_id, "topic": source_title})
    ctx_logger.info("Processing study guide.")

    content = notion.get_page_text_content(source_id)
    ai_data = gemini.generate_study_guide(content)

    msg_id = telegram.send_message(f"📚 **Topic: {source_title}**\n\n{ai_data.summary}")

    blocks = markdown_to_notion_blocks(ai_data.summary)
    target_title = f"Study Guide: {source_title}"
    msg_link = telegram.get_message_link(msg_id) if msg_id else None
    notion.create_target_page(target_id, target_title, blocks, url=msg_link)

    for i, poll in enumerate(ai_data.polls):
        ctx_logger.info("Sending poll %d/%d.", i + 1, len(ai_data.polls))
        telegram.send_poll(poll)

    return source_id, ctx_logger


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
        source_id, ctx_logger = process_page(notion, gemini, telegram, pages[0], config.target_id)

        try:
            update_pinned_index(notion, telegram, config.target_id, ctx_logger)
        except Exception as e:
            ctx_logger.error("Failed to update pinned index: %s", e)

        notion.mark_as_processed(source_id)
        ctx_logger.info("Processing complete.")

    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
