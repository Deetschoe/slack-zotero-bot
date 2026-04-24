import hashlib
import os
import time
from pathlib import Path
from typing import Literal

import requests
from pyzotero import zotero


ZOTERO_API_BASE = "https://api.zotero.org"


class ZoteroUploader:
    def __init__(self) -> None:
        self.library_id = os.environ["ZOTERO_LIBRARY_ID"]
        self.library_type = os.environ.get("ZOTERO_LIBRARY_TYPE", "group")
        self.api_key = os.environ["ZOTERO_API_KEY"]
        self.collection_key = os.environ.get("ZOTERO_COLLECTION_KEY", "")

        self.zot = zotero.Zotero(
            library_id=self.library_id,
            library_type=self.library_type,
            api_key=self.api_key,
        )

        self.session = requests.Session()
        self.session.headers.update({"Zotero-API-Key": self.api_key})

        lib_segment = (
            f"users/{self.library_id}"
            if self.library_type == "user"
            else f"groups/{self.library_id}"
        )
        self.api_prefix = f"{ZOTERO_API_BASE}/{lib_segment}"

    def _build_parent_item(self, meta: dict, category: str) -> dict:
        item_type = meta.get("item_type", "journalArticle")
        template = self.zot.item_template(item_type)

        template["title"] = meta.get("title", "Untitled")

        authors_raw = meta.get("authors", "")
        creators = []
        if authors_raw:
            for part in authors_raw.split(";"):
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
                    creators.append(
                        {"creatorType": "author", "name": part}
                    )
        template["creators"] = creators

        if meta.get("abstract"):
            template["abstractNote"] = meta["abstract"]
        if meta.get("year"):
            template["date"] = str(meta["year"])
        if meta.get("doi"):
            template["DOI"] = meta["doi"]
        if meta.get("source"):
            template["url"] = meta["source"]
        if category:
            template["extra"] = f"category: {category}"
        if self.collection_key:
            template["collections"] = [self.collection_key]

        return template

    def _md5(self, data: bytes) -> str:
        return hashlib.md5(data).hexdigest()

    def upload(self, pdf_path: str, meta: dict, category: str = "") -> tuple[str, str]:
        # Step 1: create parent item
        parent_template = self._build_parent_item(meta, category)
        resp = self.zot.create_items([parent_template])
        parent_key = resp["successful"]["0"]["key"]

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
        )
        att_resp.raise_for_status()
        att_data = att_resp.json()
        att_key = att_data["successful"]["0"]["key"]

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
        s3_resp = requests.post(s3_url, data=fields, files=files)
        if s3_resp.status_code not in (200, 201, 204):
            raise RuntimeError(
                f"S3 upload failed: {s3_resp.status_code} {s3_resp.text[:200]}"
            )

        # Step 8: register upload with Zotero
        reg_resp = self.session.post(
            f"{self.api_prefix}/items/{att_key}/file",
            json={"uploadKey": upload_key},
            headers={"Content-Type": "application/json"},
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
