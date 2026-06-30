# SPDX-License-Identifier: AGPL-3.0-or-later
"""Toutiao (今日头条) search engine

`Toutiao`_ is a Chinese search engine by ByteDance that aggregates news,
videos, encyclopedia articles and more. Supports "synthesis" (综合),
"information" (资讯), "atlas" (图片),
"weitoutiao" (微头条) and "video" (视频) categories via the
``toutiao_pd`` setting.

For most categories, results are JSON embedded in
``<script type="application/json">`` tags. For atlas (images), results
are parsed directly from the HTML DOM.

.. _Toutiao: https://www.toutiao.com
"""

import json
import re
import typing as t

from datetime import datetime
from html import unescape
from urllib.parse import urlencode, urlparse, parse_qs, unquote

from lxml import html as lxml_html

from searx import logger
from searx.enginelib import EngineCache
from searx.utils import html_to_text
from searx.exceptions import SearxEngineCaptchaException
from searx.network import post as http_post

if t.TYPE_CHECKING:
    from searx.extended_types import SXNG_Response

log = logger.getChild(__name__)

about = {
    "website": "https://www.toutiao.com",
    "wikidata_id": "Q24835387",
    "official_api_documentation": None,
    "use_official_api": False,
    "require_api_key": False,
    "results": "HTML",
}

language = "zh"
categories = ["general", "news"]
paging = True
max_page = 10
time_range_support = True

base_url = "https://so.toutiao.com"

# Search category: "synthesis" (综合, default), "information" (资讯),
# "atlas" (图片), "weitoutiao" (微头条),
# or "video" (视频)
# Override via toutiao_pd in settings.yml
toutiao_pd = "synthesis"

# Search scope: "all" = web-wide (default), "site" = Toutiao-only
# only valid for "synthesis" category
# Override via toutiao_filter_vendor in settings.yml
toutiao_filter_vendor = "all"

_TIME_RANGES = frozenset({"day", "week", "month", "year"})

_TTWID_REGISTER_URL = "https://ttwid.bytedance.com/ttwid/union/register/"
_TTWID_CACHE_KEY = "ttwid"
_TTWID_CACHE_EXPIRATION = 3600

CACHE: EngineCache
"""Stores the ttwid cookie to avoid re-fetching on every request."""


def setup(engine_settings: dict[str, t.Any]) -> bool:
    global CACHE  # pylint: disable=global-statement
    CACHE = EngineCache(engine_settings["name"])
    return True


def _get_ttwid() -> str:
    """Fetch a ttwid cookie from ByteDance's registration API.

    The ttwid helps avoid anti-bot detection (isCheating=2) on some
    search tabs. The cookie is stored in EngineCache with a 1-hour TTL.
    """
    cached: str | None = CACHE.get(_TTWID_CACHE_KEY)
    if cached:
        return cached

    try:
        resp: SXNG_Response = http_post(
            _TTWID_REGISTER_URL,
            json={
                "region": "cn",
                "aid": 24,
                "needFid": False,
                "service": "so.toutiao.com",
                "cbUrlProtocol": "https",
                "union": True,
            },
        )
        ttwid_value: str | None = resp.cookies.get("ttwid")
        if ttwid_value:
            CACHE.set(
                key=_TTWID_CACHE_KEY,
                value=ttwid_value,
                expire=_TTWID_CACHE_EXPIRATION,
            )
            return ttwid_value
    except Exception as e:  # pylint: disable=broad-except
        log.error("Failed to fetch ttwid: %s", e)

    return ""


def request(query, params):
    """Build a Toutiao search request."""
    page_num = params["pageno"] - 1

    query_params = {
        "dvpf": "pc",
        "source": "search_subtab_switch",
        "keyword": query,
        "enable_druid_v2": "1",
        "pd": toutiao_pd,
        "from": toutiao_pd,
        "cur_tab_title": toutiao_pd,
        "action_type": "search_subtab_switch",
        "page_num": page_num,
    }

    if toutiao_pd == "synthesis":
        query_params["filter_vendor"] = toutiao_filter_vendor
        query_params["index_resource"] = toutiao_filter_vendor

    time_range = params["time_range"]
    if time_range in _TIME_RANGES:
        query_params["filter_period"] = time_range

    params["url"] = f"{base_url}/search?{urlencode(query_params)}"
    params["allow_redirects"] = False

    ttwid = _get_ttwid()
    if ttwid:
        params["cookies"]["ttwid"] = ttwid

    return params


