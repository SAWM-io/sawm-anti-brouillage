# SAWM Anti-Brouillage

**Application resilience during outages — retain messages, resume delivery and verify each effect in an HTTP demonstrator.**

[Français](README.md) · [Quick start](#run-locally) · [Technical guide — French](docs/GUIDE-TECHNIQUE.md) · [Measured evidence](docs/measured-2026-10-27/) · [Proposed radio validation](docs/RADIO-VALIDATION.md#english-summary)

SAWM Anti-Brouillage is an executable demonstration of resilient HTTP delivery: a SQLite queue retains messages, a worker resumes attempts after restarting, and an idempotent recipient prevents a message from producing a second effect. It gives product and integration teams a concrete way to evaluate continuity before adapting the mechanism to their own service.

The Cloud workflow adds experiment memory through **Basira**, verified recall through **TSST**, advisory ranking through **ODVL**, and a bounded policy proposal from **SAWM**. During the experiment, optional **continuous local calibration** adjusts the HTTP timeout and retry delay using only completed attempts. Delivery remains local and independent; SAWM prepares the initial policy before execution.

## What you can evaluate

| Need | Demonstrated mechanism |
| --- | --- |
| Retain messages while the service is unavailable | Persistent SQLite queue and bounded retries |
| Resume after the sender process stops | Recovery from the same queue while preserving the attempt budget |
| Handle a missing response without repeating the effect | Stable identifier, content digest, recipient transaction and verified ACK |
| Adjust retries to observed HTTP behavior | Local calibration after every completed attempt, with persisted state and limits |
| Explain the outcome | Recipient ledger, attempt traces, JSON report and explicit checks |
| Retrieve calibration and results | Basira deposits, literal TSST recall and SAWM analysis before/after the run |

## A measured result with a defined scope

The [initial 27 October 2026 campaign](docs/measured-2026-10-27/benchmark.json) compares the same 20 messages across two channels on one local HTTP server. It injects a **2.009 s** service interruption, stops and restarts the worker, and deliberately drops an ACK after commit on each channel.

| Observation at the end of the run | Durable queue | Volatile sender, one attempt |
| --- | ---: | ---: |
| Unique effects recorded | **20 / 20** | **5 / 20** |
| ACKs received and verified | 20 | 4 |
| Messages without an effect | 0 | 15 |
| Duplicate requests received | 1 | 0 |
| Duplicated application effects | **0** | **0** |

All 14 report checks passed. The repeated request did not create a second effect. A missing ACK does not necessarily mean a missing effect: the deliberately dropped response tests this distinction.

**The local calibration without Cloud also achieved 20/20 with exactly the same policy.** These measurements show the benefit of the queue, retries and idempotent recipient under the lab's faults; they do not demonstrate a delivery improvement caused by Cloud. Both runs and their limits are [documented separately](docs/GUIDE-TECHNIQUE.md#preuves-conservées).

## Run locally

Requirements: **Python 3.10+** and an environment that allows a listener on `127.0.0.1` and child processes. The local lab uses only the Python standard library.

```sh
git clone https://github.com/SAWM-io/sawm-anti-brouillage.git
cd sawm-anti-brouillage
python3 -m venv .venv
. .venv/bin/activate
python lab.py --output-dir output/demo --messages 20 --policy examples/policy.json
```

Appropriate GitHub access is required. Use a new output directory for every run. The scenario normally takes a few seconds, within a 30-second overall budget. It starts its own HTTP server, stops it for two seconds, restarts the worker, then restores the server.

Read `output/demo/report.json` for results and `output/demo/measurements.json` for structured measurements. SQLite databases and logs remain available for inspection. Omitting `--messages` uses 10 messages; the supported range is 3–20 per channel.

```sh
# Local tests, including Cloud validators; no real API calls.
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

## Optional continuous calibration

Adaptive mode updates its parameters after each completed attempt. A verified ACK updates the latency estimate and its variation; a timeout increases the wait limit without being treated as a complete RTT sample. Timeouts and refusals space retries further apart; a connection closed without an ACK retains a bounded wait without assuming congestion. Successes bring retries closer. State and changes are persisted in SQLite and checked when the worker resumes.

Initial policy ceilings, maximum attempts and the overall budget remain fixed. Local calibration has no knowledge of future injected faults and does not estimate radio conditions. Static mode remains the default behavior of `lab.py`.

Compare both modes with the same synthetic workloads:

```sh
python adaptive_lab.py --output-dir output/adaptive-demo --repetitions 3 --messages 20
```

The [comparison method and limits — French](docs/GUIDE-TECHNIQUE.md#calibration-continue-locale) distinguish controller observations from policy assumptions prepared before the run. Better performance is not assumed: the report also retains regressions and attempt costs.

### Final comparison results

The [final 27 October campaign](docs/measured-adaptive-2026-10-27/) includes 24 runs: four profiles, two modes and three repetitions. **Every run delivers and acknowledges 20/20 messages with no duplicated effects.** The values below are means of the ACK-delay p95 values calculated separately for each of the three runs per mode.

| HTTP profile | Fixed: mean ACK p95 | Adaptive: mean ACK p95 | Attempts per run, fixed / adaptive |
| --- | ---: | ---: | ---: |
| Normal service | 34.90 ms | 34.12 ms | 20 / 20 |
| Latency increase and recovery | 588.35 ms | 609.36 ms | 20 / 22 |
| ACK lost after commit | 149.49 ms | 153.89 ms | 28 / 28 |
| Outage and worker restart | 974.77 ms | 1113.07 ms | 22 / 25 |

**Calibration works, but this campaign does not show a general performance improvement.** Adaptive mode uses 285 attempts versus 270 for fixed mode, with 240 messages per mode; fixed mode has a lower mean ACK p95 on three of the four measured profiles. Three repetitions do not establish general superiority. Static mode remains the default, while adaptive mode lets teams examine these tradeoffs with complete traces.

The [individual measurements](docs/measured-adaptive-2026-10-27/aggregate.json), [delay chart](docs/measured-adaptive-2026-10-27/ack-p95.svg) and [calibration examples](docs/measured-adaptive-2026-10-27/figures/calibration-examples.svg) are retained. The first version and its lost-ACK regression remain [documented in the guide — French](docs/GUIDE-TECHNIQUE.md#correction-documentée-après-la-première-comparaison).

## Architecture and Cloud responsibilities

```mermaid
flowchart TB
    subgraph Before[Before the lab — optional Cloud]
        C[Calibration summary] --> B[Basira: deposit and completed job]
        B --> T[TSST: verified literal recall]
        T --> S[SAWM: proposed policy]
        S --> V[Local JSON and bounds validation]
        O[ODVL: advisory option ranking] --> R[Preparation report]
    end
    subgraph Local[Local run — HTTP on 127.0.0.1]
        M[Synthetic messages] --> Q[SQLite queue]
        Q --> W[Worker and bounded retries]
        W --> H[HTTP witness and idempotent ledger]
        H --> A[Correlated and verified ACK]
        A --> Q
        W --> F[Completed attempt observations]
        F --> L[Optional local calibration]
        L --> W
        M --> E[Volatile sender]
        E --> H
        H --> J[Measurement report]
    end
    V --> Q
    subgraph After[After the lab — optional Cloud]
        J --> U[Results summary]
        U --> BT[Basira and TSST recall]
        BT --> SR[SAWM: results review]
    end
```

- **Basira** stores the small summaries supplied by the lab; receipts and job completion are checked.
- **TSST** retrieves those summaries. The validator requires the expected canonical text within evidence marked as integrity verified, in the correct space and entity.
- **ODVL** ranks three delivery options before the run. The order is validated and retained for inspection; the code does not apply it to the worker or pass it into SAWM's policy prompt.
- **SAWM** proposes an initial policy JSON object before the run and later reviews the measurements. Only the specified fields and bounds are accepted; no generated command is executed.

None of these calls is part of the delivery loop. The archived campaign includes real Cloud calls; the local `cloud_access: false` field describes the `lab.py` execution alone.

## Use Basira and SAWM

The [complete Cloud walkthrough](docs/GUIDE-TECHNIQUE.md#parcours-cloud) includes calibration, preparation, execution and result-recording commands. It requires `httpx` from `requirements.txt`, a Basira space, an explicitly authorized entity and an existing key with **`events:write`, `jobs:read`, `tsst:recall`, `odvl:rank`, `sawm:run`** for the complete workflow. Effective access grants on the entity are also required.

The key is read from a private file outside the repository with `600` permissions. The accepted API endpoint is `https://basira-api.sawm.io/api/v1/basira`. Cloud commands require `--run` and may incur charges. Send only the small synthetic measurements described in the guide.

## Scope and next steps

This is a demonstration lab running on one host. It tests a process restart with SQLite state retained during the same host boot. It does not validate machine reboot recovery, power failure, storage corruption or third-party API idempotency. Delays include the wait experienced by retained messages; they do not establish faster networking or higher bandwidth. The Mac and VMs support software validation; no radio equipment, vehicle or physical network is tested. The [future radio protocol](docs/RADIO-VALIDATION.md#english-summary) describes an isolated two-radio link, controlled attenuation and the required measurements, with no jammer deployed.

Explore the [technical guide — French](docs/GUIDE-TECHNIQUE.md), [validation checklist — French](docs/validation.md) and [27 October evidence](docs/measured-2026-10-27/). For HTTP message scheduling and deadlines, see the complementary [SAWM Smart Réseau](https://github.com/SAWM-io/sawm-smart-reseau) program.
