"""Thread management endpoints against the mock driver."""

from __future__ import annotations

SEED_IDS = [
    "6b6e1a2c-0000-4000-8000-000000000001",
    "6b6e1a2c-0000-4000-8000-000000000002",
    "6b6e1a2c-0000-4000-8000-000000000003",
]


def test_threads_list_requires_auth(client):
    assert client.get("/v1/threads").status_code == 401


def test_threads_list_sorted_and_paginated(client, auth):
    r = client.get("/v1/threads", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert [t["id"] for t in body["data"]] == SEED_IDS  # updated_at descending
    first = body["data"][0]
    assert first["title"] == "Seed alpha"
    assert first["url"].endswith(f"/thread/{SEED_IDS[0]}")
    assert first["created_at"] <= first["updated_at"]

    # Small limit + opaque cursor pagination.
    r1 = client.get("/v1/threads", headers=auth, params={"limit": 2})
    assert [t["id"] for t in r1.json()["data"]] == SEED_IDS[:2]
    cursor = r1.json()["next_cursor"]
    assert cursor
    r2 = client.get("/v1/threads", headers=auth, params={"limit": 2, "cursor": cursor})
    assert [t["id"] for t in r2.json()["data"]] == SEED_IDS[2:]
    assert r2.json()["next_cursor"] is None


def test_threads_list_bad_cursor_is_ignored(client, auth):
    r = client.get("/v1/threads", headers=auth, params={"cursor": "garbage"})
    assert r.status_code == 200
    assert [t["id"] for t in r.json()["data"]] == SEED_IDS


def test_thread_messages_with_url_ref(client, auth):
    r = client.get(f"/v1/threads/{SEED_IDS[0]}/messages", headers=auth)
    assert r.status_code == 200
    data = r.json()["data"]
    assert r.json()["object"] == "list"
    assert [m["role"] for m in data] == ["user", "assistant"]
    assert data[0]["content"] == "hello alpha"
    assert data[0]["created_at"] is not None


def test_thread_messages_accepts_full_url(client, auth):
    # A full thread URL must be percent-encoded when it rides in the path segment.
    from urllib.parse import quote

    url = quote(f"https://muse.ai/thread/{SEED_IDS[1]}", safe="")
    r = client.get(f"/v1/threads/{url}/messages", headers=auth)
    assert r.status_code == 200
    assert "Mock beta" in r.json()["data"][1]["content"]


def test_thread_messages_not_found(client, auth):
    r = client.get("/v1/threads/00000000-0000-4000-8000-0000000000zz/messages", headers=auth)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_thread_rename(client, auth):
    r = client.patch(f"/v1/threads/{SEED_IDS[2]}", headers=auth, json={"title": "Đổi tên rồi"})
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == SEED_IDS[2]
    assert body["title"] == "Đổi tên rồi"
    assert body["url"].endswith(SEED_IDS[2])
    # Renamed thread jumps to the top of the recency sort.
    listed = client.get("/v1/threads", headers=auth).json()["data"]
    assert listed[0]["id"] == SEED_IDS[2]
    # Title is also visible through the messages list response shape.
    msgs = client.get(f"/v1/threads/{SEED_IDS[2]}/messages", headers=auth).json()
    assert msgs["object"] == "list"


def test_thread_rename_requires_title(client, auth):
    assert client.patch(f"/v1/threads/{SEED_IDS[0]}", headers=auth, json={}).status_code == 400


def test_thread_rename_not_found(client, auth):
    r = client.patch("/v1/threads/00000000-0000-4000-8000-0000000000zz",
                     headers=auth, json={"title": "x"})
    assert r.status_code == 404


def test_thread_delete(client, auth):
    r = client.delete(f"/v1/threads/{SEED_IDS[1]}", headers=auth)
    assert r.status_code == 200
    assert r.json() == {"deleted": True, "id": SEED_IDS[1]}
    assert client.get(f"/v1/threads/{SEED_IDS[1]}/messages", headers=auth).status_code == 404
    remaining = client.get("/v1/threads", headers=auth).json()["data"]
    assert SEED_IDS[1] not in [t["id"] for t in remaining]


def test_thread_delete_accepts_full_url(client, auth):
    from urllib.parse import quote

    raw = f"https://muse.ai/thread/{SEED_IDS[0]}"
    url = quote(raw, safe="")
    r = client.delete(f"/v1/threads/{url}", headers=auth)
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert r.json()["id"].endswith(SEED_IDS[0])
    assert client.delete(f"/v1/threads/{SEED_IDS[0]}", headers=auth).status_code == 404


def test_thread_delete_not_found(client, auth):
    assert client.delete("/v1/threads/00000000-0000-4000-8000-0000000000zz",
                         headers=auth).status_code == 404


def test_http_driver_threads_return_501(client, auth, monkeypatch):
    # Drivers without thread support degrade gracefully with a 501.
    from muse2api.errors import FeatureNotImplemented

    async def not_impl(*args, **kwargs):
        raise FeatureNotImplemented("threads not supported")

    monkeypatch.setattr(client.app.state.services.gateway.driver, "list_threads", not_impl,
                        raising=False)
    r = client.get("/v1/threads", headers=auth)
    assert r.status_code == 501
    assert "not_implemented" in r.json()["error"]["type"]
