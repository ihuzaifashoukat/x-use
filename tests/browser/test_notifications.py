"""Notification parsing uses observed facts, without semantic guesses."""
from xuse.browser.notifications import parse_notification_snapshot
import pytest


def test_notification_unknown_semantics_and_post_preview_without_permalink():
    row = parse_notification_snapshot({"row_kind": "notification", "text": "Synthetic source text",
        "actors": [{"href": "/alpha", "avatar_handle": "alpha", "name": "Alpha"},
                   {"href": "/alpha", "avatar_handle": "alpha", "name": None}],
        "created_at": "2026-10-09T10:00:00Z", "timestamp_text": "Earlier",
        "post_text": "A quote saying liked your post is not event evidence", "post_links": [], "media": []})
    assert row["type"] == "unknown" and row["type_evidence"] is None
    assert row["unread"] is None and row["unread_source"] == "not_exposed"
    assert row["notification_id"] is None
    assert len(row["actors"]) == 1 and row["actors"][0]["handle"] == "alpha"
    assert row["created_at_source"] == "time_datetime"
    assert row["related_posts"][0]["post_id"] is None
    assert row["related_posts"][0]["text"].startswith("A quote")
    assert row["post_context_partial"]


def test_notification_canonical_references_and_bad_media_are_filtered():
    row = parse_notification_snapshot({"row_kind": "post", "text": "source",
        "actors": [{"href": "https://evil.invalid/alpha", "avatar_handle": "alpha"},
                   {"href": "/notifications", "avatar_handle": "notifications"}, {"href": "/beta", "avatar_handle": "beta"}],
        "post_links": ["https://evil.invalid/alpha/status/123", "/alpha/status/123abc", "/beta/status/321?ref=source"],
        "post_text": "Observed post", "created_at": "invalid", "media": [
            {"type": "image", "url": "https://pbs.twimg.com/media/synthetic.jpg", "alt_text": "Synthetic"},
            {"type": "image", "url": "https://private.invalid/source"}, {"type": "video", "url": "blob:private"}]})
    assert [actor["handle"] for actor in row["actors"]] == ["beta"]
    assert row["related_posts"][0]["url"] == "https://x.com/beta/status/321"
    assert row["related_posts"][0]["post_id"] == "321"
    assert len(row["related_posts"][0]["media"]) == 1
    assert row["created_at"] is None


def test_multiple_related_posts_do_not_invent_preview_attribution():
    row = parse_notification_snapshot({"row_kind": "notification", "post_links": ["/alpha/status/111", "/beta/status/222"],
                                      "post_text": "An ambiguous preview", "actors": [], "media": []})
    assert row["related_posts"][0]["text"] is None and row["related_posts"][1]["text"] is None
    assert row["related_posts"][2]["post_id"] is None


def test_avatar_identity_mismatch_and_unattributed_single_link_preview_stay_unknown():
    row = parse_notification_snapshot({"row_kind": "notification", "post_links": ["/alpha/status/111"],
        "post_text": "Preview with no structural association", "media": [],
        "actors": [{"href": "/alpha", "avatar_handle": "beta", "name": "Incorrect"}]})
    assert row["actors"] == []
    assert row["related_posts"][0]["post_id"] == "111" and row["related_posts"][0]["text"] is None
    assert row["related_posts"][1]["post_id"] is None and row["related_posts"][1]["text"]
    assert row["post_context_partial"]


@pytest.mark.parametrize("phrase,kind", [("followed you", "follow"), ("liked your post", "like"),
    ("liked your reply", "like"), ("liked 2 of your posts", "like")])
def test_only_observed_notification_action_copy_supports_type(phrase, kind):
    snapshot = {"row_kind": "notification", "action_copy": phrase,
                "actors": [{"href": "/alpha", "avatar_handle": "alpha"}]}
    row = parse_notification_snapshot(snapshot)
    assert row["type"] == kind
    assert row["type_evidence"] == {"source": "visible_notification_action_copy", "text": phrase}
    snapshot["row_kind"] = "post"
    assert parse_notification_snapshot(snapshot)["type"] == "unknown"
    snapshot["row_kind"] = "notification"
    snapshot["actors"] = []
    assert parse_notification_snapshot(snapshot)["type"] == "unknown"
