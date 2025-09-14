## mini-soc

A minimal, self-contained Security Operations Center (SOC) stack you can run locally. It ingests endpoint data from osquery and surfaces it in Elasticsearch/Kibana, with Kafka in the pipeline for durability and decoupling.

### Overview

Services (all containerized):
- Zookeeper + Kafka: message bus (`osquery_logs`, `syslog_logs`)
- Elasticsearch: storage and search
- Kibana: UI for exploring data
- Logstash: ingestion pipeline (file/syslog → Kafka → Elasticsearch)
- ml-service (syslog): Kafka consumer with Drain3 template mining → `syslog-alerts`, templates in `syslog-templates`
- ml-service-osquery: Kafka consumer (anomaly scoring demo) → `osquery-alerts`, templates in `osquery-templates`

Default flows in this repo:
- osquery → filesystem logs → Logstash file input → Kafka (`osquery_logs`) → Logstash Kafka input → Elasticsearch (`osquery-*`) → Kibana
- syslog (UDP/TCP 5514) → Logstash syslog (UDP)/tcp inputs → Kafka (`syslog_logs`) → Logstash Kafka input → Elasticsearch (`syslog-*`) → Kibana

ML flows:
- Kafka `osquery_logs` → ml-service-osquery → `osquery-alerts`
- Kafka `syslog_logs` → ml-service (Drain3) → `syslog-alerts`; templates catalog in `syslog-templates`

Why this default? The macOS osquery build commonly installed locally does not include the Kafka logger plugin. This setup still gives you a Kafka hop without rebuilding osquery.

### Prerequisites
- Docker Desktop (or Docker Engine) with Compose v2 (`docker compose`)
- curl (for quick health checks)
- osqueryd installed locally (macOS path used below: `/opt/osquery/lib/osquery.app/Contents/MacOS/osqueryd`)

### Quick start

1) Start the stack
```bash
docker compose up -d
```

2) Check Elasticsearch health (should be green)
```bash
curl -s 'http://localhost:9200/_cluster/health?pretty'
```

3) Open Kibana
```
http://localhost:5601
```
Create a Data View for `osquery-*` using `@timestamp` as the time field.

Additionally, this repo provides data views for common indices (created via API in local dev):
- `osquery-alerts`, `syslog-alerts`, `syslog-templates`, `osquery-templates`, and `syslog-*`/`osquery-*`.

### Repo layout

```text
mini-soc/
  docker-compose.yml
  logstash/
    pipeline/
      logstash.conf
  osquery/
    osquery.conf
    osquery.flags
    logs/                # created at runtime (mounted into Logstash)
```

### osquery configuration (filesystem logging → Kafka)

This repo includes a minimal osquery schedule that snapshots `system_info` and `osquery_info` frequently for easy testing.

Run osqueryd ephemerally to emit logs to `osquery/logs/`:
```bash
/opt/osquery/lib/osquery.app/Contents/MacOS/osqueryd \
  --flagfile "$(pwd)/osquery/osquery.flags" \
  --ephemeral --disable_watchdog --verbose=false
```
Logs written:
- `osquery/logs/osqueryd.snapshots.log` (JSON lines; tailed by Logstash)

Logstash tags file-originated events and publishes them to Kafka (`osquery_logs`). A second Logstash input consumes from the same Kafka topic and indexes to Elasticsearch.

### Verifying the pipeline

- Check indices
```bash
curl -s 'http://localhost:9200/_cat/indices/osquery-*?v'
```

- Fetch a sample document
```bash
curl -s 'http://localhost:9200/osquery-*/_search?q=name:osquery_info&size=2&pretty'
```

- Read messages from Kafka
```bash
docker exec -it kafka bash -lc \
  "kafka-console-consumer --bootstrap-server localhost:9092 --topic osquery_logs --from-beginning --max-messages 5"
```

You should see JSON lines similar to the osquery snapshot events.

#### Syslog quick test

