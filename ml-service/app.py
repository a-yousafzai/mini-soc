import os
import time
from typing import Any, Dict

import orjson
import numpy as np
from sklearn.linear_model import LogisticRegression
from elasticsearch import Elasticsearch
from drain3 import TemplateMiner
from drain3.file_persistence import FilePersistence

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
    drain_index = get_env("DRAIN_INDEX", "syslog-templates")
    drain_state_path = get_env("DRAIN_STATE_PATH", "/app/state/drain_state.bin")

    consumer = make_consumer()
    es = make_es()
    model = load_model()

    # Initialize Drain3 for template mining (use default config)
    os.makedirs(os.path.dirname(drain_state_path), exist_ok=True)
    persistence = FilePersistence(drain_state_path)
    drain = TemplateMiner(persistence)

    consumer.subscribe([topic])

    print(f"[ml-service] consuming from topic={topic}, indexing to {out_index}")

    try:
        while True:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                raise KafkaException(msg.error())

            raw = msg.value()
            text_payload = None
            event: Dict[str, Any] = {}
            try:
                event = orjson.loads(raw)
                source = event
                # If this looks like a syslog wrapper from Logstash plain codec
                if isinstance(source.get("message"), str) and source.get("pipeline") == "from_syslog":
                    text_payload = source.get("message")
            except Exception:
                # Non-JSON, treat as plain text (e.g., raw syslog)
                text_payload = raw.decode("utf-8", errors="replace")

            # Fallback: stringify the event for template mining
            if text_payload is None:
                text_payload = orjson.dumps(event).decode("utf-8") if event else ""

            # Infer simple anomaly score with the trivial model
            X = featurize(event)
            score = float(model.predict_proba(X)[0][1])

            # Drain3 template mining
            result = drain.add_log_message(text_payload)
            cluster_id = None
            template = None
            params = []
            if result:
                # Drain3 may return an object or a dict depending on version/config
                if isinstance(result, dict):
                    cluster_id = result.get("cluster_id")
                    cluster = result.get("cluster")
                    if cluster is not None and hasattr(cluster, "get_template"):
                        template = cluster.get_template()
                        try:
                            params = cluster.get_parameter_list(text_payload) or []
                        except Exception:
                            params = []
                    else:
                        template = (
                            result.get("log_template_mined")
                            or result.get("template_mined")
                            or result.get("template")
                        )
                        params = result.get("parameter_list") or []
                else:
                    # Object-like API
                    cluster_id = getattr(result, "cluster_id", None)
                    if hasattr(result, "get_template"):
                        template = result.get_template()
                    try:
                        params = result.get_parameter_list(text_payload) or []
                    except Exception:
                        params = []

            # Index alert with template metadata
            doc = {
                "@timestamp": event.get("@timestamp", None),
                "source": "ml-service",
                "source_event_json": orjson.dumps(event).decode("utf-8") if event else None,
                "raw_text": text_payload,
                "anomaly_score": score,
                "rule": "length-heuristic",
                "template_id": cluster_id,
                "template": template,
                "template_params": params,
            }

            try:
                es.index(index=out_index, document=doc)
            except Exception as e:
                print(f"[ml-service] index error: {e}")
                time.sleep(0.5)

            # Store/update template catalog
            if cluster_id and template:
                try:
                    es.index(index=drain_index, id=str(cluster_id), document={
                        "template_id": cluster_id,
                        "template": template,
                        "last_seen": event.get("@timestamp", None),
                    })
                except Exception as e:
                    print(f"[ml-service] template index error: {e}")
    except KeyboardInterrupt:
        pass
    finally:
        consumer.close()


if __name__ == "__main__":
    main()


