import re
import urllib.request
import urllib.error
import json
from pathlib import Path

import PyPDF2


ARXIV_FILENAME_RE = re.compile(r"^(\d{4}\.\d{4,5})(v\d+)?\.pdf$", re.IGNORECASE)


def _fetch_arxiv_metadata(arxiv_id: str) -> dict:
    url = f"https://export.arxiv.org/abs/{arxiv_id}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Engram-ZoteroBot/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return {}

    meta: dict = {}

    title_m = re.search(
        r'<meta name="citation_title" content="([^"]+)"', html
    )
    if title_m:
        meta["title"] = title_m.group(1).strip()

    authors = re.findall(r'<meta name="citation_author" content="([^"]+)"', html)
    if authors:
        meta["authors"] = "; ".join(a.strip() for a in authors)

    abstract_m = re.search(
        r'<blockquote class="abstract mathjax"[^>]*>(.*?)</blockquote>',
        html,
        re.DOTALL,
    )
    if abstract_m:
        raw = abstract_m.group(1)
        raw = re.sub(r"<[^>]+>", "", raw)
        meta["abstract"] = raw.strip().removeprefix("Abstract:").strip()

    date_m = re.search(r'<meta name="citation_date" content="([^"]+)"', html)
    if date_m:
        year_m = re.search(r"\d{4}", date_m.group(1))
        if year_m:
            meta["year"] = int(year_m.group(0))

    doi_m = re.search(r'<meta name="citation_doi" content="([^"]+)"', html)
    if doi_m:
        meta["doi"] = doi_m.group(1).strip()

    meta["source"] = f"https://arxiv.org/abs/{arxiv_id}"
    meta["item_type"] = "preprint"
    return meta


def extract_pdf_metadata(pdf_path: str, filename: str) -> dict:
    meta: dict = {
        "title": Path(filename).stem,
        "authors": "",
        "abstract": "",
        "year": None,
        "source": "",
        "doi": "",
        "item_type": "journalArticle",
    }

    # Try PyPDF2
    try:
        reader = PyPDF2.PdfReader(pdf_path)
        info = reader.metadata or {}
        raw_title = info.get("/Title", "")
        raw_author = info.get("/Author", "")
        raw_subject = info.get("/Subject", "")

        if raw_title and raw_title.strip():
            meta["title"] = raw_title.strip()
        if raw_author and raw_author.strip():
            meta["authors"] = raw_author.strip()
        if raw_subject and raw_subject.strip() and not meta["abstract"]:
            meta["abstract"] = raw_subject.strip()
    except Exception:
        pass

    # Check for arXiv filename pattern (e.g. 2301.12345.pdf)
    arxiv_match = ARXIV_FILENAME_RE.match(filename)
    if arxiv_match:
        arxiv_id = arxiv_match.group(1)
        arxiv_meta = _fetch_arxiv_metadata(arxiv_id)
        if arxiv_meta:
            meta.update(arxiv_meta)

    return meta
