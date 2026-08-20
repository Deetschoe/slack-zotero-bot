import hashlib
import os
import time
from pathlib import Path

import requests


ZOTERO_API_BASE = "https://api.zotero.org"

# Item types that actually have a DOI field. Sending DOI on anything else
# makes the API reject the whole item.
DOI_ITEM_TYPES = {"journalArticle", "conferencePaper", "preprint"}


class ZoteroUploader:
    def __init__(self) -> None:
        self.library_id = os.environ["ZOTERO_LIBRARY_ID"]
        self.library_type = os.environ.get("ZOTERO_LIBRARY_TYPE", "group")
        self.api_key = os.environ["ZOTERO_API_KEY"]
        self.collection_key = os.environ.get("ZOTERO_COLLECTION_KEY", "")

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Zotero-API-Key": self.api_key,
                "Zotero-API-Version": "3",
            }
        )

        lib_segment = (
            f"users/{self.library_id}"
            if self.library_type == "user"
            else f"groups/{self.library_id}"
        )
        self.api_prefix = f"{ZOTERO_API_BASE}/{lib_segment}"

    def _parse_creators(self, authors_raw: str) -> list[dict]:
        creators = []
        for part in (authors_raw or "").split(";"):
            part = part.strip()
            if not part:
                continue
            if "," in part:
                last, _, first = part.partition(",")
                creators.append(
                    {
                        "creatorType": "author",
                        "firstName": first.strip(),
                        "lastName": last.strip(),
                    }
                )
            else:
                creators.append({"creatorType": "author", "name": part})
        return creators

    def _build_parent_item(self, meta: dict, category: str) -> dict:
        # Built locally rather than via GET /items/new. pyzotero caches that
        # template and, once the cache is over an hour old, revalidates it
        # without the itemType query param, which comes back as
        # "400 'itemType' not provided" and killed every upload until restart.
        item_type = meta.get("item_type") or "journalArticle"

        item: dict = {
            "itemType": item_type,
            "title": meta.get("title") or "Untitled",
            "creators": self._parse_creators(meta.get("authors", "")),
            "tags": [],
            "collections": [self.collection_key] if self.collection_key else [],
            "relations": {},
        }

        if meta.get("abstract"):
            item["abstractNote"] = meta["abstract"]
        if meta.get("year"):
            item["date"] = str(meta["year"])
        if meta.get("doi") and item_type in DOI_ITEM_TYPES:
            item["DOI"] = meta["doi"]
        if meta.get("source"):
            item["url"] = meta["source"]
        if category:
            item["extra"] = f"category: {category}"

        return item

    def _first_key(self, resp: requests.Response, what: str) -> str:
        data = resp.json()
        successful = data.get("successful", {})
        if "0" in successful:
            return successful["0"]["key"]
        failed = data.get("failed", {}).get("0", {})
        raise RuntimeError(
            f"Zotero rejected the {what}: "
            f"{failed.get('code', '?')} {failed.get('message', data)}"
        )

    def _md5(self, data: bytes) -> str:
        return hashlib.md5(data).hexdigest()

    def upload(self, pdf_path: str, meta: dict, category: str = "") -> tuple[str, str]:
        # Step 1: create parent item
        parent_resp = self.session.post(
            f"{self.api_prefix}/items",
            json=[self._build_parent_item(meta, category)],
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        parent_resp.raise_for_status()
        parent_key = self._first_key(parent_resp, "item")

        filename = Path(pdf_path).name
        pdf_bytes = Path(pdf_path).read_bytes()
        md5hash = self._md5(pdf_bytes)
        filesize = len(pdf_bytes)
        mtime_ms = int(time.time() * 1000)

        # Step 2: build attachment item
        att_template = {
            "itemType": "attachment",
            "linkMode": "imported_file",
            "title": filename,
            "contentType": "application/pdf",
            "parentItem": parent_key,
            "collections": [],
            "tags": [],
            "relations": {},
        }

        # Step 3: POST attachment item to Zotero
        att_resp = self.session.post(
            f"{self.api_prefix}/items",
            json=[att_template],
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        att_resp.raise_for_status()
        att_key = self._first_key(att_resp, "attachment")

        # Step 6: authorize file upload
        auth_resp = self.session.post(
            f"{self.api_prefix}/items/{att_key}/file",
            data={
                "md5": md5hash,
                "filename": filename,
                "filesize": filesize,
                "mtime": mtime_ms,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "If-None-Match": "*",
            },
            timeout=30,
        )
        auth_resp.raise_for_status()
        auth_data = auth_resp.json()

        if auth_data.get("exists") == 1:
            # File already on S3, skip upload
            return parent_key, att_key

        # Step 7: upload to S3
        s3_url = auth_data["url"]
        s3_params: dict = auth_data["params"]
        upload_key = auth_data["uploadKey"]

        fields = list(s3_params.items())
        files = [("file", (filename, pdf_bytes, "application/pdf"))]
        s3_resp = requests.post(s3_url, data=fields, files=files, timeout=120)
        if s3_resp.status_code not in (200, 201, 204):
            raise RuntimeError(
                f"S3 upload failed: {s3_resp.status_code} {s3_resp.text[:200]}"
            )

        # Step 8: register upload with Zotero
        reg_resp = self.session.post(
            f"{self.api_prefix}/items/{att_key}/file",
            data={"upload": upload_key},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "If-None-Match": "*",
            },
            timeout=30,
        )
        reg_resp.raise_for_status()

        return parent_key, att_key

    def item_web_url(self, parent_key: str) -> str:
        lib_segment = (
            f"{self.library_id}"
            if self.library_type == "user"
            else f"groups/{self.library_id}"
        )
        return f"https://www.zotero.org/{lib_segment}/items/{parent_key}"
