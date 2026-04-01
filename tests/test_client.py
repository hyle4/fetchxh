from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from fetchxh.client import (
    FetchxhConfig,
    _build_cookie_header_from_x_state,
    _request_timeline_with_recovery,
    extract_bottom_cursor,
    extract_full_texts,
    extract_posts,
    renew_runtime_config,
)

CLIENT_MODULE = renew_runtime_config.__module__


def _config(home_query_id: str = "home-1", latest_query_id: str = "latest-1") -> FetchxhConfig:
    return FetchxhConfig(
        home_timeline_query_id=home_query_id,
        home_latest_timeline_query_id=latest_query_id,
        authorization_bearer="Bearer test",
        x_csrf_token="csrf",
        cookie_header="auth_token=a; ct0=b",
        user_agent="ua",
    )


class ExtractFullTextsTests(unittest.TestCase):
    def test_extracts_only_timeline_items_and_handles_retweets(self) -> None:
        payload = {
            "data": {
                "home": {
                    "home_timeline_urt": {
                        "instructions": [
                            {
                                "type": "TimelineAddEntries",
                                "entries": [
                                    {
                                        "content": {
                                            "entryType": "TimelineTimelineCursor",
                                            "cursorType": "Bottom",
                                            "value": "bottom-cursor-value",
                                        }
                                    },
                                    {
                                        "content": {
                                            "entryType": "TimelineTimelineItem",
                                            "itemContent": {
                                                "tweet_results": {
                                                    "result": {
                                                        "core": {
                                                            "user_results": {
                                                                "result": {
                                                                    "legacy": {
                                                                        "screen_name": "directuser",
                                                                    }
                                                                }
                                                            }
                                                        },
                                                        "legacy": {
                                                            "created_at": "Wed Oct 10 20:19:24 +0000 2018",
                                                            "full_text": "direct post text",
                                                            "id_str": "111",
                                                        }
                                                    }
                                                }
                                            },
                                        }
                                    },
                                    {
                                        "content": {
                                            "entryType": "TimelineTimelineItem",
                                            "itemContent": {
                                                "tweet_results": {
                                                    "result": {
                                                        "tweet": {
                                                            "core": {
                                                                "user_results": {
                                                                    "result": {
                                                                        "core": {
                                                                            "screen_name": "retweetuser",
                                                                        }
                                                                    }
                                                                }
                                                            },
                                                            "legacy": {
                                                                "created_at": "Thu Oct 11 12:00:00 +0000 2018",
                                                                "full_text": "RT @origuser: short wrapper",
                                                                "id_str": "222",
                                                                "retweeted_status_result": {
                                                                    "result": {
                                                                        "core": {
                                                                            "user_results": {
                                                                                "result": {
                                                                                    "core": {
                                                                                        "screen_name": "origuser",
                                                                                    }
                                                                                }
                                                                            }
                                                                        },
                                                                        "legacy": {
                                                                            "created_at": "Thu Oct 11 11:00:00 +0000 2018",
                                                                            "full_text": "full original retweeted text",
                                                                            "id_str": "999",
                                                                        },
                                                                    }
                                                                },
                                                            }
                                                        }
                                                    }
                                                }
                                            },
                                        }
                                    },
                                    {
                                        "content": {
                                            "entryType": "TimelineTimelineCursor",
                                        }
                                    },
                                    {
                                        "content": {
                                            "entryType": "TimelineTimelineItem",
                                            "itemContent": {
                                                "tweet_results": {
                                                    "result": {
                                                        "legacy": {}
                                                    }
                                                }
                                            },
                                        }
                                    },
                                ],
                            },
                            {
                                "type": "TimelineTerminateTimeline",
                            },
                        ]
                    }
                }
            }
        }

        self.assertEqual(
            extract_full_texts(payload),
            ["direct post text", "RT @origuser: full original retweeted text"],
        )
        posts = extract_posts(payload)
        self.assertEqual([post.account_handle for post in posts], ["directuser", "retweetuser"])
        self.assertEqual([post.tweet_id for post in posts], ["111", "222"])
        self.assertEqual([post.has_media for post in posts], [False, False])
        self.assertEqual(extract_bottom_cursor(payload), "bottom-cursor-value")

    def test_marks_posts_with_media(self) -> None:
        payload = {
            "data": {
                "home": {
                    "home_timeline_urt": {
                        "instructions": [
                            {
                                "type": "TimelineAddEntries",
                                "entries": [
                                    {
                                        "content": {
                                            "entryType": "TimelineTimelineItem",
                                            "itemContent": {
                                                "tweet_results": {
                                                    "result": {
                                                        "core": {
                                                            "user_results": {
                                                                "result": {
                                                                    "core": {
                                                                        "screen_name": "mediauser",
                                                                    }
                                                                }
                                                            }
                                                        },
                                                        "legacy": {
                                                            "created_at": "Wed Oct 10 20:19:24 +0000 2018",
                                                            "full_text": "post with media",
                                                            "id_str": "333",
                                                            "entities": {
                                                                "media": [{"type": "photo"}],
                                                            },
                                                        },
                                                    }
                                                }
                                            },
                                        }
                                    }
                                ],
                            }
                        ]
                    }
                }
            }
        }

        posts = extract_posts(payload)
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0].has_media)
        self.assertEqual(posts[0].text, "post with media")

    def test_prefers_note_tweet_text_when_present(self) -> None:
        payload = {
            "data": {
                "home": {
                    "home_timeline_urt": {
                        "instructions": [
                            {
                                "type": "TimelineAddEntries",
                                "entries": [
                                    {
                                        "content": {
                                            "entryType": "TimelineTimelineItem",
                                            "itemContent": {
                                                "tweet_results": {
                                                    "result": {
                                                        "core": {
                                                            "user_results": {
                                                                "result": {
                                                                    "core": {
                                                                        "screen_name": "noteuser",
                                                                    }
                                                                }
                                                            }
                                                        },
                                                        "legacy": {
                                                            "created_at": "Wed Oct 10 20:19:24 +0000 2018",
                                                            "full_text": "truncated preview…",
                                                            "id_str": "444",
                                                        },
                                                        "note_tweet": {
                                                            "note_tweet_results": {
                                                                "result": {
                                                                    "text": "full note tweet text",
                                                                }
                                                            }
                                                        },
                                                    }
                                                }
                                            },
                                        }
                                    }
                                ],
                            }
                        ]
                    }
                }
            }
        }

        posts = extract_posts(payload)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].text, "full note tweet text")