def response(resp):
    """Parse Toutiao search results."""

    if resp.status_code == 302:
        location = resp.headers.get("Location", "")
        if "verify" in location or "captcha" in location:
            raise SearxEngineCaptchaException()

    dom = lxml_html.fromstring(resp.text)
    results = []

    for data in _iter_card_data(dom):
        if _is_captcha_card(data):
            raise SearxEngineCaptchaException(
                message=f"toutiao [{toutiao_pd}]: slide captcha challenge detected"
            )
        if toutiao_pd != "atlas":
            results.extend(_extract_results(data))

    if toutiao_pd == "atlas":
        return _parse_atlas(dom)

    return results


def _iter_card_data(dom):
    """Yield the ``data`` dict from each druid JSON card embedded in the page."""
    for node in dom.xpath(
        '//script[@data-druid-card-data-id and @type="application/json"]'
    ):
        try:
            card_data = json.loads(node.text_content())
        except (json.JSONDecodeError, ValueError):
            continue
        data = card_data.get("data", {})
        if isinstance(data, dict):
            yield data


def _is_captcha_card(data):
    """Return True when the card is a slide-captcha verification challenge."""
    if data.get("template_key") != "71-undefined" or data.get("cell_type") != 71:
        return False

    decision = data.get("decision_conf", "")
    if isinstance(decision, str):
        try:
            decision = json.loads(decision)
        except (json.JSONDecodeError, ValueError):
            return False
    return isinstance(decision, dict) and decision.get("type") == "verify"


def _extract_results(data):
    """Extract zero or more search results from a single card ``data`` dict."""
    display = data.get("display")

    if isinstance(display, list):
        results = []
        for item in display:
            if not isinstance(item, dict):
                continue
            item_display = item.get("display")
            if not isinstance(item_display, dict):
                item_display = {}
            result = _build_result(item, item_display)
            if result:
                results.append(result)
        return results

    display_dict = display if isinstance(display, dict) else {}

    if _is_weitoutiao(data, display_dict):
        result = _build_weitoutiao_result(data)
        return [result] if result else []

    result = _build_result(data, display_dict)
    return [result] if result else []


def _is_weitoutiao(data, display):
    """Weitoutiao cards carry content but no title field."""
    if not (data.get("content") or data.get("rich_content")):
        return False
    return not _get_title(data, display)


def _build_result(data, display):
    """Build a single result from unified title/url/content fields."""
    title = _get_title(data, display)
    url = _get_url(data, display)
    if not title or not url:
        return None

    return _enrich_result(
        {
            "title": title,
            "url": url,
            "content": _get_content(data, display),
        },
        data,
    )


def _build_weitoutiao_result(data):
    """Build a weitoutiao result using content as title."""
    content = data.get("content", "") or data.get("rich_content", "")
    if not content:
        return None

    content = html_to_text(unescape(content))
    media_name = data.get("media_name", "")

    first_line = content.split("\n")[0].strip()
    if len(first_line) > 80:
        title = first_line[:80] + "..."
    else:
        title = first_line
    if media_name:
        title = f"{media_name}: {title}"

    url = _get_url(data, {})
    if not url:
        return None

    return _enrich_result({"title": title, "url": url, "content": content}, data)


def _enrich_result(result, data):
    """Add optional publishedDate and thumbnail fields."""
    published_date = _get_published_date(data)
    if published_date:
        result["publishedDate"] = published_date

    thumbnail = _get_thumbnail(data)
    if thumbnail:
        result["thumbnail"] = thumbnail

    return result


