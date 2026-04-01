from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .paths import first_existing_path, preferred_x_state_path, state_roots

_QUERY_ID_RE = rb"([A-Za-z0-9_-]{20,32})"
_BEARER_RE = re.compile(rb"Bearer AAAAAAAAAAAAAAAAAAAA[^\x00\"'\s\\]+")
_HOME_RE = re.compile(_QUERY_ID_RE + rb"/HomeTimeline")
_HOME_LATEST_RE = re.compile(_QUERY_ID_RE + rb"/HomeLatestTimeline")
_TRAILING_TCO_RE = re.compile(r"(?:\s*https://t\.co/[A-Za-z0-9]+)+\s*$")
_RENEWABLE_STATUS_CODES = {401, 403, 404, 500, 502, 503, 504}

DEFAULT_USER_AGENT = os.environ.get("FETCHXH_USER_AGENT", "").strip() or (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.7680.167 Safari/537.36"
)
DEFAULT_HOME_TIMELINE_QUERY_ID = os.environ.get("FETCHXH_HOME_TIMELINE_QUERY_ID", "").strip()
DEFAULT_HOME_LATEST_TIMELINE_QUERY_ID = os.environ.get("FETCHXH_HOME_LATEST_TIMELINE_QUERY_ID", "").strip()
DEFAULT_AUTHORIZATION_BEARER = os.environ.get("FETCHXH_AUTHORIZATION_BEARER", "").strip()
DEFAULT_X_CSRF_TOKEN = os.environ.get("FETCHXH_X_CSRF_TOKEN", "").strip()
DEFAULT_COOKIE_HEADER = os.environ.get("FETCHXH_COOKIE_HEADER", "").strip()

REQUEST_FEATURES: dict[str, Any] = {
    "rweb_video_screen_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": False,
    "rweb_tipjar_consumption_enabled": False,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "responsive_web_jetfuel_frame": True,
    "responsive_web_grok_share_attachment_enabled": True,
    "responsive_web_grok_annotations_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "content_disclosure_indicator_enabled": True,
    "content_disclosure_ai_generated_indicator_enabled": True,
    "responsive_web_grok_show_grok_translated_post": False,
    "responsive_web_grok_analysis_button_from_backend": True,
    "post_ctas_fetch_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": False,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": False,
    "responsive_web_enhance_cards_enabled": False,
}


@dataclass(frozen=True, slots=True)
class TimelineTextPost:
    account_handle: str
    posted_at: datetime
    text: str
    tweet_id: str
    has_media: bool = False

    @property
    def url(self) -> str:
        return f"https://x.com/{self.account_handle}/status/{self.tweet_id}"


@dataclass(frozen=True, slots=True)
class FetchxhConfig:
    home_timeline_query_id: str
    home_latest_timeline_query_id: str
    authorization_bearer: str
    x_csrf_token: str
    cookie_header: str
    user_agent: str

    @property
    def home_timeline_url(self) -> str:
        return f"https://x.com/i/api/graphql/{self.home_timeline_query_id}/HomeTimeline"

    @property
    def home_latest_timeline_url(self) -> str:
        return f"https://x.com/i/api/graphql/{self.home_latest_timeline_query_id}/HomeLatestTimeline"

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": self.authorization_bearer,
            "x-csrf-token": self.x_csrf_token,
            "cookie": self.cookie_header,
            "User-Agent": self.user_agent,
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "en",
        }

    @property
    def missing_fields(self) -> list[str]:
        missing: list[str] = []
        if not self.home_timeline_query_id:
            missing.append("home_timeline_query_id")
        if not self.home_latest_timeline_query_id:
            missing.append("home_latest_timeline_query_id")
        if not self.authorization_bearer:
            missing.append("authorization_bearer")
        if not self.x_csrf_token:
            missing.append("x_csrf_token")
        if not self.cookie_header:
            missing.append("cookie_header")
        return missing

    @property
    def is_ready(self) -> bool:
        return not self.missing_fields


class FetchxhError(RuntimeError):
    pass


RUNTIME_CONFIG = FetchxhConfig(
    home_timeline_query_id=DEFAULT_HOME_TIMELINE_QUERY_ID,
    home_latest_timeline_query_id=DEFAULT_HOME_LATEST_TIMELINE_QUERY_ID,
    authorization_bearer=DEFAULT_AUTHORIZATION_BEARER,
    x_csrf_token=DEFAULT_X_CSRF_TOKEN,
    cookie_header=DEFAULT_COOKIE_HEADER,
    user_agent=DEFAULT_USER_AGENT,
)

