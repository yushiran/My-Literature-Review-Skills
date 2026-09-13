import urllib.error
import pytest
import search as S

FAKE = "fake-key-not-a-real-credential"
URL = f"https://api.openalex.org/works?filter=title.search:x&api_key={FAKE}&mailto=a@b.c&per-page=200"


def test_safe_url_redacts_the_key_and_keeps_the_rest_of_the_query():
    out = S.safe_url(URL)
    assert FAKE not in out
    assert "api_key=<redacted>" in out
    assert "filter=title.search:x" in out
    assert "mailto=a@b.c" in out
    assert "per-page=200" in out


def test_safe_url_leaves_a_url_without_a_key_unchanged():
    plain = "https://api.openalex.org/works?filter=x&mailto=a@b.c"
    assert S.safe_url(plain) == plain


@pytest.mark.parametrize("code,exc", [(429, S.RateLimited), (400, S.SourceDown)])
def test_the_key_never_reaches_the_exception_message(code, exc, monkeypatch):
    def boom(url, headers=None):
        raise urllib.error.HTTPError(url, code, "nope", {}, None)

    monkeypatch.setattr(S, "http_get", boom)
    monkeypatch.setattr(S, "BACKOFF", ())      # one attempt, so the retry sleeps do not run
    with pytest.raises(exc) as caught:
        S.get_json_retry(URL, name="openalex")
    assert FAKE not in str(caught.value)
