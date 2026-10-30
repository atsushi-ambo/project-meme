"""動画なしでの記録とガイドからの導線をHTTPで確認する。"""
from test_reviews import review_client  # noqa: F401


def test_guide_and_recording_entry_points(review_client):
    response = review_client.get('/guide/observe')
    assert response.status_code == 200
    assert '何もしなかった' in response.text and 'その後は不明' in response.text
    assert '後ろ姿' in response.text and '思い出した' in response.text
    assert 'href="/entries/observation/new"' in response.text
    assert 'href="/entries/health/new?event_type=food"' in response.text
    for url in ['/', '/entries/observation/new']:
        page = review_client.get(url)
        assert page.status_code == 200 and 'href="/guide/observe"' in page.text