def _x_state_path() -> Path:
    return first_existing_path("x_state.json") or preferred_x_state_path()


def _coalesce(*values: str | None) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _candidate_profile_dirs() -> list[Path]:
    profiles: list[Path] = []
    seen: set[Path] = set()

    for root in state_roots():
        for candidate in (root / "browser_profile", root / "uc_profile"):
            if candidate.exists() and candidate not in seen:
                seen.add(candidate)
                profiles.append(candidate)

        active = root / ".active_profile"
        try:
            active_name = active.read_text(encoding="utf-8").strip()
        except OSError:
            active_name = ""
        if active_name:
            candidate = root / active_name
            if candidate.exists() and candidate not in seen:
                seen.add(candidate)
                profiles.append(candidate)
    return profiles


def _code_cache_roots() -> list[Path]:
    roots: list[Path] = []
    for profile_dir in _candidate_profile_dirs():
        roots.extend(
            [
                profile_dir / "Default" / "Code Cache" / "js",
                profile_dir / "Default" / "Service Worker" / "ScriptCache",
            ]
        )
    return [path for path in roots if path.exists()]


def _load_x_state() -> dict[str, Any]:
    path = _x_state_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise FetchxhError(f"Could not read {path}.") from exc
    except json.JSONDecodeError as exc:
        raise FetchxhError(f"{path} is not valid JSON.") from exc


