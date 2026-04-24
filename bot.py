import os
import sys
import logging
import requests
import re
from typing import Dict, Any, List, Optional
from google import genai
from google.genai import types
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# --- Configuration Constants ---
NOTION_API_VERSION = "2025-09-03"
NOTION_BASE_URL = "https://api.notion.com/v1"

# Notion Property Names
PROP_SOURCE_PROCESSED = "Processed by AI"
PROP_SOURCE_TITLE = "Name"

# Gemini Configuration
GEMINI_SYSTEM_INSTRUCTION = (
    "Act as a professional study assistant. Your goal is to transform the provided notes into a structured study guide. "
    "Use richly formatted Markdown. Include an executive summary, key concepts with definitions, "
    "and 5 active-recall questions at the end. Use headings (#, ##, ###), bold text, and bullet points. "
    "Do not use outside knowledge; only use the provided text."
)

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

class DualDatabaseBotError(Exception):
    """Custom exception for the Dual Database Bot."""
    pass

class Config:
    def __init__(self):
        self.notion_token = os.getenv("NOTION_TOKEN")
        self.source_id = os.getenv("SOURCE_DATABASE_ID") 
        self.target_id = os.getenv("TARGET_DATABASE_ID")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        # Robust handling for GEMINI_MODEL
        model_env = os.getenv("GEMINI_MODEL")
        self.gemini_model = model_env if model_env and model_env.strip() else "gemini-1.5-flash"

    def validate(self):
        missing = []
        if not self.notion_token: missing.append("NOTION_TOKEN")
        if not self.source_id: missing.append("SOURCE_DATABASE_ID")
        if not self.target_id: missing.append("TARGET_DATABASE_ID")
        if not self.gemini_api_key: missing.append("GEMINI_API_KEY")
        
        if missing:
            raise DualDatabaseBotError(f"Missing required environment variables: {', '.join(missing)}")

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
            if hasattr(e, 'response') and e.response:
                logger.error(f"Response: {e.response.text}")
            raise DualDatabaseBotError("Source Data Source query failed") from e

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
        title_prop = properties.get(PROP_SOURCE_TITLE, {})
        title_list = title_prop.get("title", [])
        if not title_list:
            return "Untitled"
        return "".join(t.get("plain_text", "") for t in title_list)

    def create_target_page(self, data_source_id: str, title: str, blocks: List[Dict[str, Any]]):
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
            logger.info(f"Successfully created target page: {title}")
        except Exception as e:
            logger.error(f"Failed to create target page: {e}")
            if hasattr(e, 'response') and e.response:
                logger.error(f"Response: {e.response.text}")
            raise DualDatabaseBotError("Target page creation failed") from e

    def mark_as_processed(self, page_id: str):
        url = f"{NOTION_BASE_URL}/pages/{page_id}"
        payload = {"properties": {PROP_SOURCE_PROCESSED: {"checkbox": True}}}
        try:
            response = requests.patch(url, headers=self.headers, json=payload, timeout=15)
            response.raise_for_status()
            logger.info(f"Marked source page {page_id} as processed.")
        except Exception as e:
            logger.error(f"Failed to mark source page as processed: {e}")

def markdown_to_notion_blocks(md_text: str) -> List[Dict[str, Any]]:
    """Converts a basic Markdown string into Notion block children."""
    blocks = []
    lines = md_text.split("\n")
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
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
    def generate_study_guide(self, text: str) -> str:
        if not text.strip():
            return "No content provided to summarize."
            
        try:
            logger.info(f"Requesting study guide from Gemini ({self.model_name})...")
            
            full_prompt = f"{GEMINI_SYSTEM_INSTRUCTION}\n\nNotes to process:\n\n{text}"
            
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=full_prompt
            )
            return response.text
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                logger.warning(f"Rate limit hit for {self.model_name}. Retrying with exponential backoff...")
                raise 
            logger.error(f"Gemini API error: {e}")
            raise DualDatabaseBotError("Gemini generation failed") from e

def main():
    try:
        config = Config()
        config.validate()
        
        notion = NotionClient(config.notion_token)
        gemini = GeminiClient(config.gemini_api_key, config.gemini_model)
        
        pages = notion.fetch_unprocessed_pages(config.source_id)
        if not pages:
            logger.info("No unprocessed notes found in Source Data Source.")
            return

        for page in pages:
            source_id = page.get("id")
            source_title = notion.extract_title(page)
            logger.info(f"Processing: {source_title}")
            
            content = notion.get_page_text_content(source_id)
            md_guide = gemini.generate_study_guide(content)
            blocks = markdown_to_notion_blocks(md_guide)
            
            target_title = f"Study Guide: {source_title}"
            notion.create_target_page(config.target_id, target_title, blocks)
            notion.mark_as_processed(source_id)
            
        logger.info("Batch processing complete.")
            
    except DualDatabaseBotError as e:
        logger.error(f"Automation Halted: {e}")
        sys.exit(1)
    except Exception as e:
        logger.exception(f"Unhandled Runtime Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
