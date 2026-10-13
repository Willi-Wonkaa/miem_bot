import httpx
import pytest

from app.rag.smoke import check


def client_for(ready=False, complete=False, reject=True):
    def handle(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "index_ready": ready})
        if request.url.path == "/answer":
            assert request.headers["Authorization"] == "Bearer invalid-smoke-token"
            return httpx.Response(401 if reject else 200)
        assert request.headers["Authorization"] == "Bearer test-internal-token"
        return httpx.Response(
            200,
            json={
                "coverage": {"indexed_files": int(ready), "total_files": 3, "complete": complete}
            },
        )

    return httpx.Client(
        base_url="http://rag",
        transport=httpx.MockTransport(handle),
        headers={"Authorization": "Bearer test-internal-token"},
    )


def test_warming_api_is_healthy_but_not_searchable():
    with client_for() as client:
        assert check(client)["indexed_files"] == 0
        with pytest.raises(ValueError, match="first searchable"):
            check(client, require_index=True)


def test_partial_index_is_searchable_but_not_complete():
    with client_for(ready=True) as client:
        assert check(client, require_index=True)["indexed_files"] == 1
        with pytest.raises(ValueError, match="incomplete"):
            check(client, require_complete=True)


def test_complete_index_passes():
    with client_for(ready=True, complete=True) as client:
        assert check(client, require_complete=True)["complete"]


def test_api_that_accepts_invalid_credentials_fails():
    with client_for(reject=False) as client:
        with pytest.raises(ValueError, match="invalid credentials"):
            check(client)
