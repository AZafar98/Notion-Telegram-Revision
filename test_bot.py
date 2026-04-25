import pytest
from bot import markdown_to_notion_blocks, NotionClient, StudyGuideModel, PollModel

def test_markdown_to_notion_blocks():
    md = "# Heading 1\n## Heading 2\n- Bullet\n1. Numbered\n**Bold text**"
    blocks = markdown_to_notion_blocks(md)
    
    assert len(blocks) == 5
    assert blocks[0]["type"] == "heading_1"
    assert blocks[0]["heading_1"]["rich_text"][0]["text"]["content"] == "Heading 1"
    assert blocks[1]["type"] == "heading_2"
    assert blocks[2]["type"] == "bulleted_list_item"
    assert blocks[3]["type"] == "numbered_list_item"
    assert blocks[4]["type"] == "paragraph"
    assert "Bold text" in blocks[4]["paragraph"]["rich_text"][0]["text"]["content"]

def test_study_guide_model_validation():
    data = {
        "summary": "Test summary",
        "polls": [
            {
                "question": "What is 1+1?",
                "options": ["1", "2", "3"],
                "correct_option_index": 1,
                "explanation": "Math."
            }
        ]
    }
    model = StudyGuideModel(**data)
    assert model.summary == "Test summary"
    assert len(model.polls) == 1
    assert model.polls[0].correct_option_index == 1

def test_notion_client_extract_title():
    client = NotionClient("fake-token")
    page = {
        "properties": {
            "Topic": {
                "type": "title",
                "title": [{"plain_text": "Expected Title"}]
            }
        }
    }
    assert client.extract_title(page) == "Expected Title"

def test_notion_client_extract_title_missing():
    client = NotionClient("fake-token")
    page = {"properties": {}}
    assert client.extract_title(page) == "Untitled"
