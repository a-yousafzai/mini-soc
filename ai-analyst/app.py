import os
import time
from typing import Any, Dict

import orjson
import httpx
from confluent_kafka import Consumer, KafkaException
from elasticsearch import Elasticsearch


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def make_consumer() -> Consumer:
    conf = {
        "bootstrap.servers": env("KAFKA_BOOTSTRAP", "kafka:29092"),
        "group.id": env("KAFKA_GROUP_ID", "ai-analyst"),
        "auto.offset.reset": "earliest",
        "enable.auto.commit": True,
    }
    return Consumer(conf)


def make_es() -> Elasticsearch:
    return Elasticsearch(env("ELASTICSEARCH_URL", "http://elasticsearch:9200"))


async def call_llm(prompt: str) -> str:
    # Simple OpenAI-compatible call; falls back to a heuristic if no key
    api_key = env("OPENAI_API_KEY")
    base_url = env("OPENAI_BASE_URL", "https://api.openai.com/v1")
    model = env("OPENAI_MODEL", "gpt-4o-mini")
    if not api_key:
        # No key: return trimmed prompt tail as pseudo-summary
        return (prompt[-300:]).strip()
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a SOC analyst. Write concise, actionable summaries."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 200,
    }
    async with httpx.AsyncClient(timeout=20.0, base_url=base_url) as client:
        r = await client.post("/chat/completions", headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()


def build_prompt(alert: Dict[str, Any]) -> str:
    title = f"Alert from {alert.get('source','unknown')}"
    original = alert.get("source_event_json") or alert.get("raw_text") or alert.get("event", {}).get("original")
    return (
        f"{title}\n"
        f"Time: {alert.get('@timestamp')}\n"
        f"Anomaly: {alert.get('anomaly_score')}\n"
        f"Template: {alert.get('template')} (id={alert.get('template_id')})\n"
        f"Original:\n{original}\n"
        "Summarize what happened and suggest next investigative steps."
    )


def main() -> None:
    topic = env("KAFKA_TOPIC", "syslog-alerts")
    out_index = env("OUTPUT_INDEX", "alerts-enriched")

    consumer = make_consumer()
    es = make_es()
    consumer.subscribe([topic])
    print(f"[ai-analyst] consuming from topic={topic}, indexing to {out_index}")

    try:
        while True:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                raise KafkaException(msg.error())

            try:
                alert = orjson.loads(msg.value())
            except Exception:
                continue

            prompt = build_prompt(alert)
            try:
                # Run LLM in a lightweight sync wrapper
                import asyncio

                summary = asyncio.run(call_llm(prompt))
            except Exception as e:
                summary = f"LLM unavailable. Heuristic summary: {prompt[-200:]} ({e})"

            doc = {
                "@timestamp": alert.get("@timestamp"),
                "source": alert.get("source"),
                "summary": summary,
                "original_alert": alert,
            }
            try:
                es.index(index=out_index, document=doc)
            except Exception as e:
                print(f"[ai-analyst] index error: {e}")
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        consumer.close()


if __name__ == "__main__":
    main()


