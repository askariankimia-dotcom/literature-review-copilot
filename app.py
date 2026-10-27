import streamlit as st
from pypdf import PdfReader
import sqlite3
from datetime import datetime
import os
import re
import json
import urllib.request
import urllib.parse
import urllib.error
import ssl
import certifi
from difflib import SequenceMatcher

from dotenv import load_dotenv
from anthropic import Anthropic
from openai import OpenAI


# =========================================================
# API SETUP
# =========================================================

load_dotenv()

anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
openai_api_key = os.getenv("OPENAI_API_KEY")
zotero_user_id = os.getenv("ZOTERO_USER_ID")
zotero_api_key = os.getenv("ZOTERO_API_KEY")

claude = Anthropic(api_key=anthropic_api_key) if anthropic_api_key else None
openai_client = OpenAI(api_key=openai_api_key) if openai_api_key else None


# =========================================================
# PAGE
# =========================================================

st.title("Literature Review Copilot")

# App design note:
# Clean literature library · Article / Book / Thesis · Zotero approval before write


# =========================================================
# CLEAN-SLATE APP / DATABASE
# =========================================================

APP_VERSION = "clean_slate_2026_10_19_v1"
DATABASE_PATH = "literature.db"
RESET_MARKER_PATH = ".doctoral_literature_clean_start_v1"

# Clear old Streamlit session memory once when this clean app version starts.
if st.session_state.get("_doctoral_agent_version") != APP_VERSION:
    for session_key in list(
        st.session_state.keys()
    ):
        del st.session_state[
            session_key
        ]

    st.session_state[
        "_doctoral_agent_version"
    ] = APP_VERSION


# On the first launch of this clean-slate version, move any old local database
# out of the way rather than destroying it permanently. The app then starts
# with a brand-new empty literature.db.
if not os.path.exists(
    RESET_MARKER_PATH
):
    if os.path.exists(
        DATABASE_PATH
    ):
        backup_timestamp = datetime.now().strftime(
            "%Y%m%d_%H%M%S"
        )

        backup_path = (
            "literature_backup_before_clean_start_"
            + backup_timestamp
            + ".db"
        )

        os.replace(
            DATABASE_PATH,
            backup_path
        )

    with open(
        RESET_MARKER_PATH,
        "w",
        encoding="utf-8"
    ) as marker_file:
        marker_file.write(
            APP_VERSION
        )


conn = sqlite3.connect(
    DATABASE_PATH
)

cursor = conn.cursor()


# =========================================================
# FINAL DATABASE SCHEMA
# =========================================================

cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS papers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,

        filename TEXT NOT NULL,
        research_question TEXT,
        full_text TEXT NOT NULL,
        date_added TEXT NOT NULL,

        folder TEXT NOT NULL DEFAULT 'Unsorted',
        source_type TEXT NOT NULL DEFAULT 'Article',

        claude_quick_analysis TEXT,
        claude_quick_date TEXT,
        claude_deep_analysis TEXT,
        claude_deep_date TEXT,

        openai_quick_analysis TEXT,
        openai_quick_date TEXT,
        openai_deep_analysis TEXT,
        openai_deep_date TEXT,

        comparison_quick TEXT,
        comparison_quick_date TEXT,
        comparison_deep TEXT,
        comparison_deep_date TEXT,

        free_extraction TEXT,
        free_extraction_date TEXT,

        zotero_item_key TEXT,
        zotero_collection_key TEXT,
        zotero_metadata_json TEXT,
        zotero_linked_at TEXT,

        zotero_draft_json TEXT,
        zotero_draft_date TEXT
    )
    """
)


cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS folders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL
    )
    """
)


cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS library_answers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question TEXT,
        provider TEXT,
        answer TEXT,
        evidence TEXT,
        created_at TEXT,
        scope TEXT DEFAULT 'Entire Library',
        paper_id INTEGER,
        source_label TEXT
    )
    """
)


# Start with only the two useful top-level folders.
cursor.execute(
    """
    INSERT OR IGNORE INTO folders (name)
    VALUES ('Unsorted')
    """
)

cursor.execute(
    """
    INSERT OR IGNORE INTO folders (name)
    VALUES ('Communicative Competence')
    """
)

# Upgrade older databases without resetting the user's library.
cursor.execute(
    "PRAGMA table_info(library_answers)"
)

library_answer_columns = {
    row[1]
    for row in cursor.fetchall()
}

for column_name, column_definition in [
    ("scope", "TEXT DEFAULT 'Entire Library'"),
    ("paper_id", "INTEGER"),
    ("source_label", "TEXT"),
    ("mode", "TEXT DEFAULT 'quick'"),
]:
    if column_name not in library_answer_columns:
        cursor.execute(
            f"ALTER TABLE library_answers "
            f"ADD COLUMN {column_name} {column_definition}"
        )


# This app no longer stores a literature-review question with individual papers.
# Clear any legacy values left from older versions so paper analyses stay question-neutral.
cursor.execute(
    """
    UPDATE papers
    SET research_question = NULL
    WHERE research_question IS NOT NULL
    """
)

conn.commit()


# =========================================================
# FREE PYTHON ZOTERO METADATA EXTRACTION
# ARTICLE / BOOK / THESIS HAVE DIFFERENT RULES
# =========================================================

def zotero_metadata_source_excerpt(
    paper_text,
    max_chars=24000
):
    """
    Bibliographic metadata is normally concentrated near the beginning
    of an academic source. This is local Python processing only.
    """
    if not paper_text:
        return ""

    return paper_text[:max_chars]


def normalize_zotero_draft(
    draft,
    source_type
):
    """
    Keep one internal structure, while the UI and Zotero writer use only the
    fields valid/relevant for the chosen source type.
    """
    allowed = {
        # Common
        "title": "",
        "authors": [],
        "year": "",
        "doi": "",
        "url": "",

        # Journal article
        "journal": "",
        "volume": "",
        "issue": "",
        "pages": "",

        # Book
        "publisher": "",
        "place": "",
        "edition": "",
        "isbn": "",
        "num_pages": "",

        # Thesis
        "university": "",
        "thesis_type": "",

        # Provenance / caution
        "notes": "",
    }

    result = allowed.copy()

    if isinstance(
        draft,
        dict
    ):
        for key in result:
            value = draft.get(
                key,
                result[key]
            )

            if key == "authors":
                if isinstance(
                    value,
                    list
                ):
                    result[key] = [
                        str(item).strip()
                        for item in value
                        if str(item).strip()
                    ]
                elif value:
                    result[key] = [
                        str(value).strip()
                    ]
            else:
                result[key] = (
                    str(value).strip()
                    if value is not None
                    else ""
                )

    result["doi"] = normalize_doi(
        result["doi"]
    )

    result["isbn"] = re.sub(
        r"[^0-9Xx]",
        "",
        result["isbn"]
    )

    result["source_type"] = (
        source_type
        or "Article"
    )

    return result


def save_zotero_draft(
    paper_id,
    draft
):
    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    cursor.execute(
        """
        UPDATE papers
        SET
            zotero_draft_json = ?,
            zotero_draft_date = ?
        WHERE id = ?
        """,
        (
            json.dumps(
                draft,
                ensure_ascii=False
            ),
            now,
            paper_id
        )
    )

    conn.commit()


def authors_list_to_text(
    authors
):
    return "\n".join(
        author
        for author in (
            authors
            or []
        )
        if author
    )


def crossref_request_json(
    url
):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Doctoral-Literature-Agent/1.0 "
                "(bibliographic metadata lookup)"
            ),
            "Accept": "application/json",
        }
    )

    with urllib.request.urlopen(
        request,
        timeout=20,
        context=ZOTERO_SSL_CONTEXT
    ) as response:
        return json.loads(
            response.read().decode(
                "utf-8"
            )
        )


def first_crossref_value(
    value
):
    if isinstance(
        value,
        list
    ):
        return (
            str(value[0]).strip()
            if value
            else ""
        )

    return (
        str(value).strip()
        if value
        else ""
    )


def crossref_year_from_work(
    work
):
    for field in [
        "published-print",
        "published-online",
        "published",
        "issued",
        "created",
    ]:
        value = work.get(
            field
        )

        if not isinstance(
            value,
            dict
        ):
            continue

        parts = value.get(
            "date-parts",
            []
        )

        if (
            parts
            and parts[0]
            and parts[0][0]
        ):
            return str(
                parts[0][0]
            )

    return ""


def crossref_authors_from_work(
    work
):
    names = []

    for author in work.get(
        "author",
        []
    ):
        given = (
            author.get(
                "given",
                ""
            )
            or ""
        ).strip()

        family = (
            author.get(
                "family",
                ""
            )
            or ""
        ).strip()

        if given and family:
            names.append(
                f"{given} {family}"
            )
        elif family:
            names.append(
                family
            )
        elif given:
            names.append(
                given
            )

    return names


def crossref_type_is_compatible(
    work,
    source_type
):
    crossref_type = (
        work.get(
            "type",
            ""
        )
        or ""
    ).strip().lower()

    compatible_types = {
        "Article": {
            "journal-article",
        },
        "Book": {
            "book",
            "monograph",
            "reference-book",
            "book-set",
            "book-series",
        },
        "Thesis": {
            "dissertation",
        },
    }

    expected = compatible_types.get(
        source_type,
        set()
    )

    # If Crossref omitted type, do not reject a DOI record solely for that.
    if not crossref_type:
        return True

    return crossref_type in expected


def crossref_filter_for_source_type(
    source_type
):
    return {
        "Article": "journal-article",
        "Book": "book",
        "Thesis": "dissertation",
    }.get(
        source_type,
        ""
    )


def crossref_work_from_doi(
    doi
):
    doi = normalize_doi(
        doi
    )

    if not doi:
        return None

    encoded_doi = urllib.parse.quote(
        doi,
        safe=""
    )

    url = (
        "https://api.crossref.org/works/"
        + encoded_doi
    )

    try:
        payload = crossref_request_json(
            url
        )

        return payload.get(
            "message"
        )

    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None

        raise


def crossref_institution_name(
    work
):
    institution = work.get(
        "institution"
    )

    if isinstance(
        institution,
        dict
    ):
        return (
            institution.get(
                "name",
                ""
            )
            or ""
        ).strip()

    if isinstance(
        institution,
        list
    ):
        for item in institution:
            if isinstance(
                item,
                dict
            ):
                name = (
                    item.get(
                        "name",
                        ""
                    )
                    or ""
                ).strip()

                if name:
                    return name

            elif item:
                return str(
                    item
                ).strip()

    return ""


def first_isbn_from_crossref(
    work
):
    values = work.get(
        "ISBN",
        []
    )

    if isinstance(
        values,
        str
    ):
        values = [
            values
        ]

    for value in values or []:
        normalized = re.sub(
            r"[^0-9Xx]",
            "",
            str(value)
        )

        if len(normalized) in {
            10,
            13
        }:
            return normalized

    return ""


def crossref_work_to_zotero_draft(
    work,
    source_type,
    note
):
    base = {
        "title": first_crossref_value(
            work.get(
                "title"
            )
        ),
        "authors": crossref_authors_from_work(
            work
        ),
        "year": crossref_year_from_work(
            work
        ),
        "doi": (
            work.get(
                "DOI",
                ""
            )
            or ""
        ),
        "url": (
            work.get(
                "URL",
                ""
            )
            or ""
        ),
        "notes": note,
    }

    if source_type == "Article":
        base.update(
            {
                "journal": first_crossref_value(
                    work.get(
                        "container-title"
                    )
                ),
                "volume": (
                    work.get(
                        "volume",
                        ""
                    )
                    or ""
                ),
                "issue": (
                    work.get(
                        "issue",
                        ""
                    )
                    or ""
                ),
                "pages": (
                    work.get(
                        "page",
                        ""
                    )
                    or ""
                ),
            }
        )

    elif source_type == "Book":
        base.update(
            {
                "publisher": (
                    work.get(
                        "publisher",
                        ""
                    )
                    or ""
                ),
                "place": (
                    work.get(
                        "publisher-location",
                        ""
                    )
                    or ""
                ),
                "edition": first_crossref_value(
                    work.get(
                        "edition"
                    )
                ),
                "isbn": first_isbn_from_crossref(
                    work
                ),
            }
        )

    elif source_type == "Thesis":
        base.update(
            {
                "university": crossref_institution_name(
                    work
                ),
                "thesis_type": "Thesis",
            }
        )

    return normalize_zotero_draft(
        base,
        source_type
    )


def normalize_title_for_matching(
    text
):
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        (
            text
            or ""
        ).lower()
    ).strip()


def metadata_title_similarity(
    first,
    second
):
    first = normalize_title_for_matching(
        first
    )

    second = normalize_title_for_matching(
        second
    )

    if not first or not second:
        return 0.0

    return SequenceMatcher(
        None,
        first,
        second
    ).ratio()


def crossref_search_by_title(
    title,
    probable_year="",
    source_type="Article"
):
    title = (
        title
        or ""
    ).strip()

    if not title:
        return (
            None,
            0.0
        )

    params = {
        "query.title": title,
        "rows": "5",
    }

    type_filter = (
        crossref_filter_for_source_type(
            source_type
        )
    )

    if type_filter:
        params[
            "filter"
        ] = (
            f"type:{type_filter}"
        )

    url = (
        "https://api.crossref.org/works?"
        + urllib.parse.urlencode(
            params
        )
    )

    payload = crossref_request_json(
        url
    )

    items = (
        payload.get(
            "message",
            {}
        ).get(
            "items",
            []
        )
    )

    best_item = None
    best_score = 0.0

    for item in items:
        if not crossref_type_is_compatible(
            item,
            source_type
        ):
            continue

        item_title = first_crossref_value(
            item.get(
                "title"
            )
        )

        score = metadata_title_similarity(
            title,
            item_title
        )

        item_year = crossref_year_from_work(
            item
        )

        if (
            probable_year
            and item_year
            and probable_year == item_year
        ):
            score += 0.03

        if score > best_score:
            best_score = score
            best_item = item

    return (
        best_item,
        min(
            best_score,
            1.0
        )
    )


def extract_isbn_from_text(
    text
):
    excerpt = (
        text
        or ""
    )

    patterns = [
        r"\bISBN(?:-1[03])?\s*:?\s*([0-9Xx][0-9Xx\-\s]{8,20}[0-9Xx])",
        r"\b(97[89][0-9\-\s]{10,20}[0-9])\b",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            excerpt,
            re.IGNORECASE
        )

        if not match:
            continue

        candidate = re.sub(
            r"[^0-9Xx]",
            "",
            match.group(1)
        )

        if len(candidate) in {
            10,
            13
        }:
            return candidate

    return ""


def openlibrary_request_json(
    url
):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Doctoral-Literature-Agent/1.0 "
                "(book metadata lookup)"
            ),
            "Accept": "application/json",
        }
    )

    with urllib.request.urlopen(
        request,
        timeout=20,
        context=ZOTERO_SSL_CONTEXT
    ) as response:
        return json.loads(
            response.read().decode(
                "utf-8"
            )
        )


def openlibrary_book_by_isbn(
    isbn
):
    isbn = re.sub(
        r"[^0-9Xx]",
        "",
        isbn or ""
    )

    if not isbn:
        return None

    bibkey = (
        "ISBN:"
        + isbn
    )

    params = urllib.parse.urlencode(
        {
            "bibkeys": bibkey,
            "format": "json",
            "jscmd": "data",
        }
    )

    payload = openlibrary_request_json(
        "https://openlibrary.org/api/books?"
        + params
    )

    return payload.get(
        bibkey
    )


def openlibrary_book_to_zotero_draft(
    record,
    isbn,
    note
):
    authors = []

    for author in (
        record.get(
            "authors",
            []
        )
        or []
    ):
        name = (
            author.get(
                "name",
                ""
            )
            if isinstance(
                author,
                dict
            )
            else str(
                author
            )
        )

        if name:
            authors.append(
                name.strip()
            )

    publishers = (
        record.get(
            "publishers",
            []
        )
        or []
    )

    publisher = ""

    if publishers:
        first = publishers[0]

        publisher = (
            first.get(
                "name",
                ""
            )
            if isinstance(
                first,
                dict
            )
            else str(
                first
            )
        ).strip()

    places = (
        record.get(
            "publish_places",
            []
        )
        or []
    )

    place = ""

    if places:
        first_place = places[0]

        place = (
            first_place.get(
                "name",
                ""
            )
            if isinstance(
                first_place,
                dict
            )
            else str(
                first_place
            )
        ).strip()

    publish_date = (
        record.get(
            "publish_date",
            ""
        )
        or ""
    )

    year_match = re.search(
        r"\b(?:19|20)\d{2}\b",
        publish_date
    )

    return normalize_zotero_draft(
        {
            "title": (
                record.get(
                    "title",
                    ""
                )
                or ""
            ),
            "authors": authors,
            "year": (
                year_match.group(0)
                if year_match
                else publish_date
            ),
            "publisher": publisher,
            "place": place,
            "isbn": isbn,
            "num_pages": (
                record.get(
                    "number_of_pages",
                    ""
                )
                or ""
            ),
            # Open Library's item URL is a catalog record, not the accessed work.
            "url": "",
            "notes": note,
        },
        "Book"
    )


def clean_possible_title_line(
    line
):
    return re.sub(
        r"\s+",
        " ",
        line or ""
    ).strip()


def detect_thesis_type_locally(
    excerpt
):
    lowered = (
        excerpt
        or ""
    ).lower()

    if re.search(
        r"\b(ph\.?\s*d\.?|doctor(?:al)?)[^\n]{0,40}"
        r"(thesis|dissertation)\b",
        lowered
    ):
        return "Doctoral dissertation"

    if re.search(
        r"\b(master(?:'s)?|msc|m\.?\s*a\.?)[^\n]{0,40}"
        r"(thesis|dissertation)\b",
        lowered
    ):
        return "Master's thesis"

    if re.search(
        r"\b(mphil|m\.?\s*phil\.?)[^\n]{0,40}"
        r"(thesis|dissertation)\b",
        lowered
    ):
        return "MPhil thesis"

    if re.search(
        r"\b(thesis|dissertation)\b",
        lowered
    ):
        return "Thesis"

    return ""


def detect_university_locally(
    lines
):
    """
    Conservative title-page heuristic for theses.
    """
    candidates = []

    for line in lines[:80]:
        clean = clean_possible_title_line(
            line
        )

        if (
            5 <= len(clean) <= 140
            and re.search(
                r"\b(university|universität|universite|université)\b",
                clean,
                re.IGNORECASE
            )
            and "@" not in clean
            and not re.search(
                r"\bdepartment\b|\bfaculty\b|\bschool\b",
                clean,
                re.IGNORECASE
            )
        ):
            candidates.append(
                clean
            )

    if not candidates:
        return ""

    # Prefer a short standalone institution line.
    return min(
        candidates,
        key=len
    )


def local_pdf_metadata_guess(
    paper_text,
    filename,
    source_type
):
    """
    Conservative local fallback. The selected Article / Book / Thesis category
    is authoritative. Uncertain fields stay blank.
    """
    excerpt = zotero_metadata_source_excerpt(
        paper_text
    )

    clean_text = re.sub(
        r"--- PAGE \d+ ---",
        "",
        excerpt or ""
    )

    lines = [
        clean_possible_title_line(
            line
        )
        for line in clean_text.splitlines()
        if line.strip()
    ]

    # Only inspect the beginning of the source for identifiers so a DOI/ISBN in
    # the reference list is not accidentally treated as the source identifier.
    doi = extract_doi_from_text(
        excerpt[:14000]
    )

    isbn = (
        extract_isbn_from_text(
            excerpt[:16000]
        )
        if source_type == "Book"
        else ""
    )

    year_matches = re.findall(
        r"\b(?:19|20)\d{2}\b",
        "\n".join(
            lines[:80]
        )
    )

    probable_year = (
        year_matches[0]
        if year_matches
        else ""
    )

    title_candidates = []

    blocked_patterns = re.compile(
        r"^(abstract|introduction|keywords?|doi|isbn|https?://|"
        r"copyright|received|accepted|published|volume|vol\.|"
        r"issue|issn|references?|contents|table of contents)\b",
        re.IGNORECASE
    )

    for line in lines[:35]:
        if (
            15 <= len(line) <= 260
            and not blocked_patterns.match(
                line
            )
            and not re.match(
                r"^\d+$",
                line
            )
            and "@" not in line
        ):
            title_candidates.append(
                line
            )

    probable_title = ""

    if title_candidates:
        probable_title = max(
            title_candidates,
            key=lambda value: (
                min(
                    len(value),
                    180
                ),
                -abs(
                    len(value) - 90
                )
            )
        )
    else:
        probable_title = re.sub(
            r"\.pdf$",
            "",
            filename or "",
            flags=re.IGNORECASE
        )

    draft = {
        "title": probable_title,
        "authors": [],
        "year": probable_year,
        "doi": doi,
        "url": "",
        "notes": (
            "Prepared locally from the beginning of the PDF. "
            "Fields not established reliably were left blank."
        ),
    }

    if source_type == "Book":
        draft.update(
            {
                "isbn": isbn,
                "publisher": "",
                "place": "",
                "edition": "",
                "num_pages": "",
            }
        )

    elif source_type == "Thesis":
        draft.update(
            {
                "university": detect_university_locally(
                    lines
                ),
                "thesis_type": detect_thesis_type_locally(
                    excerpt
                ),
            }
        )

    return normalize_zotero_draft(
        draft,
        source_type
    )


def extract_zotero_metadata_free(
    paper_text,
    filename,
    source_type
):
    """
    ZERO AI TOKENS.

    Article:
      PDF DOI/title/year -> Crossref journal-article metadata.

    Book:
      PDF ISBN -> Open Library first; DOI -> Crossref book metadata;
      otherwise strong Crossref book-title match.

    Thesis:
      PDF DOI -> Crossref dissertation metadata; otherwise strong dissertation
      title match; local title-page heuristics can retain thesis type/university.

    The chosen source_type remains authoritative and is never silently changed.
    """
    source_type = (
        source_type
        if source_type in {
            "Article",
            "Book",
            "Thesis"
        }
        else "Article"
    )

    local_draft = local_pdf_metadata_guess(
        paper_text,
        filename,
        source_type
    )

    # -------------------------
    # BOOK: ISBN is strongest
    # -------------------------
    if source_type == "Book":
        isbn = (
            local_draft.get(
                "isbn",
                ""
            )
            or ""
        )

        if isbn:
            try:
                book_record = (
                    openlibrary_book_by_isbn(
                        isbn
                    )
                )

                if book_record:
                    draft = (
                        openlibrary_book_to_zotero_draft(
                            book_record,
                            isbn,
                            "Book metadata matched through Open Library using the ISBN found in the PDF."
                        )
                    )

                    # Keep a DOI observed on the book itself if present.
                    if local_draft.get(
                        "doi"
                    ):
                        draft[
                            "doi"
                        ] = local_draft[
                            "doi"
                        ]

                    return draft

            except Exception as error:
                local_draft[
                    "notes"
                ] = (
                    "ISBN was found locally, but Open Library lookup failed: "
                    + str(error)
                )

    # -------------------------
    # DOI -> Crossref
    # -------------------------
    doi = normalize_doi(
        local_draft.get(
            "doi",
            ""
        )
    )

    if doi:
        try:
            work = crossref_work_from_doi(
                doi
            )

            if (
                work
                and crossref_type_is_compatible(
                    work,
                    source_type
                )
            ):
                draft = (
                    crossref_work_to_zotero_draft(
                        work,
                        source_type,
                        (
                            "Verified through Crossref using the DOI found "
                            f"in this {source_type.lower()}."
                        )
                    )
                )

                # Preserve useful thesis title-page fields Crossref may omit.
                if source_type == "Thesis":
                    if (
                        not draft.get(
                            "university"
                        )
                        and local_draft.get(
                            "university"
                        )
                    ):
                        draft[
                            "university"
                        ] = local_draft[
                            "university"
                        ]

                    if (
                        not draft.get(
                            "thesis_type"
                        )
                        and local_draft.get(
                            "thesis_type"
                        )
                    ):
                        draft[
                            "thesis_type"
                        ] = local_draft[
                            "thesis_type"
                        ]

                return draft

            elif work:
                local_draft[
                    "notes"
                ] = (
                    "A DOI was found, but the Crossref item type did not match "
                    f"the selected {source_type} category, so it was not used "
                    "to overwrite the record."
                )

        except Exception as error:
            local_draft[
                "notes"
            ] = (
                "DOI was found locally, but Crossref lookup failed: "
                + str(error)
            )

    # -------------------------
    # Strong title search
    # -------------------------
    title = (
        local_draft.get(
            "title",
            ""
        )
        or ""
    )

    probable_year = (
        local_draft.get(
            "year",
            ""
        )
        or ""
    )

    if title:
        try:
            work, score = (
                crossref_search_by_title(
                    title,
                    probable_year,
                    source_type
                )
            )

            if (
                work
                and score >= 0.90
            ):
                draft = (
                    crossref_work_to_zotero_draft(
                        work,
                        source_type,
                        (
                            f"Matched through Crossref as {source_type} "
                            f"using the PDF title ({score:.0%} title match)."
                        )
                    )
                )

                if source_type == "Book":
                    if (
                        not draft.get(
                            "isbn"
                        )
                        and local_draft.get(
                            "isbn"
                        )
                    ):
                        draft[
                            "isbn"
                        ] = local_draft[
                            "isbn"
                        ]

                if source_type == "Thesis":
                    if (
                        not draft.get(
                            "university"
                        )
                        and local_draft.get(
                            "university"
                        )
                    ):
                        draft[
                            "university"
                        ] = local_draft[
                            "university"
                        ]

                    if (
                        not draft.get(
                            "thesis_type"
                        )
                        and local_draft.get(
                            "thesis_type"
                        )
                    ):
                        draft[
                            "thesis_type"
                        ] = local_draft[
                            "thesis_type"
                        ]

                return draft

        except Exception as error:
            local_draft[
                "notes"
            ] = (
                "Local metadata was prepared, but the free external metadata "
                "lookup failed: "
                + str(error)
            )

    return local_draft


# =========================================================
# ZOTERO API HELPERS
# =========================================================

ZOTERO_API_BASE = "https://api.zotero.org"

# Use certifi's CA bundle explicitly. This avoids common macOS/Python
# CERTIFICATE_VERIFY_FAILED errors while keeping SSL verification enabled.
ZOTERO_SSL_CONTEXT = ssl.create_default_context(
    cafile=certifi.where()
)


def zotero_headers():
    if not zotero_user_id or not zotero_api_key:
        raise ValueError(
            "Zotero credentials are missing from .env. "
            "Add ZOTERO_USER_ID and ZOTERO_API_KEY."
        )

    return {
        "Zotero-API-Key": zotero_api_key,
        "Zotero-API-Version": "3",
        "Content-Type": "application/json",
        "User-Agent": "Doctoral-Literature-Agent/1.0",
    }


def zotero_api_request(
    path,
    method="GET",
    payload=None
):
    url = (
        ZOTERO_API_BASE
        + path
    )

    data = None

    if payload is not None:
        data = json.dumps(
            payload
        ).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers=zotero_headers()
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=20,
            context=ZOTERO_SSL_CONTEXT
        ) as response:
            raw = response.read()

            if not raw:
                return None

            content_type = (
                response.headers.get(
                    "Content-Type",
                    ""
                )
            )

            if "json" in content_type:
                return json.loads(
                    raw.decode("utf-8")
                )

            return raw.decode(
                "utf-8",
                errors="replace"
            )

    except urllib.error.HTTPError as error:
        try:
            detail = (
                error.read()
                .decode(
                    "utf-8",
                    errors="replace"
                )
            )
        except Exception:
            detail = ""

        raise RuntimeError(
            f"Zotero API error {error.code}: "
            f"{detail or error.reason}"
        )

    except urllib.error.URLError as error:
        reason_text = str(
            error.reason
        )

        if "CERTIFICATE_VERIFY_FAILED" in reason_text:
            raise RuntimeError(
                "Secure connection to Zotero failed because Python could not "
                "verify the certificate chain. This app is configured to use "
                "certifi's trusted CA bundle; make sure the 'certifi' package "
                "is installed in the same Python environment running Streamlit."
            )

        raise RuntimeError(
            f"Could not reach Zotero: {error.reason}"
        )


@st.cache_data(
    ttl=300,
    show_spinner=False
)
def get_zotero_collections_cached(
    user_id,
    api_key
):
    if not user_id or not api_key:
        return []

    path = (
        f"/users/{urllib.parse.quote(str(user_id))}"
        "/collections?limit=100"
    )

    return zotero_api_request(
        path
    ) or []


@st.cache_data(
    ttl=180,
    show_spinner=False
)
def get_zotero_collection_items_cached(
    user_id,
    api_key,
    collection_key
):
    if (
        not user_id
        or not api_key
        or not collection_key
    ):
        return []

    all_items = []
    start = 0
    limit = 100

    while True:
        path = (
            f"/users/{urllib.parse.quote(str(user_id))}"
            f"/collections/{urllib.parse.quote(collection_key)}"
            f"/items/top?limit={limit}&start={start}"
        )

        batch = (
            zotero_api_request(
                path
            )
            or []
        )

        all_items.extend(
            batch
        )

        if len(batch) < limit:
            break

        start += limit

        # Safety guard for unexpectedly huge collections.
        if start >= 2000:
            break

    return all_items


def get_zotero_item(
    item_key
):
    path = (
        f"/users/{urllib.parse.quote(str(zotero_user_id))}"
        f"/items/{urllib.parse.quote(item_key)}"
    )

    return zotero_api_request(
        path
    )


def normalize_doi(value):
    if not value:
        return ""

    value = value.strip().lower()

    value = re.sub(
        r"^https?://(?:dx\.)?doi\.org/",
        "",
        value
    )

    value = re.sub(
        r"^doi:\s*",
        "",
        value
    )

    return value.rstrip(
        ".,;)"
    )


def extract_doi_from_text(
    text
):
    match = re.search(
        r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b",
        text or "",
        re.IGNORECASE
    )

    if not match:
        return ""

    return normalize_doi(
        match.group(0)
    )


def guess_zotero_metadata(
    full_text,
    filename
):
    clean_text = re.sub(
        r"--- PAGE \d+ ---",
        "",
        full_text or ""
    )

    lines = [
        line.strip()
        for line in clean_text.splitlines()
        if line.strip()
    ]

    title_candidates = []

    for line in lines[:25]:
        if (
            20 <= len(line) <= 250
            and not re.match(
                r"^(abstract|introduction|keywords?|doi|https?://)",
                line,
                re.IGNORECASE
            )
            and not re.match(
                r"^\d+$",
                line
            )
        ):
            title_candidates.append(
                line
            )

    if title_candidates:
        probable_title = max(
            title_candidates,
            key=len
        )
    else:
        probable_title = re.sub(
            r"\.pdf$",
            "",
            filename or "",
            flags=re.IGNORECASE
        )

    year_matches = re.findall(
        r"\b(?:19|20)\d{2}\b",
        "\n".join(
            lines[:60]
        )
    )

    probable_year = (
        year_matches[0]
        if year_matches
        else ""
    )

    return {
        "title": probable_title,
        "year": probable_year,
        "doi": extract_doi_from_text(
            full_text
        ),
    }


def zotero_item_data(
    item
):
    if not item:
        return {}

    return (
        item.get("data", {})
        if isinstance(item, dict)
        else {}
    )


def zotero_creator_display(
    creator
):
    if creator.get("name"):
        return creator.get(
            "name",
            ""
        )

    first = creator.get(
        "firstName",
        ""
    ).strip()

    last = creator.get(
        "lastName",
        ""
    ).strip()

    return " ".join(
        part
        for part in [
            first,
            last
        ]
        if part
    )


def zotero_authors_display(
    data
):
    creators = data.get(
        "creators",
        []
    )

    names = [
        zotero_creator_display(
            creator
        )
        for creator in creators
        if creator.get(
            "creatorType"
        ) in {
            "author",
            "bookAuthor"
        }
    ]

    names = [
        name
        for name in names
        if name
    ]

    return "; ".join(
        names
    )


def format_zotero_context(
    metadata
):
    if not metadata:
        return ""

    data = (
        metadata.get("data", metadata)
        if isinstance(metadata, dict)
        else {}
    )

    if not data:
        return ""

    authors = (
        zotero_authors_display(
            data
        )
        or "Not available"
    )

    title = (
        data.get("title")
        or "Not available"
    )

    date = (
        data.get("date")
        or "Not available"
    )

    publication = (
        data.get("publicationTitle")
        or data.get("publisher")
        or data.get("university")
        or ""
    )

    doi = (
        data.get("DOI")
        or ""
    )

    return (
        "VERIFIED ZOTERO METADATA\n"
        f"Title: {title}\n"
        f"Author(s): {authors}\n"
        f"Date/Year: {date}\n"
        f"Publication/Publisher: {publication or 'Not available'}\n"
        f"DOI: {doi or 'Not available'}"
    )


def apa_creator_name(
    creator
):
    if creator.get("name"):
        return creator.get(
            "name",
            ""
        )

    last = creator.get(
        "lastName",
        ""
    ).strip()

    first = creator.get(
        "firstName",
        ""
    ).strip()

    initials = " ".join(
        f"{part[0].upper()}."
        for part in re.findall(
            r"[A-Za-zÀ-ÖØ-öø-ÿ]+",
            first
        )
        if part
    )

    if last and initials:
        return f"{last}, {initials}"

    return (
        last
        or first
        or ""
    )


def format_apa7_from_zotero(
    metadata
):
    if not metadata:
        return (
            "[Zotero metadata unavailable]"
        )

    data = (
        metadata.get("data", metadata)
        if isinstance(metadata, dict)
        else {}
    )

    creators = [
        apa_creator_name(
            creator
        )
        for creator in data.get(
            "creators",
            []
        )
        if creator.get(
            "creatorType"
        ) in {
            "author",
            "bookAuthor"
        }
    ]

    creators = [
        creator
        for creator in creators
        if creator
    ]

    if not creators:
        author_text = ""
    elif len(creators) == 1:
        author_text = creators[0]
    elif len(creators) == 2:
        author_text = (
            f"{creators[0]}, & "
            f"{creators[1]}"
        )
    else:
        author_text = (
            ", ".join(
                creators[:-1]
            )
            + ", & "
            + creators[-1]
        )

    date_text = (
        data.get("date")
        or "n.d."
    )

    year_match = re.search(
        r"\b(?:19|20)\d{2}\b",
        date_text
    )

    year = (
        year_match.group(0)
        if year_match
        else "n.d."
    )

    title = (
        data.get("title")
        or "[Untitled]"
    )

    item_type = data.get(
        "itemType",
        ""
    )

    doi = normalize_doi(
        data.get(
            "DOI",
            ""
        )
    )

    doi_text = (
        f" https://doi.org/{doi}"
        if doi
        else ""
    )

    if item_type == "journalArticle":
        journal = (
            data.get(
                "publicationTitle"
            )
            or ""
        )

        volume = (
            data.get("volume")
            or ""
        )

        issue = (
            data.get("issue")
            or ""
        )

        pages = (
            data.get("pages")
            or ""
        )

        source_bits = []

        if journal:
            source_bits.append(
                f"*{journal}*"
            )

        if volume:
            volume_text = (
                f"*{volume}*"
            )

            if issue:
                volume_text += (
                    f"({issue})"
                )

            source_bits.append(
                volume_text
            )
        elif issue:
            source_bits.append(
                f"({issue})"
            )

        if pages:
            source_bits.append(
                pages
            )

        source_text = ", ".join(
            source_bits
        )

        reference = (
            f"{author_text + '. ' if author_text else ''}"
            f"({year}). "
            f"{title}. "
            f"{source_text}"
            f"{doi_text}"
        )

    elif item_type == "book":
        publisher = (
            data.get("publisher")
            or ""
        )

        reference = (
            f"{author_text + '. ' if author_text else ''}"
            f"({year}). "
            f"*{title}*."
        )

        if publisher:
            reference += (
                f" {publisher}."
            )

        reference += doi_text

    elif item_type == "thesis":
        university = (
            data.get("university")
            or ""
        )

        thesis_type = (
            data.get("thesisType")
            or "Thesis"
        )

        bracket = ", ".join(
            part
            for part in [
                thesis_type,
                university
            ]
            if part
        )

        reference = (
            f"{author_text + '. ' if author_text else ''}"
            f"({year}). "
            f"*{title}*"
        )

        if bracket:
            reference += (
                f" [{bracket}]."
            )

        reference += doi_text

    else:
        reference = (
            f"{author_text + '. ' if author_text else ''}"
            f"({year}). "
            f"{title}."
            f"{doi_text}"
        )

    return re.sub(
        r"\s+",
        " ",
        reference
    ).strip()



def _saved_author_surname(name):
    name = str(name or "").strip()

    if not name:
        return ""

    if "," in name:
        return name.split(",", 1)[0].strip()

    parts = name.split()
    return parts[-1] if parts else ""


def _saved_author_apa(name):
    name = str(name or "").strip()

    if not name:
        return ""

    if "," in name:
        last, first = [
            part.strip()
            for part in name.split(",", 1)
        ]
    else:
        parts = name.split()

        if len(parts) == 1:
            return parts[0]

        last = parts[-1]
        first = " ".join(parts[:-1])

    initials = " ".join(
        f"{part[0].upper()}."
        for part in re.findall(
            r"[A-Za-zÀ-ÖØ-öø-ÿ]+",
            first
        )
        if part
    )

    return (
        f"{last}, {initials}"
        if initials
        else last
    )


def _saved_authors_apa(authors):
    names = [
        _saved_author_apa(author)
        for author in (authors or [])
        if str(author).strip()
    ]

    names = [
        name
        for name in names
        if name
    ]

    if not names:
        return ""

    if len(names) == 1:
        return names[0]

    if len(names) == 2:
        return f"{names[0]}, & {names[1]}"

    return (
        ", ".join(names[:-1])
        + ", & "
        + names[-1]
    )


def _saved_in_text_citation(authors, year, title=""):
    surnames = [
        _saved_author_surname(author)
        for author in (authors or [])
        if str(author).strip()
    ]

    surnames = [
        surname
        for surname in surnames
        if surname
    ]

    year = str(year or "n.d.").strip() or "n.d."

    if len(surnames) == 1:
        return f"({surnames[0]}, {year})"

    if len(surnames) == 2:
        return (
            f"({surnames[0]} & "
            f"{surnames[1]}, {year})"
        )

    if len(surnames) >= 3:
        return (
            f"({surnames[0]} et al., "
            f"{year})"
        )

    short_title = str(title or "Untitled source").strip()

    if len(short_title) > 50:
        short_title = (
            short_title[:47].rstrip()
            + "..."
        )

    return f'("{short_title}", {year})'


def format_apa7_from_saved_draft(
    draft,
    source_type
):
    draft = normalize_zotero_draft(
        draft,
        source_type
    )

    authors = draft.get("authors", []) or []
    author_text = _saved_authors_apa(authors)
    year = draft.get("year") or "n.d."
    title = draft.get("title") or "[Untitled]"

    doi = normalize_doi(
        draft.get("doi", "")
    )

    doi_text = (
        f" https://doi.org/{doi}"
        if doi
        else ""
    )

    prefix = (
        f"{author_text}. "
        if author_text
        else ""
    )

    if source_type == "Article":
        journal = draft.get("journal") or ""
        volume = draft.get("volume") or ""
        issue = draft.get("issue") or ""
        pages = draft.get("pages") or ""

        source_bits = []

        if journal:
            source_bits.append(f"*{journal}*")

        if volume:
            volume_text = f"*{volume}*"

            if issue:
                volume_text += f"({issue})"

            source_bits.append(volume_text)

        elif issue:
            source_bits.append(f"({issue})")

        if pages:
            source_bits.append(pages)

        source_text = ", ".join(source_bits)

        reference = (
            f"{prefix}({year}). "
            f"{title}."
        )

        if source_text:
            reference += f" {source_text}."

        reference += doi_text

    elif source_type == "Book":
        publisher = draft.get("publisher") or ""

        reference = (
            f"{prefix}({year}). "
            f"*{title}*."
        )

        if publisher:
            reference += f" {publisher}."

        reference += doi_text

    elif source_type == "Thesis":
        university = draft.get("university") or ""
        thesis_type = (
            draft.get("thesis_type")
            or "Thesis"
        )

        bracket = ", ".join(
            part
            for part in [
                thesis_type,
                university
            ]
            if part
        )

        reference = (
            f"{prefix}({year}). "
            f"*{title}*"
        )

        if bracket:
            reference += f" [{bracket}]."

        reference += doi_text

    else:
        reference = (
            f"{prefix}({year}). "
            f"{title}."
            f"{doi_text}"
        )

    return re.sub(
        r"\s+",
        " ",
        reference
    ).strip()


def build_saved_citation_info(
    zotero_metadata_json,
    zotero_draft_json,
    source_type,
    fallback_filename
):
    """
    Prefer approved Zotero metadata. Otherwise use the saved editable
    metadata draft already stored in the app.
    """

    if zotero_metadata_json:
        try:
            metadata = json.loads(
                zotero_metadata_json
            )

            data = (
                metadata.get("data", metadata)
                if isinstance(metadata, dict)
                else {}
            )

            title = (
                data.get("title")
                or fallback_filename
            )

            authors = []

            for creator in data.get(
                "creators",
                []
            ):
                if creator.get(
                    "creatorType"
                ) not in {
                    "author",
                    "bookAuthor"
                }:
                    continue

                if creator.get("name"):
                    authors.append(
                        creator.get("name")
                    )
                else:
                    first = (
                        creator.get(
                            "firstName",
                            ""
                        )
                        or ""
                    ).strip()

                    last = (
                        creator.get(
                            "lastName",
                            ""
                        )
                        or ""
                    ).strip()

                    combined = (
                        f"{first} {last}".strip()
                    )

                    if combined:
                        authors.append(combined)

            date_text = (
                data.get("date")
                or ""
            )

            year_match = re.search(
                r"\b(?:19|20)\d{2}\b",
                date_text
            )

            year = (
                year_match.group(0)
                if year_match
                else "n.d."
            )

            return {
                "title": title,
                "authors": authors,
                "year": year,
                "in_text": _saved_in_text_citation(
                    authors,
                    year,
                    title
                ),
                "reference": format_apa7_from_zotero(
                    metadata
                ),
                "metadata_context": format_zotero_context(
                    metadata
                ),
                "metadata_source": "Approved Zotero metadata",
            }

        except Exception:
            pass

    if zotero_draft_json:
        try:
            draft = normalize_zotero_draft(
                json.loads(
                    zotero_draft_json
                ),
                source_type
            )

            title = (
                draft.get("title")
                or fallback_filename
            )

            authors = (
                draft.get("authors", [])
                or []
            )

            year = (
                draft.get("year")
                or "n.d."
            )

            publication = (
                draft.get("journal")
                or draft.get("publisher")
                or draft.get("university")
                or ""
            )

            metadata_context = (
                "SAVED APP METADATA\n"
                f"Title: {title}\n"
                f"Author(s): "
                f"{'; '.join(authors) if authors else 'Not available'}\n"
                f"Date/Year: {year}\n"
                f"Publication/Publisher: "
                f"{publication or 'Not available'}\n"
                f"DOI: "
                f"{draft.get('doi') or 'Not available'}"
            )

            return {
                "title": title,
                "authors": authors,
                "year": year,
                "in_text": _saved_in_text_citation(
                    authors,
                    year,
                    title
                ),
                "reference": format_apa7_from_saved_draft(
                    draft,
                    source_type
                ),
                "metadata_context": metadata_context,
                "metadata_source": "Saved app metadata",
            }

        except Exception:
            pass

    return {
        "title": fallback_filename,
        "authors": [],
        "year": "n.d.",
        "in_text": "",
        "reference": "",
        "metadata_context": "",
        "metadata_source": "Filename fallback only",
    }


def title_similarity(
    first,
    second
):
    def normalize_title(
        text
    ):
        return re.sub(
            r"[^a-z0-9]+",
            " ",
            (
                text
                or ""
            ).lower()
        ).strip()

    first_normalized = (
        normalize_title(
            first
        )
    )

    second_normalized = (
        normalize_title(
            second
        )
    )

    if (
        not first_normalized
        or not second_normalized
    ):
        return 0.0

    return SequenceMatcher(
        None,
        first_normalized,
        second_normalized
    ).ratio()


def find_zotero_match(
    items,
    full_text,
    filename
):
    guessed = (
        guess_zotero_metadata(
            full_text,
            filename
        )
    )

    source_doi = normalize_doi(
        guessed.get(
            "doi",
            ""
        )
    )

    source_title = (
        guessed.get(
            "title",
            ""
        )
    )

    # DOI match is strongest.
    if source_doi:
        for item in items:
            data = zotero_item_data(
                item
            )

            item_doi = normalize_doi(
                data.get(
                    "DOI",
                    ""
                )
            )

            if (
                item_doi
                and item_doi
                == source_doi
            ):
                return (
                    item,
                    "Exact DOI match"
                )

    # Otherwise use a conservative title similarity threshold.
    best_item = None
    best_score = 0.0

    for item in items:
        data = zotero_item_data(
            item
        )

        score = title_similarity(
            source_title,
            data.get(
                "title",
                ""
            )
        )

        if score > best_score:
            best_score = score
            best_item = item

    if (
        best_item
        and best_score >= 0.88
    ):
        return (
            best_item,
            f"Strong title match ({best_score:.0%})"
        )

    return (
        None,
        None
    )


def parse_creators_from_text(
    text
):
    creators = []

    for raw_name in re.split(
        r";|\n",
        text or ""
    ):
        raw_name = raw_name.strip()

        if not raw_name:
            continue

        if "," in raw_name:
            last, first = [
                part.strip()
                for part in raw_name.split(
                    ",",
                    1
                )
            ]
        else:
            parts = raw_name.split()

            if len(parts) == 1:
                last = parts[0]
                first = ""
            else:
                first = " ".join(
                    parts[:-1]
                )
                last = parts[-1]

        creators.append(
            {
                "creatorType": "author",
                "firstName": first,
                "lastName": last,
            }
        )

    return creators


def get_zotero_new_item_template(
    item_type
):
    """
    Zotero's own template defines valid fields for each item type.
    This prevents Article / Book / Thesis fields from being mixed.
    """
    return zotero_api_request(
        "/items/new?"
        + urllib.parse.urlencode(
            {
                "itemType": item_type
            }
        )
    )


def add_doi_to_extra(
    data,
    doi
):
    doi = normalize_doi(
        doi
    )

    if not doi:
        return

    if "DOI" in data:
        data[
            "DOI"
        ] = doi
        return

    if "extra" in data:
        existing = (
            data.get(
                "extra",
                ""
            )
            or ""
        ).strip()

        doi_line = (
            "DOI: "
            + doi
        )

        data[
            "extra"
        ] = (
            existing + "\n" + doi_line
            if existing
            else doi_line
        )


def set_if_supported(
    data,
    field,
    value
):
    value = (
        str(value).strip()
        if value is not None
        else ""
    )

    if (
        value
        and field in data
    ):
        data[
            field
        ] = value


def create_zotero_item(
    collection_key,
    source_type,
    title,
    authors,
    year,
    doi,
    publication_title="",
    publisher="",
    university="",
    volume="",
    issue="",
    pages="",
    url="",
    place="",
    edition="",
    isbn="",
    num_pages="",
    thesis_type=""
):
    """
    Source type is authoritative:
      Article -> Zotero journalArticle
      Book    -> Zotero book
      Thesis  -> Zotero thesis

    The item starts from Zotero's current template, so only fields supported by
    that Zotero item type are written.
    """
    item_type_map = {
        "Article": "journalArticle",
        "Book": "book",
        "Thesis": "thesis",
    }

    item_type = item_type_map.get(
        source_type,
        "journalArticle"
    )

    template = get_zotero_new_item_template(
        item_type
    )

    if not isinstance(
        template,
        dict
    ):
        raise RuntimeError(
            "Zotero did not return a valid item template."
        )

    data = template.copy()

    data[
        "itemType"
    ] = item_type

    set_if_supported(
        data,
        "title",
        title
    )

    if "creators" in data:
        data[
            "creators"
        ] = parse_creators_from_text(
            authors
        )

    set_if_supported(
        data,
        "date",
        year
    )

    set_if_supported(
        data,
        "url",
        url
    )

    # -------------------------
    # ARTICLE RULES
    # -------------------------
    if source_type == "Article":
        set_if_supported(
            data,
            "publicationTitle",
            publication_title
        )

        set_if_supported(
            data,
            "volume",
            volume
        )

        set_if_supported(
            data,
            "issue",
            issue
        )

        set_if_supported(
            data,
            "pages",
            pages
        )

        add_doi_to_extra(
            data,
            doi
        )

    # -------------------------
    # BOOK RULES
    # -------------------------
    elif source_type == "Book":
        set_if_supported(
            data,
            "publisher",
            publisher
        )

        set_if_supported(
            data,
            "place",
            place
        )

        set_if_supported(
            data,
            "edition",
            edition
        )

        set_if_supported(
            data,
            "ISBN",
            isbn
        )

        set_if_supported(
            data,
            "numPages",
            num_pages
        )

        # Zotero guidance allows DOI for non-journal types in Extra.
        add_doi_to_extra(
            data,
            doi
        )

    # -------------------------
    # THESIS RULES
    # -------------------------
    elif source_type == "Thesis":
        set_if_supported(
            data,
            "university",
            university
        )

        set_if_supported(
            data,
            "type",
            thesis_type
        )

        set_if_supported(
            data,
            "place",
            place
        )

        add_doi_to_extra(
            data,
            doi
        )

    if "collections" in data:
        data[
            "collections"
        ] = [
            collection_key
        ]

    path = (
        f"/users/{urllib.parse.quote(str(zotero_user_id))}"
        "/items"
    )

    result = zotero_api_request(
        path,
        method="POST",
        payload=[
            data
        ]
    )

    if not isinstance(
        result,
        dict
    ):
        raise RuntimeError(
            "Zotero returned an unexpected response."
        )

    successful = result.get(
        "successful",
        {}
    )

    created = (
        successful.get("0")
        or successful.get(0)
    )

    if not created:
        failed = result.get(
            "failed",
            {}
        )

        raise RuntimeError(
            "Zotero did not create the item. "
            f"{failed}"
        )

    return created


def save_zotero_link(
    paper_id,
    item,
    collection_key
):
    item_key = (
        item.get("key")
        or item.get(
            "data",
            {}
        ).get("key")
    )

    if not item_key:
        raise ValueError(
            "Zotero item key was not found."
        )

    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    metadata_json = json.dumps(
        item,
        ensure_ascii=False
    )

    cursor.execute(
        """
        UPDATE papers
        SET
            zotero_item_key = ?,
            zotero_collection_key = ?,
            zotero_metadata_json = ?,
            zotero_linked_at = ?
        WHERE id = ?
        """,
        (
            item_key,
            collection_key,
            metadata_json,
            now,
            paper_id
        )
    )

    conn.commit()


def clear_zotero_link(
    paper_id
):
    cursor.execute(
        """
        UPDATE papers
        SET
            zotero_item_key = NULL,
            zotero_collection_key = NULL,
            zotero_metadata_json = NULL,
            zotero_linked_at = NULL
        WHERE id = ?
        """,
        (
            paper_id,
        )
    )

    conn.commit()


# =========================================================
# SIDEBAR - FOLDER MANAGEMENT
# =========================================================

st.sidebar.header("Literature Review Folders")

budget_mode = st.sidebar.toggle(
    "💰 Budget Mode",
    value=True,
    help=(
        "Reduces API spending by favouring free extraction, shorter AI outputs, "
        "smaller library evidence bundles, and preventing automatic second-model calls."
    )
)

if budget_mode:
    st.sidebar.caption(
        "💰 Budget Mode: Quick-first · Deep off · shorter library searches"
    )
else:
    st.sidebar.caption(
        "Budget Mode off · Deep analysis and larger library searches available"
    )


st.sidebar.divider()
st.sidebar.subheader("📚 Zotero")

zotero_selected_collection_key = None
zotero_selected_collection_name = None

if not zotero_user_id or not zotero_api_key:
    st.sidebar.warning(
        "Zotero is not configured. Add ZOTERO_USER_ID and "
        "ZOTERO_API_KEY to your .env file."
    )
else:
    try:
        zotero_collections = (
            get_zotero_collections_cached(
                zotero_user_id,
                zotero_api_key
            )
        )

        collection_options = {}

        for collection in zotero_collections:
            data = collection.get(
                "data",
                {}
            )

            name = data.get(
                "name",
                "Unnamed collection"
            )

            key = (
                collection.get("key")
                or data.get("key")
            )

            if key:
                display_name = name

                if display_name in collection_options:
                    display_name = (
                        f"{name} ({key})"
                    )

                collection_options[
                    display_name
                ] = key

        if collection_options:
            collection_names = list(
                collection_options.keys()
            )

            preferred_index = 0

            for index, name in enumerate(
                collection_names
            ):
                if (
                    "doctoral literature agent"
                    in name.lower()
                    or "phd"
                    in name.lower()
                ):
                    preferred_index = index
                    break

            zotero_selected_collection_name = (
                st.sidebar.selectbox(
                    "Zotero collection used by this app:",
                    collection_names,
                    index=preferred_index,
                    key="zotero_collection_selector"
                )
            )

            zotero_selected_collection_key = (
                collection_options[
                    zotero_selected_collection_name
                ]
            )

            st.sidebar.success(
                "Zotero connected. Only the selected collection "
                "is used for matching or creating references."
            )

            st.sidebar.caption(
                "Metadata is prepared free and separately for Article, Book, "
                "and Thesis. Zotero's own item template is used when you approve "
                "the record. No Claude/OpenAI tokens are used."
            )

        else:
            st.sidebar.warning(
                "Zotero connected, but no collections were found."
            )

    except Exception as error:
        st.sidebar.error(
            f"Zotero connection failed: {error}"
        )


cursor.execute("""
SELECT name
FROM folders
ORDER BY name
""")

folders = [
    row[0]
    for row in cursor.fetchall()
]

source_types = [
    "Article",
    "Book",
    "Thesis"
]


# =========================================================
# RELIABILITY / VERIFICATION HELPERS
# =========================================================

def show_ai_verification_warning():
    st.warning(
        "⚠️ AI-generated analysis: verify quotations, APA 7 in-text citations, "
        "page numbers, reference-list entries, numerical results, bibliographic "
        "details, and important claims against the original source before using "
        "them in academic writing."
    )


def confidence_label(level, reason):
    icons = {
        "High": "🟢",
        "Medium": "🟡",
        "Low": "🔴",
        "Possible match": "🟠",
        "Not detected": "⚪",
    }
    icon = icons.get(level, "⚪")
    return f"{icon} **{level} confidence** — {reason}" if level in {"High", "Medium", "Low"} else f"{icon} **{level}** — {reason}"


# =========================================================
# FREE RULE-BASED EXTRACTION
# =========================================================

def extract_basic_information(
    paper_text,
    filename=""
):
    """
    Extracts basic information using only Python rules and text patterns.
    This does NOT call Claude or OpenAI and therefore uses no API tokens.
    """

    clean_text = re.sub(
        r"--- PAGE \d+ ---",
        "",
        paper_text
    )

    clean_text = re.sub(
        r"\n{3,}",
        "\n\n",
        clean_text
    )

    lines = [
        line.strip()
        for line in clean_text.splitlines()
        if line.strip()
    ]

    # -----------------------------------------------------
    # TITLE
    # -----------------------------------------------------

    probable_title = "Not confidently detected"

    first_lines = lines[:20]
    title_candidates = []

    for line in first_lines:
        if (
            20 <= len(line) <= 250
            and not re.match(
                r"^(abstract|introduction|keywords?|doi)",
                line,
                re.IGNORECASE
            )
            and not re.match(r"^\d+$", line)
        ):
            title_candidates.append(line)

    if title_candidates:
        probable_title = max(
            title_candidates,
            key=len
        )

    if probable_title == "Not confidently detected":
        title_confidence = confidence_label(
            "Not detected",
            "No plausible title-like line was found near the beginning of the PDF."
        )
    elif probable_title in lines[:8]:
        title_confidence = confidence_label(
            "Medium",
            "The text appears near the beginning and looks title-like, but layout extraction can still misidentify headers or subtitles."
        )
    else:
        title_confidence = confidence_label(
            "Low",
            "A title-like line was found, but its position makes it less reliable."
        )

    # -----------------------------------------------------
    # DOI
    # -----------------------------------------------------

    doi_match = re.search(
        r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b",
        clean_text,
        re.IGNORECASE
    )

    doi = (
        doi_match.group(0)
        if doi_match
        else "Not detected"
    )

    doi_confidence = (
        confidence_label(
            "High",
            "Matched the standard DOI pattern directly in the extracted text."
        )
        if doi_match
        else confidence_label(
            "Not detected",
            "No DOI-pattern match was found."
        )
    )

    # -----------------------------------------------------
    # YEAR
    # -----------------------------------------------------

    year_matches = re.findall(
        r"\b(?:19|20)\d{2}\b",
        "\n".join(lines[:60])
    )

    probable_year = (
        year_matches[0]
        if year_matches
        else "Not detected"
    )

    unique_years = list(dict.fromkeys(year_matches))

    if not year_matches:
        year_confidence = confidence_label(
            "Not detected",
            "No four-digit year was found near the beginning of the source."
        )
    elif len(unique_years) == 1:
        year_confidence = confidence_label(
            "Medium",
            "Only one year was found near the beginning, but it could still be a copyright, issue, or reference year."
        )
    else:
        year_confidence = confidence_label(
            "Low",
            "Several years were found near the beginning; the first one may not be the publication year."
        )

    # -----------------------------------------------------
    # ABSTRACT
    # -----------------------------------------------------

    abstract = "Not detected"

    abstract_match = re.search(
        r"\bAbstract\b\s*(.*?)(?=\n\s*(?:Keywords?|Key words|Introduction|1[\.\s]))",
        clean_text,
        re.IGNORECASE | re.DOTALL
    )

    if abstract_match:
        abstract = abstract_match.group(1).strip()

    if abstract_match and len(abstract) >= 100:
        abstract_confidence = confidence_label(
            "High",
            "Text was captured after an explicit Abstract heading and before a likely following section."
        )
    elif abstract_match:
        abstract_confidence = confidence_label(
            "Medium",
            "An Abstract heading was found, but the captured text is unusually short and should be checked."
        )
    else:
        abstract_confidence = confidence_label(
            "Not detected",
            "No clearly bounded Abstract section was found."
        )

    # -----------------------------------------------------
    # KEYWORDS
    # -----------------------------------------------------

    keywords = "Not detected"

    keywords_match = re.search(
        r"(?:Keywords?|Key words)\s*[:\-]?\s*(.+)",
        clean_text,
        re.IGNORECASE
    )

    if keywords_match:
        keywords = (
            keywords_match.group(1)
            .split("\n")[0]
            .strip()
        )

    keywords_confidence = (
        confidence_label(
            "High",
            "Captured from an explicit Keywords/Key words label."
        )
        if keywords_match
        else confidence_label(
            "Not detected",
            "No explicit Keywords label was found."
        )
    )

    # -----------------------------------------------------
    # SECTION HEADINGS
    # -----------------------------------------------------

    section_headings = []

    known_headings = [
        "abstract",
        "introduction",
        "background",
        "literature review",
        "theoretical framework",
        "method",
        "methods",
        "methodology",
        "data",
        "participants",
        "results",
        "findings",
        "discussion",
        "conclusion",
        "conclusions",
        "limitations",
        "references"
    ]

    for line in lines:
        normalized = re.sub(
            r"^\d+(?:\.\d+)*[\.\s]*",
            "",
            line
        ).strip().lower()

        if normalized in known_headings:
            heading = line.strip()

            if heading not in section_headings:
                section_headings.append(heading)

    # -----------------------------------------------------
    # SENTENCES
    # -----------------------------------------------------

    sentences = re.split(
        r"(?<=[.!?])\s+",
        clean_text
    )

    # -----------------------------------------------------
    # AIM / PURPOSE
    # -----------------------------------------------------

    aim_patterns = [
        r"\bthe aim of (?:this|the) study\b",
        r"\bthe purpose of (?:this|the) study\b",
        r"\bthis study aims?\b",
        r"\bthis study investigates?\b",
        r"\bthis study examines?\b",
        r"\bthis paper aims?\b",
        r"\bthis paper investigates?\b",
        r"\bwe aim to\b",
        r"\bwe investigate\b",
        r"\bwe examine\b"
    ]

    aim_sentences = []

    for sentence in sentences:
        if any(
            re.search(
                pattern,
                sentence,
                re.IGNORECASE
            )
            for pattern in aim_patterns
        ):
            aim_sentences.append(sentence.strip())

    aim_sentences = aim_sentences[:5]

    # -----------------------------------------------------
    # POSSIBLE RESEARCH QUESTIONS
    # -----------------------------------------------------

    rq_patterns = [
        r"\bresearch question",
        r"\bresearch questions",
        r"\bwe ask\b",
        r"\bthe following question",
        r"\bthe following questions"
    ]

    research_question_sentences = []

    for sentence in sentences:
        if any(
            re.search(
                pattern,
                sentence,
                re.IGNORECASE
            )
            for pattern in rq_patterns
        ):
            research_question_sentences.append(
                sentence.strip()
            )

    research_question_sentences = (
        research_question_sentences[:8]
    )

    # -----------------------------------------------------
    # METHODS
    # -----------------------------------------------------

    method_patterns = [
        r"\bparticipants?\b",
        r"\bcorpus\b",
        r"\bdataset\b",
        r"\bdata were\b",
        r"\bdata was\b",
        r"\bwe collected\b",
        r"\bwe analysed\b",
        r"\bwe analyzed\b",
        r"\bmethodology\b",
        r"\bmethod\b",
        r"\bsample consisted\b"
    ]

    method_sentences = []

    for sentence in sentences:
        if any(
            re.search(
                pattern,
                sentence,
                re.IGNORECASE
            )
            for pattern in method_patterns
        ):
            method_sentences.append(
                sentence.strip()
            )

    method_sentences = method_sentences[:10]

    # -----------------------------------------------------
    # FINDINGS / RESULTS
    # -----------------------------------------------------

    result_patterns = [
        r"\bresults show",
        r"\bresults showed",
        r"\bresults indicate",
        r"\bresults indicated",
        r"\bfindings show",
        r"\bfindings suggest",
        r"\bwe found\b",
        r"\bour findings\b",
        r"\bthe analysis revealed\b",
        r"\bthe results suggest\b"
    ]

    result_sentences = []

    for sentence in sentences:
        if any(
            re.search(
                pattern,
                sentence,
                re.IGNORECASE
            )
            for pattern in result_patterns
        ):
            result_sentences.append(
                sentence.strip()
            )

    result_sentences = result_sentences[:10]

    # -----------------------------------------------------
    # CONCLUSIONS
    # -----------------------------------------------------

    conclusion_patterns = [
        r"\bwe conclude\b",
        r"\bin conclusion\b",
        r"\bthe study concludes\b",
        r"\bthese findings suggest\b",
        r"\boverall\b"
    ]

    conclusion_sentences = []

    for sentence in sentences:
        if any(
            re.search(
                pattern,
                sentence,
                re.IGNORECASE
            )
            for pattern in conclusion_patterns
        ):
            conclusion_sentences.append(
                sentence.strip()
            )

    conclusion_sentences = conclusion_sentences[:8]

    # -----------------------------------------------------
    # FORMAT RESULTS
    # -----------------------------------------------------

    def format_items(items):
        if not items:
            return "Not confidently detected"

        return "\n\n".join(
            f"- {item}"
            for item in items
        )

    section_confidence = (
        confidence_label(
            "High",
            "These are exact matches to common academic section headings."
        )
        if section_headings
        else confidence_label(
            "Not detected",
            "No exact match to the built-in section-heading list was found."
        )
    )

    possible_match_note = confidence_label(
        "Possible match",
        "Selected because of explicit wording patterns. This is a retrieval aid, not a semantic judgment."
    )

    result = f"""
