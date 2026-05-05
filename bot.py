import os
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime

import dropbox
from dotenv import load_dotenv
from flask import Flask, request
from openai import OpenAI

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SAVE_PATH = os.getenv("SAVE_PATH", "/tmp")

DROPBOX_ACCESS_TOKEN = os.getenv("DROPBOX_ACCESS_TOKEN")
DROPBOX_TARGET_FOLDER = os.getenv("DROPBOX_TARGET_FOLDER", "/AlexBrain/00_Inbox")

PUBLIC_URL = os.getenv("PUBLIC_URL")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "telegram-webhook")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN 이 없습니다.")

if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY 가 없습니다.")

if not DROPBOX_ACCESS_TOKEN:
    raise ValueError("DROPBOX_ACCESS_TOKEN 이 없습니다.")

if not PUBLIC_URL:
    raise ValueError("PUBLIC_URL 이 없습니다. Render 서비스 URL을 넣어주세요.")

os.makedirs(SAVE_PATH, exist_ok=True)

client = OpenAI(api_key=OPENAI_API_KEY)
dbx = dropbox.Dropbox(DROPBOX_ACCESS_TOKEN)

app = Flask(__name__)


def telegram_api(method: str, payload: dict | None = None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"

    if payload is None:
        payload = {}

    data = urllib.parse.urlencode(payload).encode("utf-8")

    with urllib.request.urlopen(url, data=data, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def send_telegram_message(chat_id: int, text: str):
    return telegram_api(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
        },
    )


def set_telegram_webhook():
    webhook_url = f"{PUBLIC_URL.rstrip('/')}/{WEBHOOK_PATH}"

    result = telegram_api(
        "setWebhook",
        {
            "url": webhook_url,
            "drop_pending_updates": "true",
        },
    )

    print(f"Webhook set to: {webhook_url}")
    print(f"Telegram response: {result}")


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "", name)
    name = name.strip()
    return name[:80] if name else "untitled"


def normalize_tags(tags):
    if not isinstance(tags, list):
        return ["telegram", "inbox"]

    clean = []
    for tag in tags:
        if isinstance(tag, str):
            t = tag.strip().replace(" ", "_")
            if t:
                clean.append(t)

    return clean[:8] if clean else ["telegram", "inbox"]


def build_markdown(data: dict, original_text: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    title = data.get("title", "무제 메모").strip()
    summary = data.get("summary", "").strip()
    note_type = data.get("note_type", "inbox").strip()
    tags = normalize_tags(data.get("tags", []))
    key_points = data.get("key_points", [])
    action_items = data.get("action_items", [])

    key_points_md = "\n".join(
        f"- {item}" for item in key_points if isinstance(item, str) and item.strip()
    ) or "- 없음"

    action_items_md = "\n".join(
        f"- {item}" for item in action_items if isinstance(item, str) and item.strip()
    ) or "- 없음"

    tags_yaml = ", ".join(tags)

    md = f"""---
title: "{title}"
created: "{now}"
source: "telegram"
note_type: "{note_type}"
tags: [{tags_yaml}]
---

# {title}

## 한줄 요약
{summary or "요약 없음"}

## 핵심 내용
{key_points_md}

## 액션 아이템
{action_items_md}

## 원문
{original_text}
"""
    return md


def analyze_with_gpt(user_text: str) -> dict:
    prompt = f"""
너는 Obsidian용 메모를 구조화하는 비서다.
아래 텍스트를 분석해서 반드시 JSON 객체만 출력해라.
설명, 코드블록, 머리말 없이 JSON만 출력한다.

요구 형식:
{{
  "title": "짧고 명확한 한국어 제목",
  "summary": "1~2문장 요약",
  "note_type": "idea | work | journal | todo | inbox",
  "tags": ["태그1", "태그2"],
  "key_points": ["핵심1", "핵심2"],
  "action_items": ["실행1", "실행2"]
}}

규칙:
- title은 너무 길지 않게
- tags는 2~5개
- key_points는 1~5개
- action_items가 없으면 빈 배열 허용
- note_type은 반드시 idea, work, journal, todo, inbox 중 하나만
- 출력은 반드시 JSON 객체 하나만

입력 텍스트:
{user_text}
"""

    response = client.responses.create(
        model="gpt-5.4-mini",
        input=prompt,
    )

    text = response.output_text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {
            "title": "메모 정리 실패",
            "summary": "모델 응답을 JSON으로 해석하지 못했습니다.",
            "note_type": "inbox",
            "tags": ["telegram", "inbox"],
            "key_points": [user_text],
            "action_items": [],
        }


def upload_to_dropbox(filename: str, markdown: str) -> str:
    folder = DROPBOX_TARGET_FOLDER.rstrip("/")
    dropbox_path = f"{folder}/{filename}"

    dbx.files_upload(
        markdown.encode("utf-8"),
        dropbox_path,
        mode=dropbox.files.WriteMode.overwrite,
    )

    return dropbox_path


def process_text_message(chat_id: int, user_text: str):
    send_telegram_message(chat_id, "정리 중...")

    analyzed = analyze_with_gpt(user_text)
    title = analyzed.get("title", "무제 메모")

    filename = sanitize_filename(
        f"{datetime.now().strftime('%Y-%m-%d_%H%M%S')}_{title}"
    ) + ".md"

    markdown = build_markdown(analyzed, user_text)

    local_path = os.path.join(SAVE_PATH, filename)
    with open(local_path, "w", encoding="utf-8") as f:
        f.write(markdown)

    dropbox_path = upload_to_dropbox(filename, markdown)

    send_telegram_message(
        chat_id,
        f"저장 완료 📁\n제목: {title}\nDropbox 경로: {dropbox_path}",
    )


@app.route("/")
def home():
    return "Bot is running!"


@app.route(f"/{WEBHOOK_PATH}", methods=["POST"])
def webhook():
    update = request.get_json(silent=True)

    if not update:
        return "ok"

    message = update.get("message") or update.get("edited_message")

    if not message:
        return "ok"

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    user_text = message.get("text", "")

    if not chat_id:
        return "ok"

    if not user_text.strip():
        send_telegram_message(chat_id, "텍스트 메시지만 저장할 수 있어요.")
        return "ok"

    try:
        process_text_message(chat_id, user_text.strip())
    except Exception as e:
        print("에러 발생:", e)
        send_telegram_message(chat_id, f"에러 발생: {e}")

    return "ok"


if __name__ == "__main__":
    set_telegram_webhook()

    port = int(os.environ.get("PORT", 10000))
    print("Webhook bot 실행 중...")
    app.run(host="0.0.0.0", port=port)