class RuntimeConfigTests(unittest.TestCase):
    def test_prefers_x_cookies_over_twitter_duplicates(self) -> None:
        payload = {
            "cookies": [
                {"domain": ".twitter.com", "name": "auth_token", "value": "old-auth"},
                {"domain": ".twitter.com", "name": "ct0", "value": "old-csrf"},
                {"domain": ".x.com", "name": "auth_token", "value": "new-auth"},
                {"domain": ".x.com", "name": "ct0", "value": "new-csrf"},
                {"domain": ".x.com", "name": "lang", "value": "en"},
            ]
        }

        cookie_header, csrf_token = _build_cookie_header_from_x_state(payload)

        self.assertEqual(cookie_header, "auth_token=new-auth; ct0=new-csrf; lang=en")
        self.assertEqual(csrf_token, "new-csrf")

    def test_request_timeline_with_recovery_retries_with_renewed_config(self) -> None:
        session = Mock()
        first_response = Mock(status_code=503)
        second_response = Mock(status_code=200)
        session.get.side_effect = [first_response, second_response]

        initial = _config(home_query_id="old-home")
        renewed = _config(home_query_id="new-home")

        with patch(f"{CLIENT_MODULE}.renew_runtime_config", return_value=renewed) as renew:
            response, config = _request_timeline_with_recovery(
                url_attr="home_timeline_url",
                variables={"count": 1},
                session=session,
                config=initial,
            )

        self.assertIs(response, second_response)
        self.assertIs(config, renewed)
        renew.assert_called_once_with(interactive=False)
        self.assertEqual(session.get.call_args_list[0].args[0], initial.home_timeline_url)
        self.assertEqual(session.get.call_args_list[1].args[0], renewed.home_timeline_url)

    def test_interactive_renew_uses_visible_login_fallback(self) -> None:
        calls: list[tuple[bool, bool]] = []
        result = _config()

        def fake_refresher(*, headless: bool, force_login: bool, delay_ms: int = 1200, timeout_ms: int = 15000) -> None:
            calls.append((headless, force_login))
            if headless:
                raise RuntimeError("saved session expired")

        with patch("fetchxh.session.renew_x_session_state", side_effect=fake_refresher):
            with patch(f"{CLIENT_MODULE}.refresh_runtime_config", return_value=result) as refresh:
                renewed = renew_runtime_config(interactive=True)

        self.assertIs(renewed, result)
        self.assertEqual(calls, [(True, False), (False, True)])
        refresh.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
