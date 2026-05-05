import os
import json
import re
import threading
from datetime import datetime

import dropbox
from dotenv import load_dotenv
from flask import Flask
from openai import OpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SAVE_PATH = os.getenv("SAVE_PATH", "/tmp")

DROPBOX_ACCESS_TOKEN = os.getenv("DROPBOX_ACCESS_TOKEN")
DROPBOX_TARGET_FOLDER = os.getenv("DROPBOX_TARGET_FOLDER", "/AlexBrain/00_Inbox")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN 이 없습니다.")

if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY 가 없습니다.")

if not DROPBOX_ACCESS_TOKEN:
    raise ValueError("DROPBOX_ACCESS_TOKEN 이 없습니다.")

os.makedirs(SAVE_PATH, exist_ok=True)

client = OpenAI(api_key=OPENAI_API_KEY)
dbx = dropbox.Dropbox(DROPBOX_ACCESS_TOKEN)

# Render Web Service용 작은 웹서버
app_flask = Flask(__name__)

@app_flask.route("/")
def home():
    return "Bot is running!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port)


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
        input=prompt
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
            "action_items": []
        }


def upload_to_dropbox(filename: str, markdown: str) -> str:
    folder = DROPBOX_TARGET_FOLDER.rstrip("/")
    dropbox_path = f"{folder}/{filename}"

    dbx.files_upload(
        markdown.encode("utf-8"),
        dropbox_path,
        mode=dropbox.files.WriteMode.overwrite
    )

    return dropbox_path


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_text = update.message.text.strip()

    if not user_text:
        await update.message.reply_text("빈 메시지는 저장할 수 없어요.")
        return

    await update.message.reply_text("정리 중...")

    try:
        analyzed = analyze_with_gpt(user_text)
        title = analyzed.get("title", "무제 메모")

        filename = sanitize_filename(
            f"{datetime.now().strftime('%Y-%m-%d_%H%M%S')}_{title}"
        ) + ".md"

        markdown = build_markdown(analyzed, user_text)

        # Render 서버 임시 저장도 하고
        local_path = os.path.join(SAVE_PATH, filename)
        with open(local_path, "w", encoding="utf-8") as f:
            f.write(markdown)

        # Dropbox에도 업로드
        dropbox_path = upload_to_dropbox(filename, markdown)

        await update.message.reply_text(
            f"저장 완료 📁\n제목: {title}\nDropbox 경로: {dropbox_path}"
        )

    except Exception as e:
        print("에러 발생:", e)
        await update.message.reply_text(f"에러 발생: {e}")


def main():
    threading.Thread(target=run_web, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("봇 실행 중...")
    app.run_polling()


if __name__ == "__main__":
    main()
