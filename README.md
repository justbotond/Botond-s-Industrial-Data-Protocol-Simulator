# Botond's Industrial Data Protocol Simulator

Simulates industrial data sources for testing historians, SCADA, IIoT gateways and connectors:

| Protocol | Role | Default port |
|---|---|---|
| Modbus TCP | server (slave) | 5020 |
| OPC UA | server | 4840 |
| MQTT | built-in broker + data publisher | 1883 |

Everything is driven by [config.yaml](config.yaml). Each protocol section is a **list**: add entries to run more instances. Each instance gets its own port and **its own OS process**, so instances use separate CPU cores and don't slow each other down. Each instance has one or more **tag groups**, which set the tag count, data type, value generator and update rate. One example: 40,000 random doubles updated every 500 ms on OPC UA.

## Quick start

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m simulator
```

Options:

```
python -m simulator -c my_config.yaml        # another config file
python -m simulator --export-tags tagmaps    # also write one CSV tag map per instance
python -m simulator -v                       # debug logging
```

The tag map CSVs list every tag with its address: Modbus area/address/Modicon number, OPC UA NodeId, or MQTT topic. They are handy for bulk-creating points in PI, Ignition and similar tools.

## Tag groups

```yaml
- {name: Random, count: 40000, data_type: float64, generator: random, min: 0, max: 100}
```

| Field | Values |
|---|---|
| `name` | tag name prefix; tags are numbered `Random00000`, `Random00001`, ... |
| `count` | number of tags |
| `data_type` | `float32` `float64` `int16` `uint16` `int32` `uint32` `bool` |
| `generator` | `random` `random_walk` `sine` `ramp` `square` `counter` `constant` |
| `min`, `max` | value range |
| `period_s` | period for `sine` / `ramp` / `square` (each tag gets a random phase) |
| `step` | step for `random_walk` / `counter` |

## Modbus TCP

- Supports function codes 1, 2, 3, 4, 5, 6, 15 and 16. Answers any unit id.
- Each group goes into an `area`: `holding_registers`, `input_registers`, `coils` or `discrete_inputs`. It has an optional 0-based `start_address`; without one, groups are packed back to back.
- Register width: 16-bit types use 1 register, 32-bit types use 2, `float64` uses 4. `word_order: big` is ABCD (high word first) and `little` is CDAB.
- Each area holds at most 65,536 addresses, so at most 32,768 float32 tags per area per instance. For more, add instances on more ports.
- Clients can write. The next update cycle overwrites simulated addresses.

## OPC UA

- Endpoint: `opc.tcp://<host>:<port>/<name>`, security `None`, anonymous access.
- NodeIds are `ns=<idx>;s=<group>.<tag>`, for example `ns=2;s=Random.Random00042`, in namespace `urn:botond:simulator`. Browse path: `Objects/<instance>/<group>/<group>_0-999/...`. Groups larger than `folder_size` are split into sub-folders.
- `writable: true` lets clients write values.
- Measured on this machine: 40,000 nodes are created in about 10–17 s at startup and fit a 500 ms update cycle. With many active client subscriptions the cycle can slip briefly; the server logs a warning when that happens.

## MQTT

- `mqtt_broker` runs a small built-in MQTT 3.1.1 broker. It supports QoS 0/1, retain and `+`/`#` wildcards, with no authentication. Disable it and point `broker_host`/`broker_port` at Mosquitto, EMQX or another broker if you need one.
- `mqtt` publishers send each tag group under `<topic_prefix>/<group>/...`:
  - `mode: per_tag`: one topic per tag, payload `{"value": 42.1, "timestamp": "..."}` (or the bare value with `payload_format: plain`).
  - `mode: batch`: `<prefix>/<group>/batch/<n>`, payload `{"timestamp": "...", "values": {"Temp000": 42.1, ...}}` with `batch_size` tags per message. Use this for very large tag counts.

Try it with any client, e.g. `mosquitto_sub -h 127.0.0.1 -t "sim/#" -v`.

## Layout

```
simulator/
  runner.py          config parsing, one process per instance
  tags.py            tag groups and value generators
  modbus_server.py   Modbus TCP server
  opcua_server.py    OPC UA server (asyncua) with fast bulk node creation
  mqtt_broker.py     built-in MQTT broker
  mqtt_client.py     lightweight publish-only MQTT client
  mqtt_publisher.py  MQTT data simulator
```