## 🆓 Free Basic Extraction

**No AI model was used for this extraction.**

### Reliability key
- 🟢 High confidence = direct structural/pattern evidence
- 🟡 Medium confidence = plausible but should be checked
- 🔴 Low confidence = weak/ambiguous match
- 🟠 Possible match = passage retrieved by keywords/patterns, not interpreted
- ⚪ Not detected = the rule-based extractor did not find a reliable match

### Filename
{filename if filename else "Not available"}

### Probable Title
{title_confidence}

{probable_title}

### Probable Year
{year_confidence}

{probable_year}

### DOI
{doi_confidence}

{doi}

### Abstract
{abstract_confidence}

{abstract}

### Keywords
{keywords_confidence}

{keywords}

### Detected Section Headings
{section_confidence}

{format_items(section_headings)}

### Possible Research Aim / Purpose
{possible_match_note}

{format_items(aim_sentences)}

### Possible Research Questions
{possible_match_note}

{format_items(research_question_sentences)}

### Method-Related Passages
{possible_match_note}

{format_items(method_sentences)}

### Result / Finding-Related Passages
{possible_match_note}

{format_items(result_sentences)}

### Conclusion-Related Passages
{possible_match_note}

{format_items(conclusion_sentences)}

---

### Verification note
This extraction uses text patterns rather than semantic understanding.
Treat all **Possible match** passages as places to inspect in the original source,
not as verified claims about the paper.
"""

    return result


# =========================================================
# SHARED ANALYSIS PROMPTS
# =========================================================

def get_analysis_instructions(mode):

    if mode == "quick":

        max_tokens_claude = (
            1200
            if budget_mode
            else 1800
        )

        max_tokens_openai = (
            1200
            if budget_mode
            else 1800
        )

        instructions = """
You are assisting with a doctoral literature review.

