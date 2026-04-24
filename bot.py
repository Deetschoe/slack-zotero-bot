import os
import re
import tempfile
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from pdf_metadata import extract_pdf_metadata
from zotero_uploader import ZoteroUploader

claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))


def summarize_paper(meta: dict) -> str:
    title = meta.get("title", "")
    authors = meta.get("authors", "")
    abstract = meta.get("abstract", "")
    if not abstract and not title:
        return ""
    prompt = f"Title: {title}\nAuthors: {authors}\nAbstract: {abstract}\n\nWrite a 2-sentence summary of this paper relevant to a brain organoid / biotech startup. Be specific, not generic."
    try:
        msg = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception:
        return ""

load_dotenv()

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
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


@app.middleware
def log_all_events(logger, body, next):
    event = body.get("event", {})
    logger.info(f">>> INCOMING: type={event.get('type')} subtype={event.get('subtype')} channel={event.get('channel') or event.get('channel_id')}")
    return next()


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
        summary = summarize_paper(meta)
        if summary:
            client.chat_postMessage(
                channel=channel_id,
                text=f":brain: *Summary:* {summary}",
            )
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

    logger.info(f"file_shared event received: file_id={file_id} channel_id={channel_id}")

    if not file_id or not channel_id:
        logger.info("Skipping: missing file_id or channel_id")
        return

    logger.info(f"Processing file_shared in channel {channel_id}")

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
    if event.get("bot_id"):
        return

    subtype = event.get("subtype", "")
    channel_id = event.get("channel", "")
    user_id = event.get("user", "")

    logger.info(f"message event: subtype={subtype!r} channel={channel_id}")

    logger.info(f"Processing message in channel {channel_id}")

    # Handle direct file uploads (subtype=file_share)
    if subtype == "file_share":
        files = event.get("files", [])
        logger.info(f"file_share message with {len(files)} file(s)")
        for f in files:
            mimetype = f.get("mimetype", "")
            name = f.get("name", "file.pdf")
            if mimetype != "application/pdf" and not name.lower().endswith(".pdf"):
                logger.info(f"Skipping non-PDF file: {name} ({mimetype})")
                continue
            download_url = f.get("url_private_download") or f.get("url_private")
            if not download_url:
                continue
            pdf_path = f"/tmp/{f['id']}.pdf"
            try:
                r = requests.get(
                    download_url,
                    headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                    timeout=60,
                )
                r.raise_for_status()
                with open(pdf_path, "wb") as fh:
                    fh.write(r.content)
                logger.info(f"Downloaded file to {pdf_path}")
            except Exception as exc:
                logger.error(f"File download failed: {exc}")
                client.chat_postMessage(channel=channel_id, text=f":x: Could not download PDF: {exc}")
                continue
            process_pdf(client, channel_id, user_id, pdf_path, name)
        return

    # Skip other subtypes
    if subtype:
        return

    text = event.get("text", "")
    if not text:
        return

    urls = PDF_URL_RE.findall(text)
    if not urls:
        return

    for url in urls:
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