def _parse_atlas(dom):
    """Parse image (图片) results from the HTML DOM.

    Each image card is a ``<div data-log-extra>`` element containing an
    ``<img>`` for the thumbnail, an ``<a>`` with the source page URL
    (wrapped in a search redirect), and a ``<span class="text-underline-hover">``
    with the resolution string (e.g. "800x600").
    """
    results = []

    for card in dom.xpath("//div[@data-log-extra]"):
        imgs = card.xpath(".//img")
        if not imgs:
            continue
        img_src = imgs[0].get("src", "")
        if not img_src:
            continue

        title = ""
        source_url = img_src
        links = card.xpath(".//a")
        if links:
            title = links[0].text_content().strip()
            href = links[0].get("href", "")
            if href:
                source_url = _extract_redirect_url(href) or href

        # thumbnail_src keeps CDN crop params; img_src strips them for the original
        thumbnail_src = img_src
        img_original = re.sub(r"~tplv-[^.]+", "", img_src)

        resolution = ""
        res_spans = card.xpath('.//span[contains(@class, "text-underline-hover")]')
        if res_spans:
            resolution = res_spans[0].text_content().strip()

        results.append(
            {
                "template": "images.html",
                "url": source_url,
                "thumbnail_src": thumbnail_src,
                "img_src": img_original,
                "title": title or "",
                "resolution": resolution,
            }
        )

    return results


def _extract_redirect_url(href):
    """Extract the final destination URL from a Toutiao search redirect chain."""
    url = _unwrap_redirect_param(href)
    if not url:
        return None
    inner = _unwrap_redirect_param(url)
    return inner or url


def _unwrap_redirect_param(url):
    """Return the ``url`` query parameter value, or None."""
    qs = parse_qs(urlparse(url).query)
    url_vals = qs.get("url") or []
    if not url_vals or not url_vals[0]:
        return None
    return unquote(url_vals[0])


def _extract_sslocal_tid(data):
    """Extract the thread id (tid) from an sslocal:// deep-link."""
    for key in ("schema", "source_url", "pc_schema", "comment_schema"):
        value = data.get(key, "")
        if isinstance(value, str) and value.startswith("sslocal://"):
            qs = parse_qs(urlparse(value).query)
            tid = qs.get("tid", [""])[0]
            if tid and tid.isdigit() and tid != "0":
                return tid
    return None


def _get_title(data, display):
    """Extract title with fallback chain."""
    title = ""
    if isinstance(display, dict):
        title_field = display.get("title")
        if isinstance(title_field, dict):
            title = title_field.get("text", "")
        elif isinstance(title_field, str):
            title = title_field
    title = title or data.get("title", "") or ""
    if title:
        title = html_to_text(unescape(title))
    return title


def _get_url(data, display):
    """Extract URL, preferring direct links over redirect URLs."""

    def _is_valid_url(val):
        if not isinstance(val, str) or not val:
            return False
        return not val.startswith("sslocal://") and "preview_article" not in val

    def _normalize_url(val):
        if not _is_valid_url(val):
            return ""
        if val.startswith("/"):
            return f"https://www.toutiao.com{val}"
        return val

    info_url = ""
    if isinstance(display, dict):
        info = display.get("info")
        if isinstance(info, dict):
            info_url = info.get("url", "")

    url_candidates = (
        info_url,
        data.get("pc_schema", ""),
        data.get("article_url", ""),
        data.get("url", ""),
        data.get("source_url", ""),
    )

    for url in url_candidates:
        normalized_url = _normalize_url(url)
        if normalized_url:
            return normalized_url

    group_id = data.get("group_id") or data.get("id") or _extract_sslocal_tid(data)
    if group_id:
        return f"https://www.toutiao.com/a{group_id}/"

    return ""


def _get_content(data, display):
    """Extract summary content with fallback chain."""
    content = data.get("abstract", "")
    if isinstance(display, dict):
        summary = display.get("summary")
        if isinstance(summary, dict):
            content = summary.get("text", "") or content
    if content:
        content = html_to_text(unescape(content))
    return content


def _get_published_date(data):
    """Extract publish date from Unix timestamp fields."""
    timestamp = data.get("publish_time") or data.get("create_time")
    if timestamp:
        try:
            ts = int(timestamp)
            if ts > 0:
                return datetime.fromtimestamp(ts)  # noqa: DTZ006
        except (ValueError, TypeError, OSError):
            pass
    return None


def _get_thumbnail(data):
    """Extract thumbnail URL."""
    image_list = data.get("image_list")
    if image_list and isinstance(image_list, list):
        first_img = image_list[0]
        if isinstance(first_img, dict):
            img_url = first_img.get("url", "")
        elif isinstance(first_img, str):
            img_url = first_img
        else:
            img_url = ""
        if img_url:
            return img_url

    for key in (
        "image_url",
        "middle_image_url",
        "thumbnail_url",
        "large_thumbnail_url",
    ):
        img = data.get(key, "")
        if img:
            return img

    return None
