import os
import re
import tempfile
from pathlib import Path

import requests
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from pdf_metadata import extract_pdf_metadata
from zotero_uploader import ZoteroUploader

load_dotenv()

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
TARGET_CHANNEL_NAME = "zotero-test"

PDF_URL_RE = re.compile(
    r"https?://[^\s>\"']+(?:"
    r"\.pdf"
    r"|arxiv\.org/pdf/[^\s>\"']+"
    r"|biorxiv\.org[^\s>\"']*\.pdf[^\s>\"']*"
    r")",
    re.IGNORECASE,
)

app = App(token=SLACK_BOT_TOKEN)
uploader = ZoteroUploader()


def _channel_name(client, channel_id: str) -> str:
    try:
        info = client.conversations_info(channel=channel_id)
        return info["channel"].get("name", "")
    except Exception:
        return ""


def _post_uploading(client, channel_id: str) -> str:
    resp = client.chat_postMessage(
        channel=channel_id,
        text=":hourglass_flowing_sand: Uploading PDF to Zotero...",
    )
    return resp["ts"]


def _update_success(client, channel_id: str, ts: str, title: str, url: str) -> None:
    client.chat_update(
        channel=channel_id,
        ts=ts,
        text=f":white_check_mark: *{title}* added to Zotero\n<{url}|View in Zotero>",
    )


def _update_failure(client, channel_id: str, ts: str, error: str) -> None:
    client.chat_update(
        channel=channel_id,
        ts=ts,
        text=f":x: Zotero upload failed: {error}",
    )


def process_pdf(
    client,
    channel_id: str,
    user_id: str,
    pdf_path: str,
    filename: str,
) -> None:
    ts = _post_uploading(client, channel_id)
    try:
        meta = extract_pdf_metadata(pdf_path, filename)
        parent_key, _ = uploader.upload(pdf_path, meta)
        web_url = uploader.item_web_url(parent_key)
        _update_success(client, channel_id, ts, meta["title"], web_url)
    except Exception as exc:
        _update_failure(client, channel_id, ts, str(exc))
    finally:
        try:
            Path(pdf_path).unlink(missing_ok=True)
        except Exception:
            pass


@app.event("file_shared")
def handle_file_shared(event: dict, client, logger) -> None:
    file_id = event.get("file_id") or (event.get("file") or {}).get("id")
    channel_id = event.get("channel_id")
    user_id = event.get("user_id", "")

    if not file_id or not channel_id:
        return

    if _channel_name(client, channel_id) != TARGET_CHANNEL_NAME:
        return

    try:
        info = client.files_info(file=file_id)
    except Exception as exc:
        logger.error(f"files_info failed: {exc}")
        return

    file_obj = info["file"]
    mimetype = file_obj.get("mimetype", "")
    name = file_obj.get("name", "file.pdf")

    if mimetype != "application/pdf" and not name.lower().endswith(".pdf"):
        return

    download_url = file_obj.get("url_private_download") or file_obj.get("url_private")
    if not download_url:
        logger.error("No download URL for file")
        return

    pdf_path = f"/tmp/{file_id}.pdf"
    try:
        r = requests.get(
            download_url,
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            timeout=60,
        )
        r.raise_for_status()
        with open(pdf_path, "wb") as f:
            f.write(r.content)
    except Exception as exc:
        logger.error(f"PDF download failed: {exc}")
        client.chat_postMessage(
            channel=channel_id,
            text=f":x: Could not download PDF from Slack: {exc}",
        )
        return

    process_pdf(client, channel_id, user_id, pdf_path, name)


@app.event("message")
def handle_message(event: dict, client, logger) -> None:
    # Skip bots and non-standard subtypes
    if event.get("bot_id") or event.get("subtype"):
        return

    channel_id = event.get("channel", "")
    user_id = event.get("user", "")
    text = event.get("text", "")

    if not text:
        return

    if _channel_name(client, channel_id) != TARGET_CHANNEL_NAME:
        return

    urls = PDF_URL_RE.findall(text)
    if not urls:
        return

    for url in urls:
        # Clean up Slack's angle-bracket link format if present
        url = url.strip("<>")
        filename = url.rstrip("/").split("/")[-1]
        if not filename.lower().endswith(".pdf"):
            filename = filename + ".pdf"

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            pdf_path = tmp.name

        try:
            r = requests.get(
                url,
                headers={"User-Agent": "Engram-ZoteroBot/1.0"},
                timeout=60,
                allow_redirects=True,
            )
            r.raise_for_status()
            with open(pdf_path, "wb") as f:
                f.write(r.content)
        except Exception as exc:
            logger.error(f"URL PDF download failed: {exc}")
            client.chat_postMessage(
                channel=channel_id,
                text=f":x: Could not download PDF from URL `{url}`: {exc}",
            )
            Path(pdf_path).unlink(missing_ok=True)
            continue

        process_pdf(client, channel_id, user_id, pdf_path, filename)


if __name__ == "__main__":
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
