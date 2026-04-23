# Engram Zotero PDF Uploader

Slack bot for `#zotero-test`. Monitors the channel and automatically uploads PDFs to a Zotero library — either from a direct Slack file upload or from a PDF URL pasted into the channel.

## What It Does

**Scenario A — Drag a PDF into Slack**
User drags any `.pdf` file into `#zotero-test`. The bot detects the `file_shared` event, downloads the file from Slack (authenticated), extracts metadata from the PDF (title, author from PDF metadata, or filename as fallback), and uploads it to Zotero with a parent item (journalArticle or preprint) linked to the attachment.

**Scenario B — Paste a PDF URL**
User posts a message containing a URL ending in `.pdf`, an `arxiv.org/pdf/` link, or a bioRxiv PDF link. The bot downloads the PDF from the URL, extracts metadata (and fetches arXiv metadata automatically for arXiv IDs), then uploads to Zotero the same way.

Both scenarios post a confirmation message with a direct link to the Zotero item. Errors are posted to the channel so nothing silently fails.

## Zotero Upload Flow

1. Create a parent item (journalArticle or preprint) via Zotero API
2. Create an `imported_file` attachment item pointing to the parent
3. Authorize the file upload with Zotero (get S3 pre-signed URL)
4. Upload the PDF bytes to S3
5. Register the upload with Zotero to confirm

## Setup

### 1. Zotero

1. Go to [zotero.org/settings/keys](https://www.zotero.org/settings/keys) and create an API key with read/write access to your library.
2. Find your library ID:
   - Personal library: your numeric user ID from your Zotero profile URL
   - Group library: the group ID from `zotero.org/groups/<ID>`
3. Optionally create a collection and note its key (visible in the Zotero desktop app URL bar when the collection is selected).

### 2. Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and create a new app **from scratch**.
2. Under **OAuth & Permissions → Bot Token Scopes**, add:
   - `channels:history` — read messages in public channels
   - `channels:read` — look up channel names
   - `chat:write` — post messages
   - `files:read` — download uploaded files
3. Under **Event Subscriptions**, enable events and subscribe to bot events:
   - `file_shared`
   - `message.channels`
4. Under **Socket Mode**, enable Socket Mode and generate an **App-Level Token** (`xapp-...`) with the `connections:write` scope.
5. Install the app to your workspace. Copy the **Bot User OAuth Token** (`xoxb-...`).
6. **Invite the bot to `#zotero-test`**: `/invite @YourBotName`

### 3. Environment

```bash
cp .env.example .env
# Fill in all values in .env
```

### 4. Install & Run

```bash
pip install -r requirements.txt
python bot.py
```

The bot connects via Socket Mode (no public URL needed).

## Environment Variables

| Variable | Description |
|---|---|
| `SLACK_BOT_TOKEN` | Bot OAuth token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | App-level token for Socket Mode (`xapp-...`) |
| `ZOTERO_LIBRARY_ID` | Numeric Zotero library/group ID |
| `ZOTERO_LIBRARY_TYPE` | `group` or `user` |
| `ZOTERO_API_KEY` | Zotero API key with read/write access |
| `ZOTERO_COLLECTION_KEY` | (Optional) Collection key to file items into |

## Notes

- arXiv PDFs are detected by filename pattern (e.g. `2301.12345.pdf`) and full metadata is fetched from the arXiv API automatically.
- The bot only processes PDFs in `#zotero-test`; it ignores all other channels.
- If a PDF already exists in Zotero's S3 storage (same MD5), it skips the S3 upload and just registers the item.
