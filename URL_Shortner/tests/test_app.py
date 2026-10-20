import time

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def make(client, **body):
    body.setdefault("url", "https://example.com")
    return client.post("/api/links", json=body)


def test_create_random_code(client):
    r = make(client)
    assert r.status_code == 201
    assert len(r.json()["code"]) == 7


def test_custom_alias_and_duplicate(client):
    assert make(client, alias="aliasone").status_code == 201
    assert make(client, alias="aliasone").status_code == 409


def test_invalid_and_reserved_alias(client):
    assert make(client, alias="bad-alias").status_code == 400
    assert make(client, alias="docs").status_code == 400


def test_invalid_url(client):
    r = client.post("/api/links", json={"url": "not a url"})
    assert r.status_code == 422


def test_redirect(client):
    code = make(client).json()["code"]
    r = client.get(f"/{code}", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "https://example.com/"


def test_unknown_code(client):
    assert client.get("/doesnotexist", follow_redirects=False).status_code == 404


def test_expired_link(client):
    code = make(client, expires_at="2020-01-01T00:00:00").json()["code"]
    assert client.get(f"/{code}", follow_redirects=False).status_code == 410


def test_click_tracking(client):
    code = make(client).json()["code"]
    for _ in range(3):
        client.get(f"/{code}", follow_redirects=False, headers={"user-agent": CHROME_UA})

    # clicks queue se batch mein likhe jate hain (~1s), isliye thoda wait
    stats = {}
    for _ in range(40):
        stats = client.get(f"/api/links/{code}/stats").json()
        if stats["total_clicks"] == 3:
            break
        time.sleep(0.1)

    assert stats["total_clicks"] == 3
    assert stats["unique_visitors"] == 1
    assert "Chrome" in stats["browsers"]


def test_stats_unknown_code(client):
    assert client.get("/api/links/nope/stats").status_code == 404


def test_rate_limit(client):
    codes = [make(client).status_code for _ in range(11)]
    assert codes[:10] == [201] * 10
    assert codes[10] == 429