Analyse the academic source efficiently.

STRICT RULES:
1. Use ONLY the supplied source.
2. Do not invent information.
3. If something is not stated, write "Not stated".
4. Distinguish explicit author claims from your own interpretation.
5. Preserve important terminology used by the authors.
6. Only give page numbers when the supplied PAGE markers make them identifiable.
7. Treat quotations, page numbers, numerical findings, and bibliographic details as high-verification items; if uncertain, say so rather than guessing.
8. APA 7 / Lancaster Linguistics quotation rule: whenever you use a direct quotation, immediately add an APA 7 in-text citation in the form (Author, Year, p. X) or (Author, Year, pp. X–Y). Use ONLY author, year, and page information supported by the source and PAGE markers. Never invent missing citation details.
9. If you cannot identify the author, year, or quote page reliably, do not present the wording as a direct quotation; paraphrase it instead and state that the citation details require verification.
10. If you use any direct quotation anywhere in the analysis, add a final section titled "## References" containing the quoted source as a full APA 7 reference, using only bibliographic details that can be identified from the source. If a complete APA 7 reference cannot be verified, give the available details and mark it "[Reference details require verification]" rather than inventing information.

Use these headings:

## 1. Bibliographic Information
Title, author(s), year.

## 2. Purpose
Main aim of the source.

## 3. Research Questions
Explicit research questions, if any.

## 4. Theoretical Framework
Main theories, models, or conceptual frameworks.

## 5. Key Concepts
Important concepts and distinctions.

## 6. Data / Participants
Corpus, dataset, participants, or other material.

## 7. Methodology
Research design and analytical method.

## 8. Main Findings
Most important findings.

## 9. Limitations
Limitations explicitly mentioned by the authors.

## 10. Potential Contribution to a Literature Review
Explain briefly what this source contributes conceptually, theoretically, methodologically, or empirically, based only on the source itself.

Keep this analysis focused and relatively concise.
"""

    else:

        max_tokens_claude = (
            2200
            if budget_mode
            else 4000
        )

        max_tokens_openai = (
            2200
            if budget_mode
            else 4000
        )

        instructions = """
You are an expert academic research assistant helping with a doctoral
literature review.

Analyse the academic source carefully and in depth.

STRICT RULES:

1. Use ONLY information contained in the supplied source.
2. Never invent facts, citations, findings, quotations, or page numbers.
3. If information is not stated, write "Not stated".
4. Preserve terminology used by the authors.
5. Distinguish author claims from your interpretation.
6. Only assign page numbers when PAGE markers make them identifiable.
7. Quotations must be exact.
8. Pay particular attention to conceptual and theoretical distinctions.
9. Treat quotations, page numbers, numerical findings, and bibliographic details as high-verification items; if uncertain, say so rather than guessing.
10. APA 7 / Lancaster Linguistics quotation rule: every direct quotation must be followed immediately by an APA 7 in-text citation in the form (Author, Year, p. X) or (Author, Year, pp. X–Y). Use only citation details and PAGE markers supported by the supplied source. Never guess.
11. For quotations of 40 words or more, format the quotation as a block quote and place the APA 7 citation after the quoted passage.
12. If author, year, or quote page cannot be identified reliably, do not use a direct quotation; paraphrase instead and explicitly note that citation details require verification.
13. If any direct quotation appears anywhere in the analysis, add a final "## References" section with the source formatted in APA 7 style. Use only bibliographic details present in the source. If a complete reference cannot be verified, provide the available details followed by "[Reference details require verification]".

Use the following structure:

## 1. Bibliographic Information
- Title
- Author(s)
- Year
- Source type if identifiable

## 2. Main Purpose
Explain the central purpose.

## 3. Research Questions
List explicit research questions.

## 4. Theoretical Framework
Identify theories, models, frameworks, and traditions.

## 5. Key Concepts
Explain the major concepts.

## 6. Important Definitions
For each:
- concept
- definition
- page if identifiable

## 7. Previous Literature
Identify important scholars, theories, and positions discussed.

## 8. Data / Corpus
Describe data, corpus, or other evidence.

## 9. Participants
Describe participants if applicable.

## 10. Methodology
Explain the research design.

## 11. Analytical Methods
Describe coding, statistics, corpus analysis, conversation analysis,
qualitative analysis, or other procedures.

## 12. Main Findings
Explain the major findings.

## 13. Main Arguments
Identify the central claims made by the authors.

## 14. Limitations
Separate:
- limitations explicitly stated by authors
- limitations you cautiously infer

## 15. Future Research
Identify suggestions for future research.

## 16. Potential Contribution to a Literature Review
Explain what this source contributes conceptually, theoretically, methodologically, or empirically, based only on the source itself.

## 17. Important Quotations
For each:
- exact quotation
- APA 7 in-text citation immediately after the quotation, e.g. (Author, Year, p. X)
- page only when identifiable from PAGE markers
- why it matters
Do not include a direct quotation if you cannot support its author/year/page citation reliably.

## 18. Connections and Debates
Identify:
- competing definitions
- theoretical disagreements
- methodological debates
- conceptual tensions

## 19. Critical Notes
Offer cautious academic observations.
Clearly identify these as interpretation.

## 20. Literature Review Note
Provide a final 150-250 word synthesis note useful for later writing.
"""

    return (
        instructions,
        max_tokens_claude,
        max_tokens_openai
    )


def build_analysis_prompt(
    paper_text,
    mode,
    bibliographic_context=""
):

    instructions, _, _ = (
        get_analysis_instructions(mode)
    )

    zotero_block = ""

    if bibliographic_context:
        zotero_block = f"""
VERIFIED BIBLIOGRAPHIC METADATA FROM ZOTERO:

{bibliographic_context}

Use the Zotero metadata above as the preferred source for author, year,
title, publication, and DOI details. Do not override it with guesses from
PDF extraction.
"""

    return f"""
{instructions}

{zotero_block}

ACADEMIC SOURCE:

{paper_text}
"""


# =========================================================
# CLAUDE ANALYSIS
# =========================================================

def analyse_with_claude(
    paper_text,
    mode,
    output_placeholder=None,
    bibliographic_context=""
):

    if claude is None:
        raise ValueError(
            "Anthropic API key not found."
        )

    prompt = build_analysis_prompt(
        paper_text,
        mode,
        bibliographic_context
    )

    _, max_tokens, _ = (
        get_analysis_instructions(mode)
    )

    collected_text = ""
    chunk_counter = 0

    with claude.messages.stream(

        model="claude-sonnet-4-6",

        max_tokens=max_tokens,

        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ]

    ) as stream:

        for text_chunk in stream.text_stream:

            collected_text += text_chunk

            chunk_counter += 1

            if (
                output_placeholder is not None
                and chunk_counter % 10 == 0
            ):

                output_placeholder.markdown(
                    collected_text + " ▌"
                )

    if output_placeholder is not None:

        output_placeholder.markdown(
            collected_text
        )

    return collected_text


# =========================================================
# OPENAI ANALYSIS
# =========================================================

def analyse_with_openai(
    paper_text,
    mode,
    output_placeholder=None,
    bibliographic_context=""
):

    if openai_client is None:
        raise ValueError(
            "OpenAI API key not found."
        )

    prompt = build_analysis_prompt(
        paper_text,
        mode,
        bibliographic_context
    )

    _, _, max_output_tokens = (
        get_analysis_instructions(mode)
    )

    collected_text = ""
    chunk_counter = 0

    stream = openai_client.responses.create(

        model="gpt-6-astra",

        input=prompt,

        max_output_tokens=max_output_tokens,

        stream=True

    )

    for event in stream:

        if event.type == "response.output_text.delta":

            collected_text += event.delta

            chunk_counter += 1

            if (
                output_placeholder is not None
                and chunk_counter % 10 == 0
            ):

                output_placeholder.markdown(
                    collected_text + " ▌"
                )

    if output_placeholder is not None:

        output_placeholder.markdown(
            collected_text
        )

    return collected_text


# =========================================================
# FREE STRUCTURAL COMPARISON
# =========================================================

def compare_model_outputs(
    claude_analysis,
    openai_analysis,
    mode,
    output_placeholder=None
):
    """
    Free, rule-based comparison.

    No Claude or OpenAI API call is made here.
    It compares the text of the two saved analyses.
    """

    stopwords = {
        "the", "a", "an", "and", "or", "but", "if", "then", "than",
        "of", "to", "in", "on", "for", "from", "with", "by", "as", "at",
        "is", "are", "was", "were", "be", "been", "being", "this", "that",
        "these", "those", "it", "its", "their", "they", "them", "we", "our",
        "you", "your", "not", "no", "yes", "can", "could", "may", "might",
        "will", "would", "should", "has", "have", "had", "do", "does", "did",
        "such", "also", "into", "about", "between", "through", "during",
        "paper", "study", "article", "analysis", "author", "authors",
        "research", "source", "section", "main", "using", "used"
    }

    def normalize_heading(text):
        text = re.sub(r"^#+\s*", "", text).strip()
        text = re.sub(r"^\d+[\.\)]\s*", "", text)
        return text.lower().strip()

    def split_sections(text):
        sections = {}
        current_heading = "General"
        current_lines = []

        for line in text.splitlines():
            if re.match(r"^\s*##\s+", line):
                if current_lines:
                    sections[current_heading] = "\n".join(
                        current_lines
                    ).strip()

                current_heading = re.sub(
                    r"^\s*##\s+",
                    "",
                    line
                ).strip()

                current_lines = []
            else:
                current_lines.append(line)

        if current_lines:
            sections[current_heading] = "\n".join(
                current_lines
            ).strip()

        return sections

    def content_words(text):
        words = re.findall(
            r"\b[A-Za-z][A-Za-z\-']{2,}\b",
            text.lower()
        )

        return {
            word
            for word in words
            if word not in stopwords
        }

    def ordered_keywords(text, other_text, limit=12):
        words = re.findall(
            r"\b[A-Za-z][A-Za-z\-']{2,}\b",
            text.lower()
        )

        other_words = content_words(other_text)
        counts = {}

        for word in words:
            if (
                word not in stopwords
                and word not in other_words
            ):
                counts[word] = counts.get(word, 0) + 1

        ranked = sorted(
            counts.items(),
            key=lambda item: (-item[1], item[0])
        )

        return [
            word
            for word, _ in ranked[:limit]
        ]

    def extract_numbers(text):
        return sorted(
            set(
                re.findall(
                    r"(?<!\w)(?:\d+(?:\.\d+)?%?)(?!\w)",
                    text
                )
            )
        )

    def extract_page_refs(text):
        matches = re.findall(
            r"\b(?:p\.?|pp\.?|page|pages)\s*\d+(?:\s*[-–]\s*\d+)?",
            text,
            flags=re.IGNORECASE
        )

        return sorted(
            set(
                match.strip()
                for match in matches
            )
        )

    def extract_quotes(text):
        quote_patterns = [
            r'“([^”]{10,300})”',
            r'"([^"]{10,300})"',
            r"‘([^’]{10,300})’"
        ]

        quotes = []

        for pattern in quote_patterns:
            quotes.extend(
                re.findall(
                    pattern,
                    text,
                    flags=re.DOTALL
                )
            )

        return sorted(
            set(
                " ".join(
                    quote.split()
                )
                for quote in quotes
            )
        )

    claude_sections = split_sections(
        claude_analysis
    )

    openai_sections = split_sections(
        openai_analysis
    )

    claude_lookup = {
        normalize_heading(heading): (
            heading,
            text
        )
        for heading, text
        in claude_sections.items()
    }

    openai_lookup = {
        normalize_heading(heading): (
            heading,
            text
        )
        for heading, text
        in openai_sections.items()
    }

    all_section_keys = []

    for key in list(claude_lookup) + list(openai_lookup):
        if key not in all_section_keys:
            all_section_keys.append(key)

    section_rows = []

    for key in all_section_keys:
        claude_heading, claude_text = (
            claude_lookup.get(
                key,
                (key.title(), "")
            )
        )

        openai_heading, openai_text = (
            openai_lookup.get(
                key,
                (key.title(), "")
            )
        )

        display_heading = (
            claude_heading
            if key in claude_lookup
            else openai_heading
        )

        claude_words = content_words(
            claude_text
        )

        openai_words = content_words(
            openai_text
        )

        union = (
            claude_words
            | openai_words
        )

        overlap = (
            claude_words
            & openai_words
        )

        similarity = (
            round(
                100
                * len(overlap)
                / len(union)
            )
            if union
            else 100
        )

        section_rows.append(
            {
                "heading": display_heading,
                "similarity": similarity,
                "claude_unique": ordered_keywords(
                    claude_text,
                    openai_text
                ),
                "openai_unique": ordered_keywords(
                    openai_text,
                    claude_text
                ),
            }
        )

    all_claude_words = content_words(
        claude_analysis
    )

    all_openai_words = content_words(
        openai_analysis
    )

    all_union = (
        all_claude_words
        | all_openai_words
    )

    all_overlap = (
        all_claude_words
        & all_openai_words
    )

    overall_similarity = (
        round(
            100
            * len(all_overlap)
            / len(all_union)
        )
        if all_union
        else 100
    )

    claude_numbers = set(
        extract_numbers(
            claude_analysis
        )
    )

    openai_numbers = set(
        extract_numbers(
            openai_analysis
        )
    )

    claude_pages = set(
        extract_page_refs(
            claude_analysis
        )
    )

    openai_pages = set(
        extract_page_refs(
            openai_analysis
        )
    )

    claude_quotes = set(
        extract_quotes(
            claude_analysis
        )
    )

    openai_quotes = set(
        extract_quotes(
            openai_analysis
        )
    )

    claude_only_numbers = sorted(
        claude_numbers
        - openai_numbers
    )

    openai_only_numbers = sorted(
        openai_numbers
        - claude_numbers
    )

    claude_only_pages = sorted(
        claude_pages
        - openai_pages
    )

    openai_only_pages = sorted(
        openai_pages
        - claude_pages
    )

    claude_only_quotes = sorted(
        claude_quotes
        - openai_quotes
    )

    openai_only_quotes = sorted(
        openai_quotes
        - claude_quotes
    )

    result = f"""## 🆓 Free Structural Comparison

**Mode compared:** {mode.title()}

**No AI model was used to create this comparison.**

This is a deterministic text comparison. It measures lexical overlap and
flags structural differences. It does **not** decide which model is correct.

### Overall lexical overlap

**{overall_similarity}%**

This measures overlap in meaningful words after common stopwords are removed.
A lower score does not automatically mean disagreement because the models may
express the same idea using different wording.

### Section-by-section comparison
"""

    for row in section_rows:
        result += (
            f"\n\n#### {row['heading']}\n"
            f"- Lexical overlap: **{row['similarity']}%**\n"
            f"- Claude distinct terms: "
            f"{', '.join(row['claude_unique']) if row['claude_unique'] else 'None detected'}\n"
            f"- OpenAI distinct terms: "
            f"{', '.join(row['openai_unique']) if row['openai_unique'] else 'None detected'}"
        )

    result += "\n\n### Numerical differences to verify"

    if (
        claude_only_numbers
        or openai_only_numbers
    ):
        result += (
            "\n- Numbers appearing only in Claude: "
            + (
                ", ".join(
                    claude_only_numbers
                )
                if claude_only_numbers
                else "None"
            )
        )

        result += (
            "\n- Numbers appearing only in OpenAI: "
            + (
                ", ".join(
                    openai_only_numbers
                )
                if openai_only_numbers
                else "None"
            )
        )
    else:
        result += (
            "\nNo numerical differences were detected."
        )

    result += "\n\n### Page-reference differences to verify"

    if (
        claude_only_pages
        or openai_only_pages
    ):
        result += (
            "\n- Page references appearing only in Claude: "
            + (
                ", ".join(
                    claude_only_pages
                )
                if claude_only_pages
                else "None"
            )
        )

        result += (
            "\n- Page references appearing only in OpenAI: "
            + (
                ", ".join(
                    openai_only_pages
                )
                if openai_only_pages
                else "None"
            )
        )
    else:
        result += (
            "\nNo page-reference differences were detected."
        )

    result += "\n\n### Exact-quotation differences to verify"

    if (
        claude_only_quotes
        or openai_only_quotes
    ):
        if claude_only_quotes:
            result += (
                "\n\n**Quotes appearing only in Claude:**\n"
                + "\n".join(
                    f"- “{quote}”"
                    for quote
                    in claude_only_quotes[:10]
                )
            )

        if openai_only_quotes:
            result += (
                "\n\n**Quotes appearing only in OpenAI:**\n"
                + "\n".join(
                    f"- “{quote}”"
                    for quote
                    in openai_only_quotes[:10]
                )
            )
    else:
        result += (
            "\nNo exact-quotation differences were detected."
        )

    result += """

### How to interpret this comparison

- **High lexical overlap** means the analyses use many of the same meaningful
  terms. It does not prove factual agreement.
- **Distinct terms** show wording that appears in one analysis and not the
  other. They are clues for review, not proof that one model missed an idea.
- **Different numbers, page references, and quotations** should be checked
  directly against the original paper.
- This comparison intentionally avoids judgments such as “Claude is correct”
  or “OpenAI misunderstood the author.”

### Verification status

🟢 **Deterministic:** the calculations above are produced by Python rules.

🟠 **Human review still needed:** semantic equivalence, contradictions,
theoretical interpretation, and factual correctness must be checked against
the original source.
"""

    if output_placeholder is not None:
        output_placeholder.markdown(
            result
        )

    return result


def compare_outputs_with_ai(
    claude_text,
    openai_text,
    provider,
    mode="quick",
    output_placeholder=None,
    context_label="AI outputs"
):
    """Optional paid semantic comparison of two existing AI outputs."""

    if not claude_text or not openai_text:
        raise ValueError(
            "Both Claude and OpenAI outputs are required before comparison."
        )

    prompt = f"""
Compare the following two AI-generated academic outputs about the same material.

CONTEXT:
{context_label}

MODE:
{mode.title()}

Use ONLY the two outputs below. Do not introduce outside knowledge.
Identify substantive similarities, meaningful differences, and claims that
appear in only one output. Do not assume different wording means disagreement.
Do not decide factual correctness unless it follows directly from the outputs.
Flag differences in numbers, quotations, page references, and conclusions.

Use this structure:

## Overall Agreement
## Main Similarities
## Main Differences
## Unique Claims to Verify
## Numbers / Quotations / Page References to Check
## Bottom Line

CLAUDE OUTPUT:
{claude_text}

OPENAI OUTPUT:
{openai_text}
"""

    collected_text = ""
    chunk_counter = 0
    max_tokens = 1600 if mode == "quick" else 2800

    if provider == "Claude":
        if claude is None:
            raise ValueError("Anthropic API key not found.")

        with claude.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}]
        ) as stream:
            for text_chunk in stream.text_stream:
                collected_text += text_chunk
                chunk_counter += 1
                if output_placeholder is not None and chunk_counter % 10 == 0:
                    output_placeholder.markdown(collected_text + " ▌")

    else:
        if openai_client is None:
            raise ValueError("OpenAI API key not found.")

        stream = openai_client.responses.create(
            model="gpt-6-astra",
            input=prompt,
            max_output_tokens=max_tokens,
            stream=True
        )

        for event in stream:
            if event.type == "response.output_text.delta":
                collected_text += event.delta
                chunk_counter += 1
                if output_placeholder is not None and chunk_counter % 10 == 0:
                    output_placeholder.markdown(collected_text + " ▌")

    if output_placeholder is not None:
        output_placeholder.markdown(collected_text)

    return collected_text



# =========================================================
# FREE LIBRARY RETRIEVAL
# =========================================================

def tokenize_for_retrieval(text):
    stopwords = {
        "the", "a", "an", "and", "or", "but", "if", "then", "than",
        "of", "to", "in", "on", "for", "from", "with", "by", "as", "at",
        "is", "are", "was", "were", "be", "been", "being", "this", "that",
        "these", "those", "it", "its", "their", "they", "them", "we", "our",
        "you", "your", "not", "no", "yes", "can", "could", "may", "might",
        "will", "would", "should", "has", "have", "had", "do", "does", "did",
        "such", "also", "into", "about", "between", "through", "during",
        "what", "which", "who", "whom", "whose", "how", "why", "when",
        "where", "whether"
    }

    words = re.findall(
        r"\b[A-Za-z][A-Za-z\-']{2,}\b",
        text.lower()
    )

    return [
        word
        for word in words
        if word not in stopwords
    ]


def split_paper_into_chunks(
    full_text,
    max_chars=2200
):
    page_pattern = re.compile(
        r"--- PAGE (\d+) ---"
    )

    matches = list(
        page_pattern.finditer(
            full_text
        )
    )

    chunks = []

    if not matches:
        blocks = re.split(
            r"\n\s*\n",
            full_text
        )

        buffer = ""

        for block in blocks:
            block = block.strip()

            if not block:
                continue

            if (
                buffer
                and len(buffer) + len(block) > max_chars
            ):
                chunks.append(
                    {
                        "page": "Unknown",
                        "text": buffer.strip()
                    }
                )
                buffer = block
            else:
                buffer = (
                    f"{buffer}\n\n{block}".strip()
                )

        if buffer:
            chunks.append(
                {
                    "page": "Unknown",
                    "text": buffer.strip()
                }
            )

        return chunks


    for index, match in enumerate(
        matches
    ):
        page_number = (
            match.group(1)
        )

        start = match.end()

        end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(full_text)
        )

        page_text = (
            full_text[start:end]
            .strip()
        )

        blocks = re.split(
            r"\n\s*\n",
            page_text
        )

        buffer = ""

        for block in blocks:
            block = block.strip()

            if not block:
                continue

            if (
                buffer
                and len(buffer) + len(block) > max_chars
            ):
                chunks.append(
                    {
                        "page": page_number,
                        "text": buffer.strip()
                    }
                )
                buffer = block
            else:
                buffer = (
                    f"{buffer}\n\n{block}".strip()
                )

        if buffer:
            chunks.append(
                {
                    "page": page_number,
                    "text": buffer.strip()
                }
            )

    return chunks


def score_chunk_for_question(
    question,
    chunk_text
):
    query_terms = (
        tokenize_for_retrieval(
            question
        )
    )

    if not query_terms:
        return 0

    text_lower = (
        chunk_text.lower()
    )

    score = 0

    unique_terms = set(
        query_terms
    )

    for term in unique_terms:
        occurrences = len(
            re.findall(
                rf"\b{re.escape(term)}\b",
                text_lower
            )
        )

        if occurrences:
            score += 2
            score += min(
                occurrences,
                5
            )

    for i in range(
        len(query_terms) - 1
    ):
        phrase = (
            query_terms[i]
            + " "
            + query_terms[i + 1]
        )

        if phrase in text_lower:
            score += 5

    matched_unique = sum(
        1
        for term in unique_terms
        if re.search(
            rf"\b{re.escape(term)}\b",
            text_lower
        )
    )

    score += matched_unique

    return score


def retrieve_library_evidence(
    papers,
    question,
    chunks_per_paper=3,
    max_total_chunks=30,
    chunk_size=2200
):
    evidence = []

    for paper in papers:
        paper_id = paper[0]
        filename = paper[1]
        folder = paper[2] or "Unsorted"
        source_type = paper[3] or "Article"
        full_text = paper[4] or ""

        zotero_metadata_json = (
            paper[5]
            if len(paper) > 5
            else None
        )

        zotero_draft_json = (
            paper[6]
            if len(paper) > 6
            else None
        )

        citation_info = (
            build_saved_citation_info(
                zotero_metadata_json,
                zotero_draft_json,
                source_type,
                filename
            )
        )

        chunks = split_paper_into_chunks(
            full_text,
            max_chars=chunk_size
        )

        scored_chunks = []

        for chunk in chunks:
            score = score_chunk_for_question(
                question,
                chunk["text"]
            )

            if score > 0:
                scored_chunks.append(
                    {
                        "paper_id": paper_id,
                        "filename": filename,
                        "folder": folder,
                        "source_type": source_type,
                        "page": chunk["page"],
                        "score": score,
                        "text": chunk["text"],
                        "citation_title": citation_info.get(
                            "title",
                            filename
                        ),
                        "citation_in_text": citation_info.get(
                            "in_text",
                            ""
                        ),
                        "citation_reference": citation_info.get(
                            "reference",
                            ""
                        ),
                        "metadata_context": citation_info.get(
                            "metadata_context",
                            ""
                        ),
                        "metadata_source": citation_info.get(
                            "metadata_source",
                            ""
                        ),
                    }
                )

        scored_chunks.sort(
            key=lambda item: item["score"],
            reverse=True
        )

        evidence.extend(
            scored_chunks[:chunks_per_paper]
        )

    evidence.sort(
        key=lambda item: item["score"],
        reverse=True
    )

    return evidence[:max_total_chunks]


def build_library_evidence_text(
    evidence
):
    parts = []

    for index, item in enumerate(
        evidence,
        start=1
    ):
        parts.append(
            f"""
