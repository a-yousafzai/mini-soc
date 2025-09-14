import os
import time
from typing import Any, Dict

import orjson
import numpy as np
from sklearn.linear_model import LogisticRegression
from elasticsearch import Elasticsearch

from confluent_kafka import Consumer, KafkaException


def get_env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def make_consumer() -> Consumer:
    conf = {
        "bootstrap.servers": get_env("KAFKA_BOOTSTRAP", "kafka:29092"),
        "group.id": get_env("KAFKA_GROUP_ID", "ml-service"),
        "auto.offset.reset": "earliest",
        "enable.auto.commit": True,
    }
    return Consumer(conf)


def make_es() -> Elasticsearch:
    return Elasticsearch(get_env("ELASTICSEARCH_URL", "http://elasticsearch:9200"))


def load_model() -> LogisticRegression:
    # Demo: a trivial trained model on fake data to keep the example self-contained.
    X = np.array([[0], [1]])
    y = np.array([0, 1])
    model = LogisticRegression().fit(X, y)
    return model


def featurize(event: Dict[str, Any]) -> np.ndarray:
    # Very simple heuristic feature: length of event name and message if present
    name = str(event.get("name", ""))
    message = str(event.get("message", ""))
    length_score = len(name) + len(message)
    return np.array([[1 if length_score > 10 else 0]])


def main() -> None:
    topic = get_env("KAFKA_TOPIC", "osquery_logs")
    out_index = get_env("OUTPUT_INDEX", "osquery-alerts")

    consumer = make_consumer()
    es = make_es()
    model = load_model()

    consumer.subscribe([topic])

    print(f"[ml-service] consuming from topic={topic}, indexing to {out_index}")

    try:
        while True:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                raise KafkaException(msg.error())

            try:
                event = orjson.loads(msg.value())
            except Exception:
                # Skip non-JSON payloads
                continue

            # Infer simple anomaly score with the trivial model
            X = featurize(event)
            score = float(model.predict_proba(X)[0][1])

            doc = {
                "@timestamp": event.get("@timestamp", None),
                "source": "ml-service",
                "source_event_json": orjson.dumps(event).decode("utf-8"),
                "anomaly_score": score,
                "rule": "length-heuristic",
            }

            try:
                es.index(index=out_index, document=doc)
            except Exception as e:
                print(f"[ml-service] index error: {e}")
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        consumer.close()


if __name__ == "__main__":
    main()