- Send a UDP syslog line from host (macOS/Linux):
```bash
printf "<134>Sep 14 12:00:00 mini-soc host app[123]: syslog test\n" | nc -u -w1 127.0.0.1 5514
```

- Verify Kafka and Elasticsearch:
```bash
docker exec -it kafka bash -lc "kafka-console-consumer --bootstrap-server localhost:9092 --topic syslog_logs --from-beginning --max-messages 1"
curl -s 'http://localhost:9200/_cat/indices/syslog-*?v'
```

#### ML alerts and templates

- Syslog alerts: `curl -s 'http://localhost:9200/syslog-alerts/_search?size=1&pretty'`
- Osquery alerts: `curl -s 'http://localhost:9200/osquery-alerts/_search?size=1&pretty'`
- Syslog templates (Drain3): `curl -s 'http://localhost:9200/syslog-templates/_search?size=1&pretty'`
- Osquery templates: `curl -s 'http://localhost:9200/osquery-templates/_search?size=1&pretty'`

### Switching to native osquery → Kafka (optional)

If your osquery build includes the Kafka logger plugin, you can switch to send directly to Kafka.

1) Update `osquery/osquery.flags` (replace filesystem logging):
```text
--logger_plugin=kafka
--logger_kafka_brokers=localhost:9092
--logger_kafka_topic=osquery_logs
```
Remove the filesystem logger options:
```text
--logger_plugin=filesystem
--logger_path=...
--logger_rotate=...
```
Keep the user-writable paths (already present):
```text
--extensions_socket=.../osquery/osquery.em
--database_path=.../osquery/osquery.db
--pidfile=.../osquery/osquery.pid
```

2) Simplify Logstash (`logstash/pipeline/logstash.conf`):
- Remove the `file { ... }` input and the `kafka { ... }` output that republishes.
- Keep only `input { kafka { ... } }` → `output { elasticsearch { ... } }`.

3) Restart Logstash and run osquery
```bash
docker compose restart logstash
```
Run `osqueryd` again for a minute to emit data.

4) Verify via Kafka and Elasticsearch using the commands above.

### Troubleshooting

- Compose warning: "attribute `version` is obsolete"
  - Harmless; Compose v2 ignores the top-level `version:` key. You can remove it if desired.

- Elasticsearch 400 mapping errors (e.g., `host` field conflicts)
  - If you previously indexed different-shaped documents, delete the index and let it be recreated:
```bash
curl -XDELETE 'http://localhost:9200/osquery-YYYY.MM.DD'
```

- Logstash not reading files
  - Clear the sincedb and restart:
```bash
docker exec -it logstash bash -lc 'rm -f /usr/share/logstash/data/sincedb-osquery'
docker compose restart logstash
```
  - Ensure the volume mount exists in `docker-compose.yml`:
```yaml
  logstash:
    volumes:
      - ./logstash/pipeline/:/usr/share/logstash/pipeline/
      - ./osquery/logs/:/var/osquery/logs/
```

- Kafka connectivity
  - The broker is reachable inside the Docker network as `kafka:29092` and from your host as `localhost:9092`.

- Syslog input
  - Ensure ports are exposed in `docker-compose.yml` for Logstash:
```yaml
  logstash:
    ports:
      - "5514:5514"
      - "5514:5514/udp"
```
  - If no docs appear, check Logstash logs and verify `syslog_logs` topic has messages.

### Common commands

- Start/stop
```bash
docker compose up -d
docker compose down -v
```

- Logs
```bash
docker compose logs --tail=200 logstash | less
```

- Health
```bash
curl -s 'http://localhost:9200/_cluster/health?pretty'
```

### Notes
- Security in this demo stack is disabled for convenience (e.g., Elasticsearch `xpack.security.enabled=false`). Do not deploy as-is to production.
- osquery schedule intervals are short for demo purposes; tune for real usage.

### License
Choose a license before publishing (e.g., MIT, Apache-2.0). If unsure, MIT is a common default for demos.
