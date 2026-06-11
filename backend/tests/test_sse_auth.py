def test_stream_requires_token_param(client):
    resp = client.get("/api/v1/questions/jobs/job-1/stream")
    assert resp.status_code == 422  # token query param is required


def test_stream_rejects_bad_token(client):
    resp = client.get("/api/v1/questions/jobs/job-1/stream", params={"token": "not-a-jwt"})
    assert resp.status_code == 401
