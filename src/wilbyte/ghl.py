"""GoHighLevel / LeadConnector blog client.

Replaces the manual GHL steps: new post -> paste body -> continue -> fill slug,
category, author, keywords, cover image, alt text, description -> schedule 10am.

The Blogs API needs a Private Integration token with these scopes:
    blogs/post.write, blogs/post-update.write, blogs/check-slug.readonly,
    blogs/category.readonly, blogs/author.readonly, medias.write, medias.readonly

Field names are collected in `POST_FIELDS` so they can be corrected in one place
if HighLevel's schema drifts. Run any new post through `wilbyte run --dry-run`
first to see the exact payload before it is sent.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "https://services.leadconnectorhq.com"
API_VERSION = "2021-07-28"

# Request-body keys for POST /blogs/posts, kept in one place on purpose.
POST_FIELDS = {
    "title": "title",
    "location_id": "locationId",
    "blog_id": "blogId",
    "content": "rawHTML",
    "description": "description",
    "image_url": "imageUrl",
    "image_alt": "imageAltText",
    "slug": "urlSlug",
    "canonical": "canonicalLink",
    "author": "author",
    "categories": "categories",
    "tags": "tags",
    "status": "status",
    "published_at": "publishedAt",
}

# The Blogs API rejects any page size over 50 with a 422.
MAX_PAGE = 50

STATUS_DRAFT = "DRAFT"
STATUS_PUBLISHED = "PUBLISHED"
STATUS_SCHEDULED = "SCHEDULED"


class GHLError(RuntimeError):
    """Raised when the LeadConnector API rejects a request."""


class GHLClient:
    def __init__(self, token: str, location_id: str, *, timeout: float = 60.0):
        self.location_id = location_id
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Version": API_VERSION,
                "Accept": "application/json",
            },
        )

    def __enter__(self) -> "GHLClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ---------------------------------------------------------------- requests

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise GHLError(f"{method} {path} failed to send: {exc}") from exc

        if response.status_code >= 400:
            raise GHLError(
                f"{method} {path} -> HTTP {response.status_code}: {response.text[:500]}"
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise GHLError(f"{method} {path} returned non-JSON: {response.text[:200]}") from exc

    # ------------------------------------------------------------- lookups

    def list_blog_sites(self, *, limit: int = MAX_PAGE) -> list[dict]:
        data = self._request(
            "GET", "/blogs/site/all",
            params={"locationId": self.location_id, "limit": limit, "skip": 0},
        )
        return data.get("data") or data.get("blogs") or []

    def _paged(self, path: str, keys: tuple[str, ...], **extra_params) -> list[dict]:
        """Walk a paged endpoint at the API's maximum page size."""
        items: list[dict] = []
        offset = 0
        while True:
            data = self._request(
                "GET", path,
                params={
                    "locationId": self.location_id,
                    "limit": MAX_PAGE,
                    "offset": offset,
                    **extra_params,
                },
            )
            batch = next((data[k] for k in keys if data.get(k)), [])
            items.extend(batch)
            if len(batch) < MAX_PAGE:
                return items
            offset += MAX_PAGE
            if offset > 5000:  # guard against a non-terminating pager
                return items

    def list_posts(self, blog_id: str) -> list[dict]:
        """All posts on a blog, paged so the scheduler sees every taken day."""
        return self._paged("/blogs/posts/all", ("blogs", "data", "posts"), blogId=blog_id)

    def list_authors(self) -> list[dict]:
        return self._paged("/blogs/authors", ("authors", "data"))

    def list_categories(self) -> list[dict]:
        return self._paged("/blogs/categories", ("categories", "data"))

    def slug_exists(self, url_slug: str, *, post_id: str | None = None) -> bool:
        params = {"locationId": self.location_id, "urlSlug": url_slug}
        if post_id:
            params["postId"] = post_id
        data = self._request("GET", "/blogs/posts/url-slug-exists", params=params)
        return bool(data.get("exists", data.get("data", {}).get("exists", False)))

    # ------------------------------------------------------------- media

    def upload_media(self, file_path: Path, *, name: str | None = None) -> str:
        """Upload the cover image to the media library and return its public URL."""
        if not file_path.exists():
            raise GHLError(f"Cover image not found: {file_path}")

        filename = name or file_path.name
        with file_path.open("rb") as fh:
            data = self._request(
                "POST", "/medias/upload-file",
                files={"file": (filename, fh, "image/png")},
                data={"locationId": self.location_id, "name": filename},
            )

        url = data.get("url") or data.get("fileUrl") or (data.get("data") or {}).get("url")
        if not url:
            raise GHLError(f"Media upload succeeded but returned no URL: {data}")
        return url

    # ------------------------------------------------------------- posts

    def create_post(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = self._request("POST", "/blogs/posts", json=payload)
        return data.get("data") or data

    def update_post(self, post_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = self._request("PUT", f"/blogs/posts/{post_id}", json=payload)
        return data.get("data") or data


# --------------------------------------------------------------- payload build


def build_post_payload(
    *,
    location_id: str,
    blog_id: str,
    title: str,
    content_html: str,
    description: str,
    url_slug: str,
    canonical_link: str,
    author_id: str,
    category_ids: list[str],
    keywords: list[str],
    image_url: str | None,
    image_alt: str,
    status: str,
    published_at: datetime | None,
) -> dict[str, Any]:
    """Assemble the create/update body for a blog post."""
    f = POST_FIELDS
    payload: dict[str, Any] = {
        f["location_id"]: location_id,
        f["blog_id"]: blog_id,
        f["title"]: title,
        f["content"]: content_html,
        f["description"]: description,
        f["slug"]: url_slug,
        f["canonical"]: canonical_link,
        f["author"]: author_id,
        f["categories"]: category_ids,
        f["tags"]: keywords,
        f["status"]: status,
        f["image_alt"]: image_alt,
    }
    if image_url:
        payload[f["image_url"]] = image_url
    if published_at:
        payload[f["published_at"]] = to_api_timestamp(published_at)
    return payload


def to_api_timestamp(moment: datetime) -> str:
    """UTC ISO-8601 with a trailing Z, which is what the Blogs API expects."""
    from datetime import timezone

    if moment.tzinfo is None:
        raise GHLError(f"Refusing to send a naive datetime to GHL: {moment!r}")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def resolve_by_name(items: list[dict], wanted: str, *, kind: str) -> str:
    """Find an author/category id by display name, case- and quote-insensitive."""
    target = _normalize_name(wanted)
    for item in items:
        for key in ("name", "label", "title", "fullName"):
            value = item.get(key)
            if value and _normalize_name(str(value)) == target:
                item_id = item.get("_id") or item.get("id")
                if item_id:
                    return str(item_id)
    available = ", ".join(
        sorted({str(i.get("name") or i.get("label") or i.get("title") or "?") for i in items})
    )
    raise GHLError(f"No {kind} named {wanted!r} in this location. Available: {available or '(none)'}")


def _normalize_name(text: str) -> str:
    import re

    text = text.replace("“", '"').replace("”", '"').replace("’", "'")
    return re.sub(r"\s+", " ", re.sub(r"[\"']", "", text)).strip().lower()