[EVIDENCE {index}]
Source title: {item.get('citation_title') or item['filename']}
APA in-text citation to use: {item.get('citation_in_text') or '[Citation metadata incomplete]'}
APA reference to use: {item.get('citation_reference') or '[Reference metadata incomplete]'}
Metadata source: {item.get('metadata_source') or 'Unknown'}
Folder: {item['folder']}
Source type: {item['source_type']}
Extracted PDF page marker: {item['page']}
Local relevance score: {item['score']}

Bibliographic metadata:
{item.get('metadata_context') or 'Metadata unavailable'}

Internal filename — DO NOT cite this academically:
{item['filename']}

Evidence text:
{item['text']}
""".strip()
        )

    return "\n\n".join(parts)


# =========================================================
# LIBRARY-WIDE SYNTHESIS
# =========================================================

def synthesize_library_answer(
    question,
    evidence,
    provider,
    output_placeholder=None,
    scope="Entire Library",
    source_label="",
    mode="quick"
):
    if not evidence:
        raise ValueError(
            "No relevant passages were found in the selected search scope."
        )

    evidence_text = (
        build_library_evidence_text(
            evidence
        )
    )

    if scope == "Individual Paper":
        scope_instruction = (
            "The researcher selected ONE saved source. Answer using only "
            "evidence retrieved from that source. Identify and cite it using "
            "the bibliographic metadata supplied in the evidence, never the "
            "internal PDF filename."
        )
    else:
        scope_instruction = (
            "The researcher selected the ENTIRE saved literature library. "
            "Synthesize across the retrieved evidence from all relevant sources."
        )

    # Keep Quick answers genuinely short. Deep answers can be fuller,
    # but should still avoid unnecessary repetition.
    if mode == "deep":

        if scope == "Individual Paper":
            structure = """
Use this compact structure:

## Answer
Give the direct answer first.

## Evidence and Nuance
Explain the strongest relevant evidence from the selected paper, including
important qualifications or tensions.

## Unclear / Not Established
Briefly state anything the source does not establish.

## Source
Name the selected source.
"""
        else:
            structure = """
Use this compact structure:

## Answer
Give the direct answer first.

## Synthesis
Explain the strongest cross-source patterns, agreements, and important
differences together rather than repeating them in separate sections.

## Gaps / Uncertainty
Briefly state what cannot be established confidently.

## Sources Used
List only the sources that materially contributed.
"""

        length_instruction = """
LENGTH:
Aim for roughly 700–900 words maximum.
Be selective. Do not repeat the same point in multiple sections.
Prefer dense, academically useful prose over exhaustive coverage.
"""
        answer_max_tokens = 1200

    else:

        if scope == "Individual Paper":
            structure = """
Use this structure:

## Answer
Answer the question directly in 1–3 short paragraphs.

## Key Evidence
Give only the 2–4 most relevant points from the selected paper.

## Source
Name the selected source.
"""
        else:
            structure = """
Use this structure:

## Answer
Answer the question directly in 1–3 short paragraphs.

## Key Evidence
Give only the 3–5 most important cross-source points. Combine agreements
and differences instead of creating many separate sections.

## Sources Used
List only the sources that materially contributed.
"""

        length_instruction = """
LENGTH:
Keep the whole answer concise: roughly 300–450 words maximum.
Prioritize the answer to the question. Omit background, repetition, and
minor details unless essential.
"""
        answer_max_tokens = 650

    prompt = f"""
You are assisting with a doctoral literature review.

{scope_instruction}

Answer the researcher's question using ONLY the evidence passages supplied below.

RESEARCH QUESTION:
{question}

STRICT RULES:

1. Use only the supplied evidence.
2. Do not add general knowledge that is not supported by the evidence.
3. Do not invent citations, page numbers, quotations, findings, or authors.
4. NEVER use the internal PDF filename as the academic citation or source name.
5. Each evidence block supplies an "APA in-text citation to use". Use that
   author-year citation for claims supported by that source.
6. Each evidence block supplies an "APA reference to use". Preserve that
   bibliographic information rather than reconstructing it from the filename.
7. If a page marker is supplied, it is only an extracted PDF page marker. Do not
   present it as a publisher page number unless the evidence itself establishes that.
8. If the available evidence is insufficient to answer part of the question,
   say so briefly.
9. Prioritize the most relevant conceptual distinctions, definitions,
   theoretical positions, findings, methods, and disagreements.
10. Every substantive paragraph should contain the appropriate APA author-year
    in-text citation(s) from the evidence it uses.
11. For direct quotations, use a page number only when a reliable publisher page
    number is available. Otherwise paraphrase rather than invent a page citation.
12. Do not invent author names, years, journal details, DOIs, or other reference data.
13. End every answer with a short "## References" section containing the supplied
    APA reference for each source materially used in the answer.
14. Approved Zotero metadata has priority. If a source is not yet linked to
    Zotero, use the saved app metadata supplied in the evidence.
15. Do not repeat the same claim in several sections.
16. Do not add a long verification checklist unless there is a specific
    uncertainty that materially affects the answer.

{structure}

{length_instruction}

SEARCH EVIDENCE:

{evidence_text}
"""

    collected_text = ""
    chunk_counter = 0

    if provider == "Claude":

        if claude is None:
            raise ValueError(
                "Anthropic API key not found."
            )

        with claude.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=answer_max_tokens,
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        ) as stream:

            for text_chunk in stream.text_stream:
                collected_text += text_chunk
                chunk_counter += 1

                if (
                    output_placeholder is not None
                    and chunk_counter % 10 == 0
                ):
                    output_placeholder.markdown(
                        collected_text + " ▌"
                    )

    else:

        if openai_client is None:
            raise ValueError(
                "OpenAI API key not found."
            )

        stream = (
            openai_client.responses.create(
                model="gpt-6-astra",
                input=prompt,
                max_output_tokens=answer_max_tokens,
                stream=True
            )
        )

        for event in stream:

            if (
                event.type
                == "response.output_text.delta"
            ):
                collected_text += event.delta
                chunk_counter += 1

                if (
                    output_placeholder is not None
                    and chunk_counter % 10 == 0
                ):
                    output_placeholder.markdown(
                        collected_text + " ▌"
                    )

    if output_placeholder is not None:
        output_placeholder.markdown(
            collected_text
        )

    return (
        collected_text,
        evidence_text
    )



# =========================================================
# DATABASE HELPERS
# =========================================================

def update_saved_analysis(
    paper_id,
    provider,
    mode,
    analysis_text
):

    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    if (
        provider == "Claude"
        and mode == "quick"
    ):

        text_column = (
            "claude_quick_analysis"
        )

        date_column = (
            "claude_quick_date"
        )

    elif (
        provider == "Claude"
        and mode == "deep"
    ):

        text_column = (
            "claude_deep_analysis"
        )

        date_column = (
            "claude_deep_date"
        )

    elif (
        provider == "OpenAI"
        and mode == "quick"
    ):

        text_column = (
            "openai_quick_analysis"
        )

        date_column = (
            "openai_quick_date"
        )

    else:

        text_column = (
            "openai_deep_analysis"
        )

        date_column = (
            "openai_deep_date"
        )

    cursor.execute(

        f"""
        UPDATE papers
        SET
            {text_column} = ?,
            {date_column} = ?
        WHERE id = ?
        """,

        (
            analysis_text,
            now,
            paper_id
        )

    )

    conn.commit()


def update_comparison(
    paper_id,
    mode,
    comparison_text
):

    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    if mode == "quick":

        text_column = (
            "comparison_quick"
        )

        date_column = (
            "comparison_quick_date"
        )

    else:

        text_column = (
            "comparison_deep"
        )

        date_column = (
            "comparison_deep_date"
        )

    cursor.execute(

        f"""
        UPDATE papers
        SET
            {text_column} = ?,
            {date_column} = ?
        WHERE id = ?
        """,

        (
            comparison_text,
            now,
            paper_id
        )

    )

    conn.commit()



# =========================================================
# MAIN WORKSPACE TABS
# =========================================================

upload_tab, library_tab, ask_tab = st.tabs(
    [
        "➕ Add to Library",
        "📚 Library and Reference Entry",
        "🔎 Ask My Literature",
    ]
)


with upload_tab:
    # =========================================================
    # UPLOAD / ADD NEW SOURCE
    # =========================================================

    st.subheader("Add a New Source")

    st.caption(
        "Upload the PDF first. After reading it, you can analyse it and save it to your literature library."
    )

    if "pdf_uploader_version" not in st.session_state:
        st.session_state[
            "pdf_uploader_version"
        ] = 0

    uploaded_file = st.file_uploader(
        "Upload a research paper",
        type=["pdf"],
        key=(
            "pdf_uploader_"
            + str(
                st.session_state[
                    "pdf_uploader_version"
                ]
            )
        )
    )


    # =========================================================
    # READ PDF
    # =========================================================

    if st.button("Read PDF"):

        if uploaded_file:

            reader = PdfReader(
                uploaded_file
            )

            text = ""

            total_pages = len(
                reader.pages
            )

            for page_number, page in enumerate(
                reader.pages,
                start=1
            ):

                page_text = (
                    page.extract_text()
                )

                if page_text:

                    text += (
                        f"\n\n"
                        f"--- PAGE {page_number} ---"
                        f"\n\n"
                        f"{page_text}"
                    )

            st.session_state[
                "paper_text"
            ] = text

            st.session_state[
                "paper_filename"
            ] = uploaded_file.name

            st.session_state[
                "paper_pages"
            ] = total_pages


            for key in [

                "new_claude_quick",
                "new_claude_deep",
                "new_openai_quick",
                "new_openai_deep",
                "new_free_extraction",
                "new_python_comparison",
                "new_ai_comparison",
                "new_ai_compare_pending",
                "new_metadata_signature",
                "new_prepared_zotero_draft",
                "new_library_title",
                "new_metadata_error"

            ]:

                st.session_state.pop(
                    key,
                    None
                )


            st.success(
                "PDF uploaded and read successfully."
            )

            st.subheader(
                "Extracted text preview"
            )

            st.write(
                text[:1000]
            )

        else:

            st.warning(
                "Please upload a PDF."
            )


    # =========================================================
    # PAPER IN MEMORY
    # =========================================================

    if "paper_text" in st.session_state:

        st.info(

            f"Article is currently in memory: "
            f"{len(st.session_state['paper_text']):,} characters | "
            f"{st.session_state.get('paper_pages', 'Unknown')} pages"

        )



    # =========================================================
    # RESEARCH QUESTION
    # =========================================================

    # =========================================================
    # NEW / UNSAVED PAPER AI ANALYSIS
    # =========================================================

    if "paper_text" in st.session_state:

        st.subheader(
            "Paper Analysis"
        )

        st.caption(
            "Free Basic Extraction uses only Python rules and costs no API tokens."
        )

        if st.button(
            "🆓 Extract Basic Information",
            key="new_free_extraction_button"
        ):
            free_result = extract_basic_information(
                st.session_state["paper_text"],
                st.session_state.get(
                    "paper_filename",
                    ""
                )
            )

            st.session_state[
                "new_free_extraction"
            ] = free_result

        if "new_free_extraction" in st.session_state:

            with st.expander(
                "🆓 View Free Basic Extraction",
                expanded=True
            ):
                st.markdown(
                    st.session_state[
                        "new_free_extraction"
                    ]
                )

        st.markdown("### AI Analysis")

        budget_ready_new = (
            (not budget_mode)
            or (
                "new_free_extraction"
                in st.session_state
            )
        )

        provider = st.selectbox(

            "Choose AI provider:",

            [
                "Claude",
                "OpenAI"
            ],

            key="new_provider"

        )


        quick_col, deep_col = (
            st.columns(2)
        )


        with quick_col:

            if st.button(

                f"⚡ Quick Analysis with {provider}",

                key="new_quick",
                disabled=not budget_ready_new,
                use_container_width=True

            ):

                try:

                    output = st.empty()


                    if provider == "Claude":

                        result = (
                            analyse_with_claude(

                                st.session_state[
                                    "paper_text"
                                ],

                                "quick",

                                output

                            )
                        )

                        st.session_state[
                            "new_claude_quick"
                        ] = result

                    else:

                        result = (
                            analyse_with_openai(

                                st.session_state[
                                    "paper_text"
                                ],

                                "quick",

                                output

                            )
                        )

                        st.session_state[
                            "new_openai_quick"
                        ] = result


                except Exception as error:

                    st.error(
                        f"{provider} analysis failed: {error}"
                    )


        with deep_col:

            if st.button(

                f"🧠 Deep Analysis with {provider}",

                key="new_deep",
                disabled=(
                    budget_mode
                    or not budget_ready_new
                ),
                use_container_width=True

            ):

                try:

                    output = st.empty()


                    if provider == "Claude":

                        result = (
                            analyse_with_claude(

                                st.session_state[
                                    "paper_text"
                                ],

                                "deep",

                                output

                            )
                        )

                        st.session_state[
                            "new_claude_deep"
                        ] = result

                    else:

                        result = (
                            analyse_with_openai(

                                st.session_state[
                                    "paper_text"
                                ],

                                "deep",

                                output

                            )
                        )

                        st.session_state[
                            "new_openai_deep"
                        ] = result


                except Exception as error:

                    st.error(
                        f"{provider} analysis failed: {error}"
                    )


        # Keep the two analysis choices visible at all times.
        # Budget Mode changes what is enabled, not what the user can see.
        if budget_mode:
            if not budget_ready_new:
                st.caption(
                    "Run Free Basic Extraction first to enable Quick Analysis. "
                    "Deep Analysis is available when Budget Mode is off."
                )
            else:
                st.caption(
                    "Budget Mode is on: Quick Analysis is available; "
                    "turn Budget Mode off to use Deep Analysis."
                )


    # =========================================================
    # TEMPORARY ANALYSIS DISPLAY
    # =========================================================

    temporary_analysis_labels = [

        (
            "new_claude_quick",
            "Claude ⚡ Quick Analysis"
        ),

        (
            "new_claude_deep",
            "Claude 🧠 Deep Analysis"
        ),

        (
            "new_openai_quick",
            "OpenAI ⚡ Quick Analysis"
        ),

        (
            "new_openai_deep",
            "OpenAI 🧠 Deep Analysis"
        ),

    ]


    for key, label in temporary_analysis_labels:

        if key in st.session_state:

            with st.expander(
                f"View {label}",
                expanded=False
            ):

                show_ai_verification_warning()

                st.markdown(
                    st.session_state[
                        key
                    ]
                )


    # =========================================================
    # COMPARE NEW-PAPER AI ANALYSES
    # =========================================================

    if "paper_text" in st.session_state:

        st.markdown("### Compare Claude and OpenAI")

        compare_depth_label = st.radio(
            "Comparison depth:",
            ["⚡ Quick", "🧠 Deep"],
            horizontal=True,
            key="new_compare_depth"
        )

        compare_depth = (
            "quick"
            if compare_depth_label == "⚡ Quick"
            else "deep"
        )

        if compare_depth == "quick":
            compare_claude = st.session_state.get("new_claude_quick")
            compare_openai = st.session_state.get("new_openai_quick")
        else:
            compare_claude = st.session_state.get("new_claude_deep")
            compare_openai = st.session_state.get("new_openai_deep")

        missing_models = []
        if not compare_claude:
            missing_models.append("Claude")
        if not compare_openai:
            missing_models.append("OpenAI")

        if missing_models:
            st.caption(
                "Generate the "
                + compare_depth.title()
                + " analysis with "
                + " and ".join(missing_models)
                + " before comparing."
            )

        free_compare_col, ai_compare_col = st.columns(2)

        with free_compare_col:
            if st.button(
                "🆓 Compare with Python",
                key="new_compare_python",
                disabled=bool(missing_models),
                use_container_width=True
            ):
                st.session_state["new_python_comparison"] = (
                    compare_model_outputs(
                        compare_claude,
                        compare_openai,
                        compare_depth
                    )
                )

        with ai_compare_col:
            if st.button(
                "🤖 Compare with AI",
                key="new_compare_ai",
                disabled=bool(missing_models) or budget_mode,
                use_container_width=True
            ):
                st.session_state["new_ai_compare_pending"] = True

        if budget_mode:
            st.caption(
                "Python comparison is free. Turn Budget Mode off to use AI comparison."
            )

        if st.session_state.get("new_ai_compare_pending", False):
            compare_ai_provider = st.selectbox(
                "AI for comparison:",
                ["Claude", "OpenAI"],
                key="new_compare_ai_provider"
            )

            if st.button(
                "Run AI comparison",
                key="run_new_ai_comparison"
            ):
                try:
                    compare_placeholder = st.empty()
                    st.session_state["new_ai_comparison"] = (
                        compare_outputs_with_ai(
                            compare_claude,
                            compare_openai,
                            compare_ai_provider,
                            mode=compare_depth,
                            output_placeholder=compare_placeholder,
                            context_label="two analyses of the currently uploaded paper"
                        )
                    )
                    st.session_state["new_ai_compare_pending"] = False
                except Exception as error:
                    st.error(f"AI comparison failed: {error}")

        if st.session_state.get("new_python_comparison"):
            with st.expander("View free Python comparison", expanded=True):
                st.markdown(st.session_state["new_python_comparison"])

        if st.session_state.get("new_ai_comparison"):
            with st.expander("View AI comparison", expanded=True):
                st.markdown(st.session_state["new_ai_comparison"])


    # =========================================================
    # SAVE NEW PAPER
    # =========================================================

    if "paper_text" in st.session_state:

        st.subheader(
            "Save paper to Library"
        )

        selected_folder = st.selectbox(
            "Choose a folder:",
            folders,
            key="save_folder"
        )

        selected_source_type = st.selectbox(
            "Choose source type:",
            source_types,
            key="save_source_type"
        )

        source_type_help = {
            "Article": (
                "Journal Article → journal, volume, issue, pages, DOI."
            ),
            "Book": (
                "Book → publisher, place, edition, ISBN, number of pages."
            ),
            "Thesis": (
                "Thesis → university, thesis type, place, DOI/URL when available."
            ),
        }

        st.caption(
            source_type_help[
                selected_source_type
            ]
        )

        # -----------------------------------------------------
        # PREPARE TITLE + REFERENCE METADATA BEFORE SAVING
        # -----------------------------------------------------
        # The title retrieved for the Zotero/reference draft becomes the
        # Library name. It is always editable before the paper is saved.
        original_pdf_filename = (
            st.session_state.get(
                "paper_filename",
                "Unknown"
            )
        )

        metadata_signature = (
            f"{original_pdf_filename}|"
            f"{selected_source_type}|"
            f"{len(st.session_state['paper_text'])}"
        )

        if (
            st.session_state.get(
                "new_metadata_signature"
            )
            != metadata_signature
        ):
            try:
                with st.spinner(
                    "Preparing title and reference metadata..."
                ):
                    prepared_zotero_draft = (
                        extract_zotero_metadata_free(
                            st.session_state[
                                "paper_text"
                            ],
                            original_pdf_filename,
                            selected_source_type
                        )
                    )

                prepared_zotero_draft = (
                    normalize_zotero_draft(
                        prepared_zotero_draft,
                        selected_source_type
                    )
                )

                st.session_state[
                    "new_prepared_zotero_draft"
                ] = prepared_zotero_draft

                st.session_state[
                    "new_library_title"
                ] = (
                    prepared_zotero_draft.get(
                        "title",
                        ""
                    )
                    or ""
                ).strip()

                st.session_state[
                    "new_metadata_error"
                ] = ""

            except Exception as error:
                # Metadata lookup failure should never block the user from
                # saving the paper. They can type the title manually.
                st.session_state[
                    "new_prepared_zotero_draft"
                ] = normalize_zotero_draft(
                    {
                        "title": ""
                    },
                    selected_source_type
                )

                st.session_state[
                    "new_library_title"
                ] = ""

                st.session_state[
                    "new_metadata_error"
                ] = str(
                    error
                )

            st.session_state[
                "new_metadata_signature"
            ] = metadata_signature

        library_title = st.text_input(
            "Title used in Library:",
            key="new_library_title",
            placeholder=(
                "Enter the article/book/thesis title manually "
                "if it could not be retrieved"
            ),
            help=(
                "The app fills this from the same metadata prepared for "
                "the Zotero/reference entry. You can correct it before saving."
            )
        )

        if st.session_state.get(
            "new_metadata_error"
        ):
            st.caption(
                "The title could not be retrieved automatically. "
                "Enter or correct it manually before saving."
            )
        elif library_title.strip():
            st.caption(
                "Retrieved from the prepared reference metadata. "
                "You can edit the title before saving."
            )
        else:
            st.caption(
                "No usable title was retrieved. "
                "Please enter the title manually."
            )

        if st.button(
            "Save paper to library"
        ):

            clean_library_title = (
                library_title.strip()
            )

            if not clean_library_title:
                st.warning(
                    "Please enter the paper title before saving."
                )

            else:
                now = datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                )

                # Keep the reference draft and Library title synchronized.
                new_zotero_draft = (
                    st.session_state.get(
                        "new_prepared_zotero_draft"
                    )
                    or {}
                )

                new_zotero_draft = (
                    normalize_zotero_draft(
                        new_zotero_draft,
                        selected_source_type
                    )
                )

                new_zotero_draft[
                    "title"
                ] = clean_library_title

                cursor.execute(
                    """
                    INSERT INTO papers (
                        filename,
                        research_question,
                        full_text,
                        date_added,
                        folder,
                        source_type,

                        claude_quick_analysis,
                        claude_quick_date,
                        claude_deep_analysis,
                        claude_deep_date,

                        openai_quick_analysis,
                        openai_quick_date,
                        openai_deep_analysis,
                        openai_deep_date,

                        free_extraction,
                        free_extraction_date
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        # The Library now stores/displays the bibliographic title,
                        # not the random uploaded PDF filename.
                        clean_library_title,

                        None,

                        st.session_state[
                            "paper_text"
                        ],

                        now,

                        selected_folder,

                        selected_source_type,

                        st.session_state.get(
                            "new_claude_quick"
                        ),

                        now
                        if st.session_state.get(
                            "new_claude_quick"
                        )
                        else None,

                        st.session_state.get(
                            "new_claude_deep"
                        ),

                        now
                        if st.session_state.get(
                            "new_claude_deep"
                        )
                        else None,

                        st.session_state.get(
                            "new_openai_quick"
                        ),

                        now
                        if st.session_state.get(
                            "new_openai_quick"
                        )
                        else None,

                        st.session_state.get(
                            "new_openai_deep"
                        ),

                        now
                        if st.session_state.get(
                            "new_openai_deep"
                        )
                        else None,

                        st.session_state.get(
                            "new_free_extraction"
                        ),

                        now
                        if st.session_state.get(
                            "new_free_extraction"
                        )
                        else None,
                    )
                )

                conn.commit()

                new_paper_id = (
                    cursor.lastrowid
                )

                # Save the prepared/edited metadata locally for the Zotero
                # approval workflow. This still writes NOTHING to Zotero.
                save_zotero_draft(
                    new_paper_id,
                    new_zotero_draft
                )

                for key in [
                    "paper_text",
                    "paper_filename",
                    "paper_pages",
                    "new_claude_quick",
                    "new_claude_deep",
                    "new_openai_quick",
                    "new_openai_deep",
                    "new_free_extraction",
                    "new_python_comparison",
                    "new_ai_comparison",
                    "new_ai_compare_pending",
                    "new_metadata_signature",
                    "new_prepared_zotero_draft",
                    "new_library_title",
                    "new_metadata_error"
                ]:

                    st.session_state.pop(
                        key,
                        None
                    )

                # Reset the PDF uploader after a successful save so the next
                # source can be uploaded immediately.
                st.session_state[
                    "pdf_uploader_version"
                ] = (
                    st.session_state.get(
                        "pdf_uploader_version",
                        0
                    )
                    + 1
                )

                st.success(
                    f"Saved to the Library as: {clean_library_title}. "
                    "Its reference metadata was saved locally, but nothing is "
                    "written to Zotero until you approve it."
                )

                st.rerun()