def _build_cookie_header_from_x_state(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    cookies = payload.get("cookies")
    if not isinstance(cookies, list):
        return None, None

    names_in_order = [
        "auth_token",
        "ct0",
        "twid",
        "kdt",
        "att",
        "lang",
        "guest_id",
        "guest_id_ads",
        "guest_id_marketing",
        "personalization_id",
    ]

    values: dict[str, str] = {}
    priorities: dict[str, int] = {}
    for item in cookies:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain") or "")
        normalized = domain.lstrip(".").lower()
        if normalized == "x.com" or normalized.endswith(".x.com"):
            priority = 0
        elif normalized == "twitter.com" or normalized.endswith(".twitter.com"):
            priority = 1
        else:
            continue
        name = item.get("name")
        value = item.get("value")
        if isinstance(name, str) and isinstance(value, str):
            current_priority = priorities.get(name)
            if current_priority is not None and current_priority <= priority:
                continue
            values[name] = value
            priorities[name] = priority

    if "auth_token" not in values or "ct0" not in values:
        return None, None

    cookie_parts = [f"{name}={values[name]}" for name in names_in_order if name in values]
    return "; ".join(cookie_parts), values.get("ct0")


def _scan_binary_text(root: Path) -> tuple[str | None, str | None, str | None]:
    bearer: str | None = None
    home_query_id: str | None = None
    latest_query_id: str | None = None

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue

        if bearer is None:
            match = _BEARER_RE.search(data)
            if match:
                bearer = match.group(0).decode("utf-8", errors="ignore")

        if home_query_id is None:
            match = _HOME_RE.search(data)
            if match:
                home_query_id = match.group(1).decode("utf-8", errors="ignore")

        if latest_query_id is None:
            match = _HOME_LATEST_RE.search(data)
            if match:
                latest_query_id = match.group(1).decode("utf-8", errors="ignore")

        if bearer and home_query_id and latest_query_id:
            break

    return bearer, home_query_id, latest_query_id


def discover_runtime_config() -> FetchxhConfig:
    payload = _load_x_state()
    cookie_header, csrf_token = _build_cookie_header_from_x_state(payload) if payload else (None, None)

    bearer = None
    home_query_id = None
    latest_query_id = None
    for root in _code_cache_roots():
        found_bearer, found_home, found_latest = _scan_binary_text(root)
        bearer = bearer or found_bearer
        home_query_id = home_query_id or found_home
        latest_query_id = latest_query_id or found_latest
        if bearer and home_query_id and latest_query_id:
            break

    return FetchxhConfig(
        home_timeline_query_id=_coalesce(home_query_id, DEFAULT_HOME_TIMELINE_QUERY_ID),
        home_latest_timeline_query_id=_coalesce(latest_query_id, DEFAULT_HOME_LATEST_TIMELINE_QUERY_ID),
        authorization_bearer=_coalesce(bearer, DEFAULT_AUTHORIZATION_BEARER),
        x_csrf_token=_coalesce(csrf_token, DEFAULT_X_CSRF_TOKEN),
        cookie_header=_coalesce(cookie_header, DEFAULT_COOKIE_HEADER),
        user_agent=DEFAULT_USER_AGENT,
    )


def refresh_runtime_config() -> FetchxhConfig:
    global RUNTIME_CONFIG
    RUNTIME_CONFIG = discover_runtime_config()
    return RUNTIME_CONFIG


def renew_runtime_config(*, interactive: bool = False) -> FetchxhConfig:
    try:
        from .session import SessionRefreshUnavailable, renew_x_session_state
    except ImportError:
        if interactive:
            raise FetchxhError(
                "Automatic renewal is unavailable because the browser session refresher is not installed."
            )
        return refresh_runtime_config()

    attempts = [(True, False)]
    if interactive:
        attempts.append((False, True))

    last_error: Exception | None = None
    for headless, force_login in attempts:
        try:
            renew_x_session_state(headless=headless, force_login=force_login)
            return refresh_runtime_config()
        except SessionRefreshUnavailable as exc:
            if interactive:
                raise FetchxhError(str(exc)) from exc
            return refresh_runtime_config()
        except Exception as exc:
            last_error = exc

    if interactive and last_error is not None:
        raise FetchxhError(f"Could not renew the local X session automatically: {last_error}") from last_error
    return refresh_runtime_config()


def build_home_timeline_variables(count: int = 54, cursor: str | None = None) -> dict[str, Any]:
    return {
        "count": count,
        **({"cursor": cursor} if cursor else {}),
        "includePromotedContent": True,
        "latestControlAvailable": True,
        "requestContext": "launch",
        "seenTweetIds": [],
    }


def build_home_latest_timeline_variables(count: int = 54, cursor: str | None = None) -> dict[str, Any]:
    return {
        "count": count,
        **({"cursor": cursor} if cursor else {}),
        "enableRanking": True,
        "includePromotedContent": True,
        "requestContext": "launch",
    }


def build_params(variables: dict[str, Any]) -> dict[str, str]:
    return {
        "variables": json.dumps(variables, separators=(",", ":")),
        "features": json.dumps(REQUEST_FEATURES, separators=(",", ":")),
    }


def _request_timeline(
    url: str,
    variables: dict[str, Any],
    headers: dict[str, str],
    session: requests.Session,
) -> requests.Response:
    return session.get(
        url,
        headers=headers,
        params=build_params(variables),
        timeout=30,
    )


def _request_timeline_with_recovery(
    *,
    url_attr: str,
    variables: dict[str, Any],
    session: requests.Session,
    config: FetchxhConfig,
) -> tuple[requests.Response, FetchxhConfig]:
    current = config
    _require_ready_config(current)

    for attempt in range(2):
        response = _request_timeline(
            getattr(current, url_attr),
            variables,
            current.headers,
            session,
        )
        if response.status_code not in _RENEWABLE_STATUS_CODES or attempt == 1:
            return response, current
        current = renew_runtime_config(interactive=False)

    raise AssertionError("unreachable")


def _require_ready_config(config: FetchxhConfig) -> None:
    if config.is_ready:
        return
    missing = ", ".join(config.missing_fields)
    raise FetchxhError(
        "fetchxh is missing local X session data "
        f"({missing}). Choose 'Renew auth' from the menu or set the FETCHXH_* environment variables."
    )


def fetch_home_timeline(
    count: int = 54,
    session: requests.Session | None = None,
    config: FetchxhConfig | None = None,
) -> dict[str, Any]:
    current = config or RUNTIME_CONFIG
    client = session or requests.Session()

    response, current = _request_timeline_with_recovery(
        url_attr="home_timeline_url",
        variables=build_home_timeline_variables(count),
        session=client,
        config=current,
    )

    if response.status_code in (401, 403):
        raise PermissionError(
            "X returned an authentication error. Renew the local session from the menu or refresh the X login state."
        )

    if response.status_code >= 400:
        response, current = _request_timeline_with_recovery(
            url_attr="home_latest_timeline_url",
            variables=build_home_latest_timeline_variables(count),
            session=client,
            config=current,
        )

    if response.status_code in (401, 403):
        raise PermissionError(
            "X returned an authentication error. Renew the local session from the menu or refresh the X login state."
        )

    response.raise_for_status()
    payload = json.loads(response.content.decode("utf-8"))
    if not isinstance(payload, dict):
        raise FetchxhError("The HomeTimeline response was not a JSON object.")
    return payload


def get_timeline_instructions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    current: Any = payload
    for key in ("data", "home", "home_timeline_urt", "instructions"):
        if isinstance(current, dict):
            current = current.get(key)
        else:
            current = None
        if current is None:
            raise FetchxhError(
                "Could not find data -> home -> home_timeline_urt -> instructions in the response payload."
            )

    if not isinstance(current, list):
        raise FetchxhError("The instructions node was present but was not a list.")
    return [item for item in current if isinstance(item, dict)]


def extract_bottom_cursor(payload: dict[str, Any]) -> str | None:
    for instruction in get_timeline_instructions(payload):
        if instruction.get("type") != "TimelineAddEntries":
            continue

        entries = instruction.get("entries")
        if not isinstance(entries, list):
            continue

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            content = entry.get("content")
            if not isinstance(content, dict):
                continue
            if content.get("entryType") != "TimelineTimelineCursor":
                continue
            if content.get("cursorType") != "Bottom":
                continue
            value = content.get("value")
            if isinstance(value, str) and value:
                return value
    return None


def _unwrap_result(result: Any) -> dict[str, Any]:
    current = result
    while isinstance(current, dict):
        if "tweet" in current and isinstance(current["tweet"], dict):
            current = current["tweet"]
            continue
        if "result" in current and isinstance(current["result"], dict) and "legacy" not in current:
            current = current["result"]
            continue
        break
    return current if isinstance(current, dict) else {}


def _extract_tweet_result_from_entry(entry: dict[str, Any]) -> dict[str, Any]:
    content = entry.get("content")
    if not isinstance(content, dict):
        return {}
    if content.get("entryType") != "TimelineTimelineItem":
        return {}

    item_content = content.get("itemContent")
    if not isinstance(item_content, dict):
        return {}

    tweet_results = item_content.get("tweet_results")
    if not isinstance(tweet_results, dict):
        return {}

    return _unwrap_result(tweet_results.get("result"))


def _parse_twitter_timestamp(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        return None


def _clean_display_text(text: str, has_media: bool) -> str:
    cleaned = html.unescape(text).strip()
    if has_media:
        cleaned = _TRAILING_TCO_RE.sub("", cleaned).rstrip()
    return cleaned


def _extract_note_tweet_text(node: dict[str, Any]) -> str | None:
    for key in ("note_tweet", "note_tweet_results"):
        value = node.get(key)
        if not isinstance(value, dict):
            continue
        if key == "note_tweet":
            value = value.get("note_tweet_results")
        if not isinstance(value, dict):
            continue
        result = value.get("result")
        if not isinstance(result, dict):
            continue
        text = result.get("text")
        if isinstance(text, str) and text.strip():
            return text
    return None


def _extract_display_text_from_node(node: dict[str, Any], has_media: bool) -> str | None:
    note_text = _extract_note_tweet_text(node)
    if note_text:
        return _clean_display_text(note_text, has_media)

    legacy = node.get("legacy")
    if not isinstance(legacy, dict):
        return None

    full_text = legacy.get("full_text")
    if isinstance(full_text, str) and full_text.strip():
        return _clean_display_text(full_text, has_media)
    return None


def _extract_retweeted_node(result: dict[str, Any]) -> dict[str, Any]:
    legacy = result.get("legacy")
    if not isinstance(legacy, dict):
        return {}
    nested = legacy.get("retweeted_status_result")
    return _unwrap_result(nested)


def _extract_post_from_entry(entry: dict[str, Any]) -> TimelineTextPost | None:
    result = _extract_tweet_result_from_entry(entry)
    if not result:
        return None

    legacy = result.get("legacy")
    if not isinstance(legacy, dict):
        return None

    created_at = legacy.get("created_at")
    tweet_id = legacy.get("id_str") or result.get("rest_id")
    entities = legacy.get("entities")
    has_media = isinstance(entities, dict) and bool(entities.get("media"))

    core = result.get("core")
    if not isinstance(core, dict):
        return None
    user_results = core.get("user_results")
    if not isinstance(user_results, dict):
        return None
    user_result = _unwrap_result(user_results.get("result"))
    user_legacy = user_result.get("legacy")
    user_core = user_result.get("core")
    screen_name = None
    if isinstance(user_core, dict):
        core_screen_name = user_core.get("screen_name")
        if isinstance(core_screen_name, str) and core_screen_name:
            screen_name = core_screen_name
    if screen_name is None and isinstance(user_legacy, dict):
        legacy_screen_name = user_legacy.get("screen_name")
        if isinstance(legacy_screen_name, str) and legacy_screen_name:
            screen_name = legacy_screen_name

    if not isinstance(created_at, str):
        return None
    if not isinstance(tweet_id, str) or not tweet_id:
        return None
    if not isinstance(screen_name, str) or not screen_name:
        return None

    posted_at = _parse_twitter_timestamp(created_at)
    if posted_at is None:
        return None

    display_text = _extract_display_text_from_node(result, has_media)

    retweeted_node = _extract_retweeted_node(result)
    if retweeted_node:
        retweeted_legacy = retweeted_node.get("legacy")
        retweeted_entities = retweeted_legacy.get("entities") if isinstance(retweeted_legacy, dict) else None
        retweeted_has_media = isinstance(retweeted_entities, dict) and bool(retweeted_entities.get("media"))
        retweeted_text = _extract_display_text_from_node(retweeted_node, retweeted_has_media)
        retweeted_user = (
            retweeted_node.get("core", {})
            .get("user_results", {})
            .get("result", {})
            .get("core", {})
            .get("screen_name")
        )
        if isinstance(retweeted_text, str) and retweeted_text:
            if isinstance(retweeted_user, str) and retweeted_user:
                display_text = f"RT @{retweeted_user}: {retweeted_text}"
            else:
                display_text = retweeted_text
            has_media = has_media or retweeted_has_media

    if not isinstance(display_text, str) or (not display_text and not has_media):
        return None

    return TimelineTextPost(
        account_handle=screen_name.lstrip("@"),
        posted_at=posted_at,
        text=display_text,
        tweet_id=tweet_id,
        has_media=has_media,
    )


def extract_posts(payload: dict[str, Any]) -> list[TimelineTextPost]:
    posts: list[TimelineTextPost] = []
    seen_ids: set[str] = set()

    for instruction in get_timeline_instructions(payload):
        if instruction.get("type") != "TimelineAddEntries":
            continue

        entries = instruction.get("entries")
        if not isinstance(entries, list):
            continue

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            post = _extract_post_from_entry(entry)
            if post is None or post.tweet_id in seen_ids:
                continue
            seen_ids.add(post.tweet_id)
            posts.append(post)

    return posts


def extract_full_texts(payload: dict[str, Any]) -> list[str]:
    return [post.text for post in extract_posts(payload)]


def _fetch_timeline_payload(
    url_attr: str,
    variables_builder: Any,
    count: int,
    cursor: str | None,
    session: requests.Session,
    config: FetchxhConfig,
) -> tuple[dict[str, Any], FetchxhConfig]:
    response, current = _request_timeline_with_recovery(
        url_attr=url_attr,
        variables=variables_builder(count, cursor),
        session=session,
        config=config,
    )

    if response.status_code in (401, 403):
        raise PermissionError(
            "X returned an authentication error. Renew the local session from the menu or refresh the X login state."
        )

    response.raise_for_status()
    payload = json.loads(response.content.decode("utf-8"))
    if not isinstance(payload, dict):
        raise FetchxhError("The timeline response was not a JSON object.")
    return payload, current


def _collect_posts(
    *,
    label: str,
    url_attr: str,
    variables_builder: Any,
    count: int,
    session: requests.Session | None = None,
    config: FetchxhConfig | None = None,
) -> list[TimelineTextPost]:
    current = config or RUNTIME_CONFIG
    client = session or requests.Session()
    posts: list[TimelineTextPost] = []
    seen_ids: set[str] = set()
    cursor: str | None = None

    for _ in range(8):
        payload, current = _fetch_timeline_payload(url_attr, variables_builder, count, cursor, client, current)
        for post in extract_posts(payload):
            if post.tweet_id in seen_ids:
                continue
            seen_ids.add(post.tweet_id)
            posts.append(post)
            if len(posts) >= count:
                return posts[:count]

        cursor = extract_bottom_cursor(payload)
        if not cursor:
            break

    raise FetchxhError(f"Could only collect {len(posts)} text posts from {label}; expected {count}.")


def fetch_for_you_posts(
    count: int = 54,
    session: requests.Session | None = None,
    config: FetchxhConfig | None = None,
) -> list[TimelineTextPost]:
    current = config or RUNTIME_CONFIG
    return _collect_posts(
        label="For You",
        url_attr="home_timeline_url",
        variables_builder=build_home_timeline_variables,
        count=count,
        session=session,
        config=current,
    )


def fetch_following_posts(
    count: int = 54,
    session: requests.Session | None = None,
    config: FetchxhConfig | None = None,
) -> list[TimelineTextPost]:
    current = config or RUNTIME_CONFIG
    return _collect_posts(
        label="Following",
        url_attr="home_latest_timeline_url",
        variables_builder=build_home_latest_timeline_variables,
        count=count,
        session=session,
        config=current,
    )
