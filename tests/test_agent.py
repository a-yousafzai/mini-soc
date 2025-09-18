from ai_analyst.app import app, _default_query, translate_to_es_dsl
from fastapi.testclient import TestClient
from typing import Optional, Dict, Any
import os
import json
import time
import urllib.request
import urllib.error

def test_default_query_shape():
    q = _default_query("ssh", "24h")
    assert "bool" in q and "must" in q["bool"]

def test_translate_fallback_no_llm():
    dsl = translate_to_es_dsl("ssh brute force", "24h")
    assert "query" in dsl and "size" in dsl

def test_api_contract():
    client = TestClient(app)
    r = client.post("/analyze", json={"query":"ssh", "time_range":"24h"})
    assert r.status_code == 200
    data = r.json()
    for k in ("dsl","total","insights","samples"):
        assert k in data


def _es_http(url: str, method: str, path: str, body: Optional[Dict[str, Any]] = None):
    data = None
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(url.rstrip("/") + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.getcode(), json.loads(resp.read().decode())


def test_nlp_to_dsl_and_search_integration(monkeypatch):
    es_url = os.environ.get("ELASTICSEARCH_URL", "http://localhost:9200")
    print(f"[test] Using Elasticsearch: {es_url}")
    # Check ES availability; skip if not running
    try:
        code, _ = _es_http(es_url, "GET", "/")
        if code >= 400:
            raise AssertionError
    except Exception:
        # Skip the test gracefully if ES is not up locally
        import pytest
        pytest.skip("Elasticsearch not available on localhost:9200")

    # Seed a fresh doc
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    doc = {
        "@timestamp": now,
        "host": {"name": "srv-int"},
        "process": {"name": "sshd"},
        "message": "ssh brute force from 1.2.3.4",
    }
    print("[test] Seeding doc:", json.dumps(doc))
    _es_http(es_url, "POST", "/alerts-enriched/_doc", doc)
    # Ensure the doc is searchable immediately
    try:
        _es_http(es_url, "POST", "/alerts-enriched/_refresh", {})
    except Exception:
        pass

    # Mock the LLM translator and insights
    from ai_analyst import app as module

    async def fake_call_llm(prompt: str) -> str:
        if "translate a natural-language SOC question" in prompt:
            dsl = {
                "query": {"match_phrase": {"message": "ssh brute force"}},
                "size": 5,
                "sort": [{"@timestamp": "desc"}],
            }
            print("[test] Mocked DSL:", json.dumps(dsl))
            return json.dumps(dsl)
        return "Detected ssh brute force; Next: block offending IP and review auth logs."

    monkeypatch.setattr(module, "call_llm", fake_call_llm)

    # Point app to local ES
    os.environ["ELASTICSEARCH_URL"] = es_url

    client = TestClient(app)
    payload = {"query": "ssh brute force", "time_range": "24h", "index": "alerts-enriched"}
    print("[test] Request payload:", json.dumps(payload))
    r = client.post("/analyze", json=payload)
    print("[test] Response status:", r.status_code)
    try:
        print("[test] Response body:", json.dumps(r.json()))
    except Exception:
        print("[test] Raw response:", r.text)
    assert r.status_code == 200
    data = r.json()
    assert data["total"] >= 1
    assert any("ssh brute force" in (s.get("message") or "") for s in data.get("samples", []))