with ask_tab:
    # =========================================================
    # ASK THE ENTIRE SAVED LIBRARY
    # =========================================================

    st.subheader(
        "Ask My Literature"
    )

    # Search design note:
    # Choose whether to search across your entire Library or ask a question
    # about one individual saved source.

    search_scope = st.selectbox(
        "Search:",
        [
            "Entire Library",
            "Individual Paper"
        ],
        key="literature_search_scope"
    )

    cursor.execute(
        """
        SELECT
            id,
            filename,
            folder,
            source_type
        FROM papers
        ORDER BY
            folder,
            source_type,
            filename
        """
    )

    searchable_papers = (
        cursor.fetchall()
    )

    selected_paper_id = None
    selected_paper_label = ""

    if search_scope == "Individual Paper":

        if searchable_papers:

            # -------------------------------------------------
            # TREE-STYLE LIBRARY BROWSER
            # Folder → Source type → Search → Paper
            # -------------------------------------------------

            available_folders = sorted(
                {
                    (
                        paper[2]
                        or "Unsorted"
                    )
                    for paper in searchable_papers
                },
                key=str.lower
            )

            selected_search_folder = st.selectbox(
                "📁 Folder:",
                [
                    "All folders"
                ]
                + available_folders,
                key="individual_paper_folder_filter"
            )

            papers_after_folder = [
                paper
                for paper in searchable_papers
                if (
                    selected_search_folder
                    == "All folders"
                    or (
                        paper[2]
                        or "Unsorted"
                    )
                    == selected_search_folder
                )
            ]

            available_source_types = [
                source_type
                for source_type in [
                    "Article",
                    "Book",
                    "Thesis"
                ]
                if any(
                    (
                        paper[3]
                        or "Article"
                    )
                    == source_type
                    for paper in papers_after_folder
                )
            ]

            selected_search_source_type = st.selectbox(
                "📄 Source type:",
                [
                    "All source types"
                ]
                + available_source_types,
                key="individual_paper_type_filter"
            )

            papers_after_tree = [
                paper
                for paper in papers_after_folder
                if (
                    selected_search_source_type
                    == "All source types"
                    or (
                        paper[3]
                        or "Article"
                    )
                    == selected_search_source_type
                )
            ]

            # The paper dropdown itself is searchable in Streamlit.
            # Keep folder/type as the tree filters and let the user type
            # directly inside the final dropdown to find a paper.
            filtered_papers = papers_after_tree

            if filtered_papers:

                paper_options = {}

                for (
                    search_paper_id,
                    search_filename,
                    search_folder,
                    search_source_type
                ) in filtered_papers:

                    display_label = (
                        search_filename
                    )

                    # Keep the title readable while still disambiguating
                    # duplicate titles when necessary.
                    if display_label in paper_options:
                        display_label = (
                            f"{search_filename} "
                            f"— {search_folder or 'Unsorted'} / "
                            f"{search_source_type or 'Article'} "
                            f"[#{search_paper_id}]"
                        )

                    paper_options[
                        display_label
                    ] = search_paper_id

                selected_paper_label = st.selectbox(
                    "Choose or search a paper:",
                    list(
                        paper_options.keys()
                    ),
                    key="individual_paper_search_selector"
                )

                selected_paper_id = (
                    paper_options[
                        selected_paper_label
                    ]
                )

                st.caption(
                    f"{len(filtered_papers)} paper"
                    + (
                        ""
                        if len(filtered_papers) == 1
                        else "s"
                    )
                    + " match the current filters."
                )

            else:

                st.info(
                    "No papers match this folder and source type."
                )

        else:
            st.caption(
                "Your Library is empty. Save a paper first."
            )


    literature_question = st.text_input(
        (
            "What would you like to ask your Library?"
            if search_scope == "Entire Library"
            else "What would you like to ask this paper?"
        ),
        key="literature_search_question"
    )

    search_provider = st.selectbox(
        "AI provider for the answer:",
        [
            "Claude",
            "OpenAI"
        ],
        key="literature_search_provider"
    )


    # -----------------------------------------
    # Retrieval size controls
    # -----------------------------------------

    if search_scope == "Entire Library":

        search_col1, search_col2 = (
            st.columns(2)
        )

        if budget_mode:
            search_chunk_options = [
                1,
                2
            ]

            search_max_options = [
                8,
                10,
                12,
                15
            ]

            default_chunk_index = 1
            default_max_index = 2
            search_chunk_size = 1400

        else:
            search_chunk_options = [
                1,
                2,
                3,
                4,
                5
            ]

            search_max_options = [
                10,
                20,
                30,
                40,
                50
            ]

            default_chunk_index = 2
            default_max_index = 2
            search_chunk_size = 2200

        with search_col1:
            chunks_per_paper = st.selectbox(
                "Relevant passages per source:",
                search_chunk_options,
                index=default_chunk_index,
                key="library_chunks_per_paper"
            )

        with search_col2:
            max_total_chunks = st.selectbox(
                "Maximum passages sent to AI:",
                search_max_options,
                index=default_max_index,
                key="library_max_total_chunks"
            )

    else:

        if budget_mode:
            individual_passage_options = [
                2,
                3,
                5
            ]

            individual_default_index = 1
            search_chunk_size = 1400

        else:
            individual_passage_options = [
                3,
                5,
                8,
                10
            ]

            individual_default_index = 1
            search_chunk_size = 2200

        chunks_per_paper = st.selectbox(
            "Relevant passages from this paper:",
            individual_passage_options,
            index=individual_default_index,
            key="individual_paper_passage_count"
        )

        max_total_chunks = (
            chunks_per_paper
        )


    st.markdown("### Answer depth")

    quick_answer_col, deep_answer_col = st.columns(2)

    with quick_answer_col:
        quick_answer_clicked = st.button(
            "⚡ Quick Answer",
            key="ask_literature_quick",
            use_container_width=True
        )

    with deep_answer_col:
        deep_answer_clicked = st.button(
            "🧠 Deep Answer",
            key="ask_literature_deep",
            disabled=budget_mode,
            use_container_width=True
        )

    if budget_mode:
        st.caption(
            "Budget Mode is on: Quick Answer is available; "
            "turn Budget Mode off to use Deep Answer."
        )

    answer_mode = None

    if quick_answer_clicked:
        answer_mode = "quick"

    elif deep_answer_clicked:
        answer_mode = "deep"

    if answer_mode:

        if not literature_question.strip():

            st.warning(
                "Please enter a question first."
            )

        elif (
            search_scope == "Individual Paper"
            and selected_paper_id is None
        ):

            st.warning(
                "Please choose a saved paper first."
            )

        else:

            if search_scope == "Entire Library":

                cursor.execute(
                    """
                    SELECT
                        id,
                        filename,
                        folder,
                        source_type,
                        full_text,
                        zotero_metadata_json,
                        zotero_draft_json
                    FROM papers
                    ORDER BY id
                    """
                )

                papers_for_search = (
                    cursor.fetchall()
                )

                source_label_for_save = (
                    "Entire Library"
                )

            else:

                cursor.execute(
                    """
                    SELECT
                        id,
                        filename,
                        folder,
                        source_type,
                        full_text,
                        zotero_metadata_json,
                        zotero_draft_json
                    FROM papers
                    WHERE id = ?
                    """,
                    (
                        selected_paper_id,
                    )
                )

                papers_for_search = (
                    cursor.fetchall()
                )

                source_label_for_save = (
                    selected_paper_label
                )

            if not papers_for_search:

                st.warning(
                    "No saved source is available for this search."
                )

            else:

                spinner_text = (
                    "Scanning your saved Library locally..."
                    if search_scope == "Entire Library"
                    else "Searching the selected paper locally..."
                )

                with st.spinner(
                    spinner_text
                ):

                    search_evidence = (
                        retrieve_library_evidence(
                            papers_for_search,
                            literature_question,
                            chunks_per_paper=chunks_per_paper,
                            max_total_chunks=max_total_chunks,
                            chunk_size=search_chunk_size
                        )
                    )

                if not search_evidence:

                    st.warning(
                        "No passages matched the question strongly enough. "
                        "Try broader wording or use terminology that appears in the source."
                    )

                else:

                    st.session_state[
                        "last_library_evidence"
                    ] = search_evidence

                    st.session_state[
                        "last_search_scope"
                    ] = search_scope

                    st.session_state[
                        "last_search_source_label"
                    ] = source_label_for_save

                    if search_scope == "Entire Library":
                        st.success(
                            f"Scanned {len(papers_for_search)} saved sources and "
                            f"selected {len(search_evidence)} relevant passages."
                        )
                    else:
                        st.success(
                            f"Selected {len(search_evidence)} relevant passage(s) "
                            "from the chosen paper."
                        )

                    try:

                        answer_heading = (
                            "### Library-Wide Answer"
                            if search_scope == "Entire Library"
                            else "### Answer from Selected Paper"
                        )

                        st.markdown(
                            answer_heading
                        )

                        show_ai_verification_warning()

                        search_output = (
                            st.empty()
                        )

                        answer, evidence_text = (
                            synthesize_library_answer(
                                literature_question,
                                search_evidence,
                                search_provider,
                                search_output,
                                scope=search_scope,
                                source_label=source_label_for_save,
                                mode=answer_mode
                            )
                        )

                        now = (
                            datetime.now().strftime(
                                "%Y-%m-%d %H:%M:%S"
                            )
                        )

                        cursor.execute(
                            """
                            INSERT INTO library_answers (
                                question,
                                provider,
                                answer,
                                evidence,
                                created_at,
                                scope,
                                paper_id,
                                source_label,
                                mode
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                literature_question,
                                search_provider,
                                answer,
                                evidence_text,
                                now,
                                search_scope,
                                (
                                    selected_paper_id
                                    if search_scope == "Individual Paper"
                                    else None
                                ),
                                source_label_for_save,
                                answer_mode
                            )
                        )

                        conn.commit()

                        st.session_state[
                            "last_library_answer"
                        ] = answer

                    except Exception as error:

                        st.error(
                            f"Literature search failed: {error}"
                        )


    # =====================================================
    # COMPARE CLAUDE VS OPENAI ANSWERS
    # =====================================================

    if literature_question.strip():

        ask_compare_depth_label = st.radio(
            "Compare answers:",
            ["⚡ Quick", "🧠 Deep"],
            horizontal=True,
            key="ask_compare_depth"
        )

        ask_compare_depth = (
            "quick"
            if ask_compare_depth_label == "⚡ Quick"
            else "deep"
        )

        if search_scope == "Entire Library":
            cursor.execute(
                """
                SELECT provider, answer
                FROM library_answers
                WHERE question = ?
                  AND COALESCE(scope, 'Entire Library') = 'Entire Library'
                  AND paper_id IS NULL
                  AND COALESCE(mode, 'quick') = ?
                  AND provider IN ('Claude', 'OpenAI')
                ORDER BY id DESC
                """,
                (literature_question, ask_compare_depth)
            )
        else:
            cursor.execute(
                """
                SELECT provider, answer
                FROM library_answers
                WHERE question = ?
                  AND COALESCE(scope, 'Entire Library') = 'Individual Paper'
                  AND paper_id = ?
                  AND COALESCE(mode, 'quick') = ?
                  AND provider IN ('Claude', 'OpenAI')
                ORDER BY id DESC
                """,
                (
                    literature_question,
                    selected_paper_id,
                    ask_compare_depth
                )
            )

        comparison_rows = cursor.fetchall()
        latest_answers = {}

        for provider_name, answer_text in comparison_rows:
            if provider_name not in latest_answers:
                latest_answers[provider_name] = answer_text

        ask_claude = latest_answers.get("Claude")
        ask_openai = latest_answers.get("OpenAI")

        missing_answers = []
        if not ask_claude:
            missing_answers.append("Claude")
        if not ask_openai:
            missing_answers.append("OpenAI")

        if missing_answers:
            st.caption(
                "Run this same question in "
                + ask_compare_depth.title()
                + " mode with "
                + " and ".join(missing_answers)
                + " before comparing."
            )

        ask_free_col, ask_ai_col = st.columns(2)

        with ask_free_col:
            if st.button(
                "🆓 Compare answers with Python",
                key="ask_compare_python",
                disabled=bool(missing_answers),
                use_container_width=True
            ):
                st.session_state["ask_python_comparison"] = (
                    compare_model_outputs(
                        ask_claude,
                        ask_openai,
                        ask_compare_depth
                    )
                )

        with ask_ai_col:
            if st.button(
                "🤖 Compare answers with AI",
                key="ask_compare_ai",
                disabled=bool(missing_answers) or budget_mode,
                use_container_width=True
            ):
                st.session_state["ask_ai_compare_pending"] = True

        if budget_mode:
            st.caption(
                "Python comparison is free. Turn Budget Mode off to use AI comparison."
            )

        if st.session_state.get("ask_ai_compare_pending", False):
            ask_compare_ai_provider = st.selectbox(
                "AI for comparison:",
                ["Claude", "OpenAI"],
                key="ask_compare_ai_provider"
            )

            if st.button(
                "Run AI answer comparison",
                key="run_ask_ai_comparison"
            ):
                try:
                    ask_compare_placeholder = st.empty()
                    st.session_state["ask_ai_comparison"] = (
                        compare_outputs_with_ai(
                            ask_claude,
                            ask_openai,
                            ask_compare_ai_provider,
                            mode=ask_compare_depth,
                            output_placeholder=ask_compare_placeholder,
                            context_label=(
                                "Claude and OpenAI answers to the same "
                                + search_scope
                                + " literature question"
                            )
                        )
                    )
                    st.session_state["ask_ai_compare_pending"] = False
                except Exception as error:
                    st.error(f"AI comparison failed: {error}")

        if st.session_state.get("ask_python_comparison"):
            with st.expander(
                "View free Python answer comparison",
                expanded=True
            ):
                st.markdown(st.session_state["ask_python_comparison"])

        if st.session_state.get("ask_ai_comparison"):
            with st.expander(
                "View AI answer comparison",
                expanded=True
            ):
                st.markdown(st.session_state["ask_ai_comparison"])


    if st.session_state.get(
        "last_library_evidence"
    ):

        evidence_scope = st.session_state.get(
            "last_search_scope",
            "Entire Library"
        )

        evidence_label = (
            "View passages retrieved from my Library"
            if evidence_scope == "Entire Library"
            else "View passages retrieved from the selected paper"
        )

        with st.expander(
            evidence_label,
            expanded=False
        ):

            for index, item in enumerate(
                st.session_state[
                    "last_library_evidence"
                ],
                start=1
            ):

                st.markdown(
                    f"### Evidence {index}: "
                    f"{item.get('citation_title') or item['filename']}"
                )

                if item.get(
                    "citation_in_text"
                ):
                    st.caption(
                        "Citation: "
                        + item[
                            "citation_in_text"
                        ]
                    )

                st.caption(
                    f"Folder: {item['folder']} | "
                    f"Type: {item['source_type']} | "
                    f"Page marker: {item['page']} | "
                    f"Local relevance score: {item['score']}"
                )

                if item.get(
                    "metadata_source"
                ):
                    st.caption(
                        "Bibliographic source: "
                        + item[
                            "metadata_source"
                        ]
                    )

                st.write(
                    item["text"]
                )

                st.divider()


    with st.expander(
        "Previous search answers",
        expanded=False
    ):

        cursor.execute(
            """
            SELECT
                id,
                question,
                provider,
                answer,
                created_at,
                COALESCE(scope, 'Entire Library'),
                source_label
            FROM library_answers
            ORDER BY id DESC
            LIMIT 10
            """
        )

        previous_library_answers = (
            cursor.fetchall()
        )

        if not previous_library_answers:

            st.caption(
                "No previous answers saved yet."
            )

        else:

            for (
                answer_id,
                saved_library_question,
                saved_provider,
                saved_answer,
                saved_created_at,
                saved_scope,
                saved_source_label
            ) in previous_library_answers:

                header_col, delete_col = st.columns(
                    [8, 1]
                )

                with header_col:
                    st.markdown(
                        f"#### {saved_library_question}"
                    )

                    scope_caption = (
                        "Entire Library"
                        if saved_scope == "Entire Library"
                        else (
                            "Individual Paper"
                            + (
                                f" · {saved_source_label}"
                                if saved_source_label
                                else ""
                            )
                        )
                    )

                    st.caption(
                        f"{saved_provider} | "
                        f"{scope_caption} | "
                        f"{saved_created_at}"
                    )

                with delete_col:
                    if st.button(
                        "🗑️",
                        key=f"delete_library_answer_{answer_id}",
                        help="Delete this saved answer"
                    ):
                        st.session_state[
                            f"confirm_delete_library_answer_{answer_id}"
                        ] = True

                if st.session_state.get(
                    f"confirm_delete_library_answer_{answer_id}",
                    False
                ):
                    st.warning(
                        "Delete this saved answer? "
                        "This cannot be undone."
                    )

                    confirm_col, cancel_col = st.columns(2)

                    with confirm_col:
                        if st.button(
                            "Yes, delete",
                            key=f"confirm_delete_library_answer_button_{answer_id}"
                        ):
                            cursor.execute(
                                """
                                DELETE FROM library_answers
                                WHERE id = ?
                                """,
                                (
                                    answer_id,
                                )
                            )

                            conn.commit()

                            st.session_state.pop(
                                f"confirm_delete_library_answer_{answer_id}",
                                None
                            )

                            st.rerun()

                    with cancel_col:
                        if st.button(
                            "Cancel",
                            key=f"cancel_delete_library_answer_{answer_id}"
                        ):
                            st.session_state.pop(
                                f"confirm_delete_library_answer_{answer_id}",
                                None
                            )

                            st.rerun()

                st.markdown(
                    saved_answer
                )

                st.divider()


with library_tab:
    # =========================================================
    # LIBRARY
    # =========================================================

    st.header(
        "Library and Reference Entry"
    )

    st.markdown("### 📁 Create a New Folder")

    new_folder = st.text_input(
        "Folder name:",
        key="library_new_folder_name"
    )

    if st.button(
        "📁 Create Folder",
        key="library_create_folder"
    ):
        new_folder = new_folder.strip()

        if new_folder:
            try:
                cursor.execute(
                    "INSERT INTO folders (name) VALUES (?)",
                    (new_folder,)
                )

                conn.commit()

                st.success(
                    f"Folder '{new_folder}' created."
                )

                st.rerun()

            except sqlite3.IntegrityError:
                st.warning(
                    "A folder with this name already exists."
                )

        else:
            st.warning(
                "Please enter a folder name."
            )

    st.divider()


    cursor.execute("""
    SELECT
        id,
        filename,
        date_added,
        folder,
        source_type,
        full_text,
        research_question,

        claude_quick_analysis,
        claude_quick_date,
        claude_deep_analysis,
        claude_deep_date,

        openai_quick_analysis,
        openai_quick_date,
        openai_deep_analysis,
        openai_deep_date,

        comparison_quick,
        comparison_quick_date,
        comparison_deep,
        comparison_deep_date,

        free_extraction,
        free_extraction_date,

        zotero_item_key,
        zotero_collection_key,
        zotero_metadata_json,
        zotero_linked_at,
        zotero_draft_json,
        zotero_draft_date

    FROM papers

    ORDER BY
        folder,
        source_type,
        id DESC
    """)


    saved_papers = (
        cursor.fetchall()
    )


    # =========================================================
    # ORGANISE PAPERS
    # =========================================================

    papers_by_folder = {}


    for paper in saved_papers:

        folder_name = (
            paper[3]
            or "Unsorted"
        )

        source_type = (
            paper[4]
            or "Article"
        )


        if (
            folder_name
            not in papers_by_folder
        ):

            papers_by_folder[
                folder_name
            ] = {

                "Article": [],
                "Book": [],
                "Thesis": []

            }


        if (
            source_type
            not in papers_by_folder[
                folder_name
            ]
        ):

            papers_by_folder[
                folder_name
            ][
                source_type
            ] = []


        papers_by_folder[
            folder_name
        ][
            source_type
        ].append(
            paper
        )


    # =========================================================
    # DISPLAY LIBRARY
    # =========================================================

    if saved_papers:

        for folder_name in folders:

            folder_data = (
                papers_by_folder.get(

                    folder_name,

                    {
                        "Article": [],
                        "Book": [],
                        "Thesis": []
                    }

                )
            )


            total_papers = sum(

                len(
                    folder_data.get(
                        source_type,
                        []
                    )
                )

                for source_type
                in source_types

            )


            with st.expander(

                f"📁 {folder_name} "
                f"({total_papers} papers)",

                expanded=False

            ):


                for source_type in source_types:

                    papers = (
                        folder_data.get(
                            source_type,
                            []
                        )
                    )


                    icon = {

                        "Article": "📄",
                        "Book": "📚",
                        "Thesis": "🎓"

                    }[
                        source_type
                    ]


                    source_type_plural = {
                        "Article": "Articles",
                        "Book": "Books",
                        "Thesis": "Theses",
                    }[
                        source_type
                    ]

                    with st.expander(
                        f"{icon} "
                        f"{source_type_plural} "
                        f"({len(papers)})",
                        expanded=False
                    ):


                        if not papers:

                            st.caption(
                                f"No "
                                f"{source_type_plural.lower()}."
                            )


                        for paper in papers:

                            paper_id = paper[0]
                            filename = paper[1]
                            date_added = paper[2]
                            full_text = paper[5]

                            claude_quick = (
                                paper[7]
                            )

                            claude_quick_date = (
                                paper[8]
                            )

                            claude_deep = (
                                paper[9]
                            )

                            claude_deep_date = (
                                paper[10]
                            )

                            openai_quick = (
                                paper[11]
                            )

                            openai_quick_date = (
                                paper[12]
                            )

                            openai_deep = (
                                paper[13]
                            )

                            openai_deep_date = (
                                paper[14]
                            )

                            comparison_quick = (
                                paper[15]
                            )

                            comparison_quick_date = (
                                paper[16]
                            )

                            comparison_deep = (
                                paper[17]
                            )

                            comparison_deep_date = (
                                paper[18]
                            )

                            free_extraction = (
                                paper[19]
                            )

                            free_extraction_date = (
                                paper[20]
                            )

                            zotero_item_key = (
                                paper[21]
                            )

                            zotero_collection_key = (
                                paper[22]
                            )

                            zotero_metadata_json = (
                                paper[23]
                            )

                            zotero_linked_at = (
                                paper[24]
                            )

                            zotero_draft_json = (
                                paper[25]
                            )

                            zotero_draft_date = (
                                paper[26]
                            )

                            zotero_draft = None

                            if zotero_draft_json:
                                try:
                                    zotero_draft = (
                                        json.loads(
                                            zotero_draft_json
                                        )
                                    )
                                except Exception:
                                    zotero_draft = None

                            zotero_metadata = None
                            zotero_context = ""

                            if zotero_metadata_json:
                                try:
                                    zotero_metadata = (
                                        json.loads(
                                            zotero_metadata_json
                                        )
                                    )

                                    zotero_context = (
                                        format_zotero_context(
                                            zotero_metadata
                                        )
                                    )
                                except Exception:
                                    zotero_metadata = None
                                    zotero_context = ""


                            with st.expander(
                                f"{icon} {filename}",
                                expanded=False
                            ):

                                st.caption(
                                    f"Added: {date_added}"
                                )


                                status_parts = []


                                if claude_quick:

                                    status_parts.append(
                                        "Claude ⚡"
                                    )


                                if claude_deep:

                                    status_parts.append(
                                        "Claude 🧠"
                                    )


                                if openai_quick:

                                    status_parts.append(
                                        "OpenAI ⚡"
                                    )


                                if openai_deep:

                                    status_parts.append(
                                        "OpenAI 🧠"
                                    )


                                if status_parts:

                                    st.caption(

                                        "Saved AI analyses: "
                                        + " | ".join(
                                            status_parts
                                        )

                                    )

                                else:

                                    st.caption(
                                        "⚪ No AI analysis yet"
                                    )



                                # =====================================
                                # ZOTERO LINK
                                # =====================================

                                st.markdown(
                                    "#### 📚 Zotero"
                                )

                                if zotero_item_key and zotero_metadata:
                                    zotero_data = zotero_item_data(
                                        zotero_metadata
                                    )

                                    st.success(
                                        "Linked to Zotero ✓"
                                    )

                                    st.caption(
                                        f"Linked: {zotero_linked_at or 'Unknown'}"
                                    )

                                    st.markdown(
                                        "**APA 7-style reference from Zotero metadata:**"
                                    )

                                    st.markdown(
                                        format_apa7_from_zotero(
                                            zotero_metadata
                                        )
                                    )

                                    zc1, zc2 = st.columns(2)

                                    with zc1:
                                        if st.button(
                                            "Refresh Zotero metadata",
                                            key=f"zotero_refresh_{paper_id}"
                                        ):
                                            try:
                                                refreshed_item = (
                                                    get_zotero_item(
                                                        zotero_item_key
                                                    )
                                                )

                                                save_zotero_link(
                                                    paper_id,
                                                    refreshed_item,
                                                    (
                                                        zotero_collection_key
                                                        or zotero_selected_collection_key
                                                        or ""
                                                    )
                                                )

                                                st.rerun()

                                            except Exception as error:
                                                st.error(
                                                    f"Could not refresh Zotero metadata: {error}"
                                                )

                                    with zc2:
                                        if st.button(
                                            "Unlink from app",
                                            key=f"zotero_unlink_{paper_id}",
                                            help=(
                                                "This only removes the link from this app. "
                                                "It does not delete anything from Zotero."
                                            )
                                        ):
                                            clear_zotero_link(
                                                paper_id
                                            )

                                            st.rerun()

                                else:
                                    if (
                                        not zotero_user_id
                                        or not zotero_api_key
                                    ):
                                        st.caption(
                                            "Add Zotero credentials to .env to enable linking."
                                        )

                                    elif not zotero_selected_collection_key:
                                        st.caption(
                                            "Choose a Zotero collection in the sidebar."
                                        )

                                    else:
                                        st.caption(
                                            "Not linked. This app will search or create items "
                                            "only inside the selected Zotero collection."
                                        )

                                        if st.button(
                                            "Find match in Zotero",
                                            key=f"zotero_find_{paper_id}"
                                        ):
                                            try:
                                                items = (
                                                    get_zotero_collection_items_cached(
                                                        zotero_user_id,
                                                        zotero_api_key,
                                                        zotero_selected_collection_key
                                                    )
                                                )

                                                matched_item, match_reason = (
                                                    find_zotero_match(
                                                        items,
                                                        full_text,
                                                        filename
                                                    )
                                                )

                                                if matched_item:
                                                    st.session_state[
                                                        f"zotero_match_{paper_id}"
                                                    ] = matched_item

                                                    st.session_state[
                                                        f"zotero_match_reason_{paper_id}"
                                                    ] = match_reason

                                                else:
                                                    st.session_state.pop(
                                                        f"zotero_match_{paper_id}",
                                                        None
                                                    )

                                                    st.session_state.pop(
                                                        f"zotero_match_reason_{paper_id}",
                                                        None
                                                    )

                                                    st.info(
                                                        "No confident DOI/title match was found "
                                                        "in the selected Zotero collection."
                                                    )

                                            except Exception as error:
                                                st.error(
                                                    f"Zotero search failed: {error}"
                                                )

                                        candidate = (
                                            st.session_state.get(
                                                f"zotero_match_{paper_id}"
                                            )
                                        )

                                        if candidate:
                                            candidate_data = (
                                                zotero_item_data(
                                                    candidate
                                                )
                                            )

                                            st.info(
                                                "Possible Zotero match found: "
                                                + str(
                                                    st.session_state.get(
                                                        f"zotero_match_reason_{paper_id}",
                                                        ""
                                                    )
                                                )
                                            )

                                            st.markdown(
                                                f"**{candidate_data.get('title', 'Untitled')}**"
                                            )

                                            st.caption(
                                                (
                                                    zotero_authors_display(
                                                        candidate_data
                                                    )
                                                    or "Author not listed"
                                                )
                                                + " | "
                                                + (
                                                    candidate_data.get(
                                                        "date",
                                                        ""
                                                    )
                                                    or "No date"
                                                )
                                            )

                                            link_col1, link_col2 = st.columns(2)

                                            with link_col1:
                                                if st.button(
                                                    "Link this Zotero item",
                                                    key=f"zotero_link_candidate_{paper_id}"
                                                ):
                                                    save_zotero_link(
                                                        paper_id,
                                                        candidate,
                                                        zotero_selected_collection_key
                                                    )

                                                    st.session_state.pop(
                                                        f"zotero_match_{paper_id}",
                                                        None
                                                    )

                                                    st.session_state.pop(
                                                        f"zotero_match_reason_{paper_id}",
                                                        None
                                                    )

                                                    st.rerun()

                                            with link_col2:
                                                if st.button(
                                                    "Not this item",
                                                    key=f"zotero_reject_candidate_{paper_id}"
                                                ):
                                                    st.session_state.pop(
                                                        f"zotero_match_{paper_id}",
                                                        None
                                                    )

                                                    st.session_state.pop(
                                                        f"zotero_match_reason_{paper_id}",
                                                        None
                                                    )

                                                    st.rerun()

                                        st.markdown(
                                            "##### Free Python-prepared Zotero record"
                                        )

                                        if not zotero_draft:
                                            st.info(
                                                "This paper does not have a prepared Zotero record yet."
                                            )

                                            if st.button(
                                                "✨ Prepare Zotero record automatically — free",
                                                key=f"prepare_zotero_{paper_id}"
                                            ):
                                                try:
                                                    with st.spinner(
                                                        "Preparing Zotero metadata with Python/Crossref..."
                                                    ):
                                                        zotero_draft = (
                                                            extract_zotero_metadata_free(full_text, filename, source_type)
                                                        )

                                                        save_zotero_draft(
                                                            paper_id,
                                                            zotero_draft
                                                        )

                                                    st.rerun()

                                                except Exception as error:
                                                    st.error(
                                                        f"Could not prepare Zotero metadata: {error}"
                                                    )

                                        else:
                                            st.caption(
                                                f"Prepared automatically for free: "
                                                f"{zotero_draft_date or 'Unknown'}"
                                            )

                                            st.info(
                                                "You can edit any field below before adding the record "
                                                "to Zotero. If automatic extraction missed something, "
                                                "fill it in manually and save your corrections."
                                            )

                                            draft_title = (
                                                zotero_draft.get(
                                                    "title",
                                                    ""
                                                )
                                            )

                                            draft_authors = (
                                                authors_list_to_text(
                                                    zotero_draft.get(
                                                        "authors",
                                                        []
                                                    )
                                                )
                                            )

                                            draft_year = (
                                                zotero_draft.get(
                                                    "year",
                                                    ""
                                                )
                                            )

                                            draft_doi = (
                                                zotero_draft.get(
                                                    "doi",
                                                    ""
                                                )
                                            )

                                            draft_journal = (
                                                zotero_draft.get(
                                                    "journal",
                                                    ""
                                                )
                                            )

                                            draft_volume = (
                                                zotero_draft.get(
                                                    "volume",
                                                    ""
                                                )
                                            )

                                            draft_issue = (
                                                zotero_draft.get(
                                                    "issue",
                                                    ""
                                                )
                                            )

                                            draft_pages = (
                                                zotero_draft.get(
                                                    "pages",
                                                    ""
                                                )
                                            )

                                            draft_publisher = (
                                                zotero_draft.get(
                                                    "publisher",
                                                    ""
                                                )
                                            )

                                            draft_place = (
                                                zotero_draft.get(
                                                    "place",
                                                    ""
                                                )
                                            )

                                            draft_edition = (
                                                zotero_draft.get(
                                                    "edition",
                                                    ""
                                                )
                                            )

                                            draft_isbn = (
                                                zotero_draft.get(
                                                    "isbn",
                                                    ""
                                                )
                                            )

                                            draft_num_pages = (
                                                zotero_draft.get(
                                                    "num_pages",
                                                    ""
                                                )
                                            )

                                            draft_university = (
                                                zotero_draft.get(
                                                    "university",
                                                    ""
                                                )
                                            )

                                            draft_thesis_type = (
                                                zotero_draft.get(
                                                    "thesis_type",
                                                    ""
                                                )
                                            )

                                            draft_url = (
                                                zotero_draft.get(
                                                    "url",
                                                    ""
                                                )
                                            )

                                            # ---------------------------------
                                            # COMMON EDITABLE FIELDS
                                            # ---------------------------------

                                            edited_title = st.text_input(
                                                "Title",
                                                value=draft_title,
                                                key=f"zotero_draft_title_{paper_id}"
                                            )

                                            edited_authors = st.text_area(
                                                "Authors (one author per line, or separate with semicolons)",
                                                value=draft_authors,
                                                key=f"zotero_draft_authors_{paper_id}",
                                                help=(
                                                    "Examples: Evelina Galaczi on one line and "
                                                    "Lynda Taylor on the next line."
                                                )
                                            )

                                            zd1, zd2 = st.columns(2)

                                            with zd1:
                                                edited_year = st.text_input(
                                                    "Year",
                                                    value=draft_year,
                                                    key=f"zotero_draft_year_{paper_id}"
                                                )

                                            with zd2:
                                                edited_doi = st.text_input(
                                                    "DOI",
                                                    value=draft_doi,
                                                    key=f"zotero_draft_doi_{paper_id}"
                                                )

                                            # Defaults for fields that belong only to
                                            # particular source types.
                                            edited_journal = draft_journal
                                            edited_volume = draft_volume
                                            edited_issue = draft_issue
                                            edited_pages = draft_pages

                                            edited_publisher = draft_publisher
                                            edited_place = draft_place
                                            edited_edition = draft_edition
                                            edited_isbn = draft_isbn
                                            edited_num_pages = draft_num_pages

                                            edited_university = draft_university
                                            edited_thesis_type = draft_thesis_type

                                            # ---------------------------------
                                            # ARTICLE-SPECIFIC EDITABLE FIELDS
                                            # ---------------------------------

                                            if source_type == "Article":
                                                st.caption(
                                                    "📄 Zotero type: Journal Article — journal, "
                                                    "volume, issue, pages and DOI are used."
                                                )

                                                edited_journal = st.text_input(
                                                    "Journal / publication title",
                                                    value=draft_journal,
                                                    key=f"zotero_draft_journal_{paper_id}"
                                                )

                                                zv1, zv2, zv3 = st.columns(3)

                                                with zv1:
                                                    edited_volume = st.text_input(
                                                        "Volume",
                                                        value=draft_volume,
                                                        key=f"zotero_draft_volume_{paper_id}"
                                                    )

                                                with zv2:
                                                    edited_issue = st.text_input(
                                                        "Issue",
                                                        value=draft_issue,
                                                        key=f"zotero_draft_issue_{paper_id}"
                                                    )

                                                with zv3:
                                                    edited_pages = st.text_input(
                                                        "Pages",
                                                        value=draft_pages,
                                                        key=f"zotero_draft_pages_{paper_id}"
                                                    )

                                            # ---------------------------------
                                            # BOOK-SPECIFIC EDITABLE FIELDS
                                            # ---------------------------------

                                            elif source_type == "Book":
                                                st.caption(
                                                    "📚 Zotero type: Book — publisher, place, "
                                                    "edition, ISBN and number of pages are used."
                                                )

                                                zb1, zb2 = st.columns(2)

                                                with zb1:
                                                    edited_publisher = st.text_input(
                                                        "Publisher",
                                                        value=draft_publisher,
                                                        key=f"zotero_draft_publisher_{paper_id}"
                                                    )

                                                with zb2:
                                                    edited_place = st.text_input(
                                                        "Place",
                                                        value=draft_place,
                                                        key=f"zotero_draft_place_{paper_id}"
                                                    )

                                                zb3, zb4, zb5 = st.columns(3)

                                                with zb3:
                                                    edited_edition = st.text_input(
                                                        "Edition",
                                                        value=draft_edition,
                                                        key=f"zotero_draft_edition_{paper_id}"
                                                    )

                                                with zb4:
                                                    edited_isbn = st.text_input(
                                                        "ISBN",
                                                        value=draft_isbn,
                                                        key=f"zotero_draft_isbn_{paper_id}"
                                                    )

                                                with zb5:
                                                    edited_num_pages = st.text_input(
                                                        "Number of pages",
                                                        value=draft_num_pages,
                                                        key=f"zotero_draft_num_pages_{paper_id}"
                                                    )

                                            # ---------------------------------
                                            # THESIS-SPECIFIC EDITABLE FIELDS
                                            # ---------------------------------

                                            elif source_type == "Thesis":
                                                st.caption(
                                                    "🎓 Zotero type: Thesis — university, thesis "
                                                    "type and place are used instead of journal fields."
                                                )

                                                zt1, zt2 = st.columns(2)

                                                with zt1:
                                                    edited_university = st.text_input(
                                                        "University",
                                                        value=draft_university,
                                                        key=f"zotero_draft_university_{paper_id}"
                                                    )

                                                with zt2:
                                                    edited_thesis_type = st.text_input(
                                                        "Thesis type",
                                                        value=draft_thesis_type,
                                                        key=f"zotero_draft_thesis_type_{paper_id}",
                                                        help=(
                                                            "Examples: Doctoral dissertation, "
                                                            "Master's thesis, MPhil thesis."
                                                        )
                                                    )

                                                edited_place = st.text_input(
                                                    "Place",
                                                    value=draft_place,
                                                    key=f"zotero_draft_thesis_place_{paper_id}"
                                                )

                                            # URL must always be editable, even when the
                                            # automatic extractor left it blank.
                                            edited_url = st.text_input(
                                                "URL",
                                                value=draft_url,
                                                key=f"zotero_draft_url_{paper_id}"
                                            )

                                            if zotero_draft.get(
                                                "notes"
                                            ):
                                                st.caption(
                                                    "Metadata note: "
                                                    + zotero_draft[
                                                        "notes"
                                                    ]
                                                )

                                            edited_authors_list = [
                                                name.strip()
                                                for name in re.split(
                                                    r";|\n",
                                                    edited_authors or ""
                                                )
                                                if name.strip()
                                            ]

                                            edited_draft = normalize_zotero_draft(
                                                {
                                                    "title": edited_title,
                                                    "authors": edited_authors_list,
                                                    "year": edited_year,
                                                    "doi": edited_doi,
                                                    "journal": edited_journal,
                                                    "volume": edited_volume,
                                                    "issue": edited_issue,
                                                    "pages": edited_pages,
                                                    "publisher": edited_publisher,
                                                    "place": edited_place,
                                                    "edition": edited_edition,
                                                    "isbn": edited_isbn,
                                                    "num_pages": edited_num_pages,
                                                    "university": edited_university,
                                                    "thesis_type": edited_thesis_type,
                                                    "url": edited_url,
                                                    "notes": zotero_draft.get(
                                                        "notes",
                                                        ""
                                                    ),
                                                },
                                                source_type
                                            )

                                            save_col, add_col = st.columns(2)

                                            with save_col:
                                                if st.button(
                                                    "💾 Save metadata corrections",
                                                    key=f"save_zotero_edits_{paper_id}"
                                                ):
                                                    save_zotero_draft(
                                                        paper_id,
                                                        edited_draft
                                                    )

                                                    st.success(
                                                        "Your Zotero metadata corrections were saved "
                                                        "in the app. Nothing was added to Zotero."
                                                    )

                                            with add_col:
                                                st.caption(
                                                    "Nothing is written to Zotero until you approve it."
                                                )

                                                if st.button(
                                                    "✅ Add this record to Zotero",
                                                    key=f"approve_zotero_{paper_id}"
                                                ):
                                                    try:
                                                        # Persist the exact values the user approved.
                                                        save_zotero_draft(
                                                            paper_id,
                                                            edited_draft
                                                        )

                                                        created_item = (
                                                            create_zotero_item(
                                                                zotero_selected_collection_key,
                                                                source_type,
                                                                edited_title,
                                                                edited_authors,
                                                                edited_year,
                                                                edited_doi,
                                                                publication_title=edited_journal,
                                                                publisher=edited_publisher,
                                                                university=edited_university,
                                                                volume=edited_volume,
                                                                issue=edited_issue,
                                                                pages=edited_pages,
                                                                url=edited_url,
                                                                place=edited_place,
                                                                edition=edited_edition,
                                                                isbn=edited_isbn,
                                                                num_pages=edited_num_pages,
                                                                thesis_type=edited_thesis_type
                                                            )
                                                        )

                                                        created_key = (
                                                            created_item.get("key")
                                                            or created_item.get(
                                                                "data",
                                                                {}
                                                            ).get("key")
                                                        )

                                                        if created_key:
                                                            try:
                                                                canonical_item = (
                                                                    get_zotero_item(
                                                                        created_key
                                                                    )
                                                                )
                                                            except Exception:
                                                                canonical_item = (
                                                                    created_item
                                                                )

                                                            save_zotero_link(
                                                                paper_id,
                                                                canonical_item,
                                                                zotero_selected_collection_key
                                                            )

                                                            get_zotero_collection_items_cached.clear()

                                                            st.success(
                                                                "Approved record added to Zotero."
                                                            )

                                                            st.rerun()

                                                        else:
                                                            st.error(
                                                                "Zotero created the item but did not "
                                                                "return an item key."
                                                            )

                                                    except Exception as error:
                                                        st.error(
                                                            "Could not add the approved record to Zotero: "
                                                            f"{error}"
                                                        )

                                            if st.button(
                                                "↻ Re-read source and replace metadata with automatic extraction",
                                                key=f"regenerate_zotero_draft_{paper_id}",
                                                help=(
                                                    "This replaces your saved metadata draft with a fresh "
                                                    "automatic extraction. Use it only if you want to discard "
                                                    "the current manual corrections."
                                                )
                                            ):
                                                try:
                                                    with st.spinner(
                                                        "Re-checking metadata with Python/Crossref..."
                                                    ):
                                                        new_draft = (
                                                            extract_zotero_metadata_free(
                                                                full_text,
                                                                filename,
                                                                source_type
                                                            )
                                                        )

                                                        save_zotero_draft(
                                                            paper_id,
                                                            new_draft
                                                        )

                                                        # Clear widget values so the newly extracted
                                                        # draft is displayed after rerun.
                                                        draft_widget_keys = [
                                                            f"zotero_draft_title_{paper_id}",
                                                            f"zotero_draft_authors_{paper_id}",
                                                            f"zotero_draft_year_{paper_id}",
                                                            f"zotero_draft_doi_{paper_id}",
                                                            f"zotero_draft_journal_{paper_id}",
                                                            f"zotero_draft_volume_{paper_id}",
                                                            f"zotero_draft_issue_{paper_id}",
                                                            f"zotero_draft_pages_{paper_id}",
                                                            f"zotero_draft_publisher_{paper_id}",
                                                            f"zotero_draft_place_{paper_id}",
                                                            f"zotero_draft_edition_{paper_id}",
                                                            f"zotero_draft_isbn_{paper_id}",
                                                            f"zotero_draft_num_pages_{paper_id}",
                                                            f"zotero_draft_university_{paper_id}",
                                                            f"zotero_draft_thesis_type_{paper_id}",
                                                            f"zotero_draft_thesis_place_{paper_id}",
                                                            f"zotero_draft_url_{paper_id}",
                                                        ]

                                                        for widget_key in draft_widget_keys:
                                                            st.session_state.pop(
                                                                widget_key,
                                                                None
                                                            )

                                                    st.rerun()

                                                except Exception as error:
                                                    st.error(
                                                        f"Could not regenerate Zotero metadata: {error}"
                                                    )


                                # =====================================
                                # MAIN BUTTONS
                                # =====================================

                                col1, col2, col3, col4 = (
                                    st.columns(4)
                                )


                                with col1:

                                    if st.button(
                                        "Rename",
                                        key=f"rename_{paper_id}"
                                    ):

                                        st.session_state[
                                            f"rename_mode_{paper_id}"
                                        ] = True


                                with col2:

                                    if st.button(
                                        "Move",
                                        key=f"move_{paper_id}"
                                    ):

                                        st.session_state[
                                            f"move_mode_{paper_id}"
                                        ] = True


                                with col3:

                                    if st.button(
                                        "AI Analysis",
                                        key=f"ai_{paper_id}"
                                    ):

                                        current = (
                                            st.session_state.get(

                                                f"show_ai_{paper_id}",

                                                False

                                            )
                                        )

                                        st.session_state[
                                            f"show_ai_{paper_id}"
                                        ] = not current


                                with col4:

                                    if st.button(
                                        "Delete",
                                        key=f"delete_{paper_id}"
                                    ):

                                        st.session_state[
                                            f"confirm_delete_{paper_id}"
                                        ] = True


                                # =====================================
                                # AI PANEL
                                # =====================================

                                if st.session_state.get(

                                    f"show_ai_{paper_id}",

                                    False

                                ):

                                    st.markdown(
                                        "#### Paper Analysis"
                                    )

                                    st.markdown(
                                        "### 🆓 Free Basic Extraction"
                                    )

                                    if free_extraction:

                                        st.caption(
                                            f"Saved: {free_extraction_date}"
                                        )

                                        with st.expander(
                                            "View Free Basic Extraction"
                                        ):

                                            st.markdown(
                                                free_extraction
                                            )

                                        free_button_label = (
                                            "Run Free Extraction Again"
                                        )

                                    else:

                                        st.caption(
                                            "No free extraction saved yet."
                                        )

                                        free_button_label = (
                                            "🆓 Extract Basic Information"
                                        )

                                    if st.button(
                                        free_button_label,
                                        key=f"free_extract_{paper_id}"
                                    ):

                                        free_result = (
                                            extract_basic_information(
                                                full_text,
                                                filename
                                            )
                                        )

                                        now = (
                                            datetime.now().strftime(
                                                "%Y-%m-%d %H:%M:%S"
                                            )
                                        )

                                        cursor.execute(
                                            """
                                            UPDATE papers
                                            SET
                                                free_extraction = ?,
                                                free_extraction_date = ?
                                            WHERE id = ?
                                            """,
                                            (
                                                free_result,
                                                now,
                                                paper_id
                                            )
                                        )

                                        conn.commit()
                                        st.rerun()

                                    st.markdown(
                                        "### AI Analysis"
                                    )

                                    budget_ready_saved = (
                                        (not budget_mode)
                                        or bool(free_extraction)
                                    )

                                    if budget_mode and not budget_ready_saved:
                                        st.caption(
                                            "Run the free extraction first to unlock paid AI analysis."
                                        )

                                    claude_tab, openai_tab, compare_tab = (
                                        st.tabs(
                                            [
                                                "Claude",
                                                "OpenAI",
                                                "Compare"
                                            ]
                                        )
                                    )


                                    # =============================
                                    # CLAUDE TAB
                                    # =============================

                                    with claude_tab:

                                        c1, c2 = (
                                            st.columns(2)
                                        )


                                        with c1:

                                            st.markdown(
                                                "**⚡ Quick Analysis**"
                                            )


                                            if claude_quick:

                                                st.caption(
                                                    f"Saved: "
                                                    f"{claude_quick_date}"
                                                )


                                                with st.expander(
                                                    "View Claude Quick Analysis"
                                                ):

                                                    st.markdown(
                                                        claude_quick
                                                    )


                                                label = (
                                                    "Regenerate Claude Quick"
                                                )

                                            else:

                                                st.caption(
                                                    "Not generated yet."
                                                )

                                                label = (
                                                    "Generate Claude Quick"
                                                )


                                            if st.button(

                                                label,

                                                key=f"cq_{paper_id}",
                                                disabled=not budget_ready_saved

                                            ):

                                                try:

                                                    output = (
                                                        st.empty()
                                                    )


                                                    result = (
                                                        analyse_with_claude(

                                                            full_text,

                                                            "quick",

                                                            output,

                                                            zotero_context

                                                        )
                                                    )


                                                    update_saved_analysis(

                                                        paper_id,

                                                        "Claude",

                                                        "quick",

                                                        result

                                                    )


                                                    cursor.execute(

                                                        """
                                                        UPDATE papers
                                                        SET
                                                            comparison_quick = NULL,
                                                            comparison_quick_date = NULL
                                                        WHERE id = ?
                                                        """,

                                                        (
                                                            paper_id,
                                                        )

                                                    )

                                                    conn.commit()

                                                    st.rerun()


                                                except Exception as error:

                                                    st.error(

                                                        f"Claude analysis "
                                                        f"failed: {error}"

                                                    )


                                        with c2:

                                            st.markdown(
                                                "**🧠 Deep Analysis**"
                                            )


                                            if claude_deep:

                                                st.caption(
                                                    f"Saved: "
                                                    f"{claude_deep_date}"
                                                )


                                                with st.expander(
                                                    "View Claude Deep Analysis"
                                                ):

                                                    st.markdown(
                                                        claude_deep
                                                    )


                                                label = (
                                                    "Regenerate Claude Deep"
                                                )

                                            else:

                                                st.caption(
                                                    "Not generated yet."
                                                )

                                                label = (
                                                    "Generate Claude Deep"
                                                )


                                            if st.button(

                                                label,

                                                key=f"cd_{paper_id}",
                                                disabled=(
                                                    budget_mode
                                                    or not budget_ready_saved
                                                )

                                            ):

                                                try:

                                                    output = (
                                                        st.empty()
                                                    )


                                                    result = (
                                                        analyse_with_claude(

                                                            full_text,

                                                            "deep",

                                                            output,

                                                            zotero_context

                                                        )
                                                    )


                                                    update_saved_analysis(

                                                        paper_id,

                                                        "Claude",

                                                        "deep",

                                                        result

                                                    )


                                                    cursor.execute(

                                                        """
                                                        UPDATE papers
                                                        SET
                                                            comparison_deep = NULL,
                                                            comparison_deep_date = NULL
                                                        WHERE id = ?
                                                        """,

                                                        (
                                                            paper_id,
                                                        )

                                                    )

                                                    conn.commit()

                                                    st.rerun()


                                                except Exception as error:

                                                    st.error(

                                                        f"Claude analysis "
                                                        f"failed: {error}"

                                                    )


                                    # =============================
                                    # OPENAI TAB
                                    # =============================

                                    with openai_tab:

                                        o1, o2 = (
                                            st.columns(2)
                                        )


                                        with o1:

                                            st.markdown(
                                                "**⚡ Quick Analysis**"
                                            )


                                            if openai_quick:

                                                st.caption(
                                                    f"Saved: "
                                                    f"{openai_quick_date}"
                                                )


                                                with st.expander(
                                                    "View OpenAI Quick Analysis"
                                                ):

                                                    st.markdown(
                                                        openai_quick
                                                    )


                                                label = (
                                                    "Regenerate OpenAI Quick"
                                                )

                                            else:

                                                st.caption(
                                                    "Not generated yet."
                                                )

                                                label = (
                                                    "Generate OpenAI Quick"
                                                )


                                            if st.button(

                                                label,

                                                key=f"oq_{paper_id}",
                                                disabled=not budget_ready_saved

                                            ):

                                                try:

                                                    output = (
                                                        st.empty()
                                                    )


                                                    result = (
                                                        analyse_with_openai(

                                                            full_text,

                                                            "quick",

                                                            output,

                                                            zotero_context

                                                        )
                                                    )


                                                    update_saved_analysis(

                                                        paper_id,

                                                        "OpenAI",

                                                        "quick",

                                                        result

                                                    )


                                                    cursor.execute(

                                                        """
                                                        UPDATE papers
                                                        SET
                                                            comparison_quick = NULL,
                                                            comparison_quick_date = NULL
                                                        WHERE id = ?
                                                        """,

                                                        (
                                                            paper_id,
                                                        )

                                                    )

                                                    conn.commit()

                                                    st.rerun()


                                                except Exception as error:

                                                    st.error(

                                                        f"OpenAI analysis "
                                                        f"failed: {error}"

                                                    )


                                        with o2:

                                            st.markdown(
                                                "**🧠 Deep Analysis**"
                                            )


                                            if openai_deep:

                                                st.caption(
                                                    f"Saved: "
                                                    f"{openai_deep_date}"
                                                )


                                                with st.expander(
                                                    "View OpenAI Deep Analysis"
                                                ):

                                                    st.markdown(
                                                        openai_deep
                                                    )


                                                label = (
                                                    "Regenerate OpenAI Deep"
                                                )

                                            else:

                                                st.caption(
                                                    "Not generated yet."
                                                )

                                                label = (
                                                    "Generate OpenAI Deep"
                                                )


                                            if st.button(

                                                label,

                                                key=f"od_{paper_id}",
                                                disabled=(
                                                    budget_mode
                                                    or not budget_ready_saved
                                                )

                                            ):

                                                try:

                                                    output = (
                                                        st.empty()
                                                    )


                                                    result = (
                                                        analyse_with_openai(

                                                            full_text,

                                                            "deep",

                                                            output,

                                                            zotero_context

                                                        )
                                                    )


                                                    update_saved_analysis(

                                                        paper_id,

                                                        "OpenAI",

                                                        "deep",

                                                        result

                                                    )


                                                    cursor.execute(

                                                        """
                                                        UPDATE papers
                                                        SET
                                                            comparison_deep = NULL,
                                                            comparison_deep_date = NULL
                                                        WHERE id = ?
                                                        """,

                                                        (
                                                            paper_id,
                                                        )

                                                    )

                                                    conn.commit()

                                                    st.rerun()


                                                except Exception as error:

                                                    st.error(

                                                        f"OpenAI analysis "
                                                        f"failed: {error}"

                                                    )


                                    # =============================
                                    # COMPARE TAB
                                    # =============================

                                    with compare_tab:

                                        st.markdown(
                                            "### Claude vs OpenAI"
                                        )


                                        st.caption(

                                            "The comparison itself is free and uses Python rules only. "
                                            "It compares already-saved Claude and OpenAI analyses. "
                                            "It will never generate a missing paid analysis automatically."

                                        )


                                        compare_mode_label = (
                                            st.radio(

                                                "Comparison depth:",

                                                [
                                                    "⚡ Quick",
                                                    "🧠 Deep"
                                                ],

                                                horizontal=True,

                                                key=f"compare_mode_{paper_id}"

                                            )
                                        )


                                        compare_mode = (

                                            "quick"

                                            if compare_mode_label
                                            == "⚡ Quick"

                                            else "deep"

                                        )


                                        if compare_mode == "quick":

                                            current_claude = (
                                                claude_quick
                                            )

                                            current_openai = (
                                                openai_quick
                                            )

                                            current_comparison = (
                                                comparison_quick
                                            )

                                            current_comparison_date = (
                                                comparison_quick_date
                                            )

                                        else:

                                            current_claude = (
                                                claude_deep
                                            )

                                            current_openai = (
                                                openai_deep
                                            )

                                            current_comparison = (
                                                comparison_deep
                                            )

                                            current_comparison_date = (
                                                comparison_deep_date
                                            )


                                        missing = []


                                        if not current_claude:

                                            missing.append(
                                                "Claude"
                                            )


                                        if not current_openai:

                                            missing.append(
                                                "OpenAI"
                                            )


                                        if missing:

                                            st.info(
                                                "Missing analysis: "
                                                + ", ".join(
                                                    missing
                                                )
                                                + ". Generate the missing analysis manually "
                                                + "before comparing. The Compare button will "
                                                + "never trigger a paid model automatically."
                                            )


                                        button_label = (

                                            "Run comparison"

                                            if not current_comparison

                                            else "Regenerate comparison"

                                        )


                                        if st.button(

                                            button_label,

                                            key=f"compare_{paper_id}",
                                            disabled=bool(missing)

                                        ):

                                            try:

                                                claude_result = (
                                                    current_claude
                                                )

                                                openai_result = (
                                                    current_openai
                                                )

                                                st.markdown(
                                                    "**Running free structural comparison...**"
                                                )


                                                comparison_output = (
                                                    st.empty()
                                                )


                                                comparison_result = (
                                                    compare_model_outputs(

                                                        claude_result,

                                                        openai_result,

                                                        compare_mode,

                                                        comparison_output

                                                    )
                                                )


                                                update_comparison(

                                                    paper_id,

                                                    compare_mode,

                                                    comparison_result

                                                )


                                                st.rerun()


                                            except Exception as error:

                                                st.error(

                                                    f"Comparison failed: "
                                                    f"{error}"

                                                )


                                        # =================================
                                        # SIDE-BY-SIDE OUTPUTS
                                        # =================================

                                        if (
                                            current_claude
                                            and current_openai
                                        ):

                                            st.markdown(
                                                "#### Side-by-side analysis"
                                            )


                                            left, right = (
                                                st.columns(2)
                                            )


                                            with left:

                                                st.markdown(
                                                    "##### Claude"
                                                )

                                                with st.container(
                                                    height=500,
                                                    border=True
                                                ):

                                                    st.markdown(
                                                        current_claude
                                                    )


                                            with right:

                                                st.markdown(
                                                    "##### OpenAI"
                                                )

                                                with st.container(
                                                    height=500,
                                                    border=True
                                                ):

                                                    st.markdown(
                                                        current_openai
                                                    )


                                        # =================================
                                        # COMPARISON NOTES
                                        # =================================

                                        if current_comparison:

                                            st.markdown(
                                                "#### 🆓 Free Structural Comparison"
                                            )


                                            st.caption(

                                                f"Saved: "
                                                f"{current_comparison_date}"

                                            )


                                            st.warning(

                                                "This comparison is rule-based, not AI-generated. "
                                                "It can flag textual differences, but semantic "
                                                "meaning and factual correctness still require "
                                                "checking against the original paper."

                                            )


                                            st.markdown(
                                                current_comparison
                                            )


                                # =====================================
                                # RENAME
                                # =====================================

                                if st.session_state.get(

                                    f"rename_mode_{paper_id}",

                                    False

                                ):

                                    new_name = st.text_input(

                                        "Enter a new paper name:",

                                        value=filename,

                                        key=f"new_name_{paper_id}"

                                    )


                                    rename_col1, rename_col2 = (
                                        st.columns(2)
                                    )


                                    with rename_col1:

                                        if st.button(

                                            "Save new name",

                                            key=f"save_name_{paper_id}"

                                        ):

                                            new_name = (
                                                new_name.strip()
                                            )


                                            if new_name:

                                                cursor.execute(

                                                    """
                                                    UPDATE papers
                                                    SET filename = ?
                                                    WHERE id = ?
                                                    """,

                                                    (
                                                        new_name,
                                                        paper_id
                                                    )

                                                )


                                                conn.commit()


                                                st.session_state[
                                                    f"rename_mode_{paper_id}"
                                                ] = False


                                                st.rerun()


                                    with rename_col2:

                                        if st.button(

                                            "Cancel rename",

                                            key=f"cancel_rename_{paper_id}"

                                        ):

                                            st.session_state[
                                                f"rename_mode_{paper_id}"
                                            ] = False


                                            st.rerun()


                                # =====================================
                                # MOVE
                                # =====================================

                                if st.session_state.get(

                                    f"move_mode_{paper_id}",

                                    False

                                ):

                                    move_folder = st.selectbox(

                                        "Move to folder:",

                                        folders,

                                        key=f"move_folder_{paper_id}"

                                    )


                                    move_type = st.selectbox(

                                        "Move to source type:",

                                        source_types,

                                        key=f"move_type_{paper_id}"

                                    )


                                    move_col1, move_col2 = (
                                        st.columns(2)
                                    )


                                    with move_col1:

                                        if st.button(

                                            "Move paper",

                                            key=f"confirm_move_{paper_id}"

                                        ):

                                            cursor.execute(

                                                """
                                                UPDATE papers
                                                SET
                                                    folder = ?,
                                                    source_type = ?
                                                WHERE id = ?
                                                """,

                                                (
                                                    move_folder,
                                                    move_type,
                                                    paper_id
                                                )

                                            )


                                            conn.commit()


                                            st.session_state[
                                                f"move_mode_{paper_id}"
                                            ] = False


                                            st.rerun()


                                    with move_col2:

                                        if st.button(

                                            "Cancel move",

                                            key=f"cancel_move_{paper_id}"

                                        ):

                                            st.session_state[
                                                f"move_mode_{paper_id}"
                                            ] = False


                                            st.rerun()


                                # =====================================
                                # DELETE
                                # =====================================

                                if st.session_state.get(

                                    f"confirm_delete_{paper_id}",

                                    False

                                ):

                                    st.warning(

                                        f"Are you sure you want to delete "
                                        f"'{filename}'?"

                                    )


                                    delete_col1, delete_col2 = (
                                        st.columns(2)
                                    )


                                    with delete_col1:

                                        if st.button(

                                            "Yes, delete",

                                            key=f"yes_delete_{paper_id}"

                                        ):

                                            cursor.execute(

                                                """
                                                DELETE FROM papers
                                                WHERE id = ?
                                                """,

                                                (
                                                    paper_id,
                                                )

                                            )


                                            conn.commit()

                                            st.rerun()


                                    with delete_col2:

                                        if st.button(

                                            "Cancel",

                                            key=f"cancel_delete_{paper_id}"

                                        ):

                                            st.session_state[
                                                f"confirm_delete_{paper_id}"
                                            ] = False


                                            st.rerun()




    else:

        st.write(
            "No papers saved yet."
        )


    # =========================================================
    # CLOSE DATABASE
    # =========================================================

    conn.close()
