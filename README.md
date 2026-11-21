# llm-serving-lab

Measuring what it actually costs to serve a language model, one controlled
experiment at a time. Every number here was produced on the hardware described
below, with the benchmark script in this repository, and every raw run is kept
in `results/`.

**Hardware** — shared CI server, 8 cores, 15 GB RAM, RHEL. The inference
container is confined to 4 cores via `cpuset` and a 3 GB hard memory limit, so
the measurements are reproducible and the host's other workloads are unaffected.

**Reference model** — `llama3.2:1b`, quantized, CPU only. Model size is
irrelevant to what is being measured: queueing behaviour, saturation points and
the shape of the latency curve are the same at every scale. Only the absolute
values move.

---

## Headline findings

**1. Thread oversubscription cost a factor of 19.** Inside a container,
`/proc/cpuinfo` still reports all of the host's cores. `llama-server` sizes its
thread pool from that number and ignores the `cpuset`, so 8 threads were
fighting over 2 allowed cores. Aligning the thread count to the cpuset took a
single request from 55.1 s to 2.9 s.

**2. Without continuous batching, throughput is flat.** With one slot,
aggregate throughput stayed at 22 tok/s whether 1 or 4 requests were in flight —
the extra requests simply queued. With four slots, throughput scaled to 54 tok/s
on the same four cores.

**3. The optimal concurrency equals the number of slots.** Beyond it, latency
degrades sharply for no throughput gain at all.

---

## Step 1 — CPU baseline

### 1.1 Thread count vs. cpuset

Same model, same cores allowed, only the thread count changed.

| Configuration | Model load | One request | Throughput | Host load avg |
|---|---:|---:|---:|---:|
| 2 cores, 8 threads (default) | 28.2 s | 55.1 s | ~1 tok/s | 6.19 |
| 4 cores, 4 threads (aligned) | 12.0 s | 2.9 s | 22.5 tok/s | ~2.5 |

Evidence for the slow case, straight from the server log:

```
[GIN] 2026/11/21 - 12:21:33 | 200 | 55.06s | POST "/v1/chat/completions"
```

The request succeeded. It was not an error condition — the engine was simply
spending its time on context switches rather than arithmetic.

**A side lesson on monitoring.** The host load average reached 6.19 while the
container was confined to 2 cores. Load average counts *runnable* threads, not
CPU consumed: eight threads queued on two cores inflate it well beyond actual
utilisation. For a cgroup-confined workload, host load average is a misleading
signal — the `cpuset` was the real guarantee, and it held.

The fix is the `Modelfile` in this repository. Note that the documented
`OLLAMA_NUM_THREADS` environment variable has no effect; the `num_thread`
model parameter does.

### 1.2 Continuous batching, or its absence

Both series below use 4 cores and 4 aligned threads. Only the number of
serving slots differs.

**One slot** — `OLLAMA_NUM_PARALLEL=1`:

| Concurrency | TTFT p50 | TTFT p95 | Total p50 | Throughput |
|---:|---:|---:|---:|---:|
| 1 | 60 ms | 65 ms | 2839 ms | 22.5 tok/s |
| 2 | 2854 ms | 2964 ms | 5562 ms | 22.7 tok/s |
| 4 | 8501 ms | 8901 ms | 11291 ms | 22.3 tok/s |

Throughput is flat to within 2%. The time to first token is simply the queue
position multiplied by a full generation: 1 × 2.84 s = 2840 ms predicted against
2854 ms measured at concurrency 2, and 3 × 2.84 s = 8520 ms against 8501 ms at
concurrency 4. Both within 1%. This is perfect serialisation, not gradual
degradation.

**Four slots** — `OLLAMA_NUM_PARALLEL=4`:

| Concurrency | TTFT p50 | TTFT p95 | Total p50 | Total p95 | Throughput |
|---:|---:|---:|---:|---:|---:|
| 1 | 58 ms | 64 ms | 2846 ms | 2875 ms | 22.5 tok/s |
| 2 | 87 ms | 118 ms | 3575 ms | 3602 ms | 36.1 tok/s |
| 4 | 132 ms | 677 ms | 4738 ms | 5049 ms | 53.7 tok/s |
| 8 | 4820 ms | 4997 ms | 9403 ms | 9585 ms | 54.1 tok/s |
| 16 | 14621 ms | 19145 ms | 19270 ms | 25006 ms | 48.6 tok/s |

Raw data: `results/2026-11-21-parallel4.csv`

### 1.3 Reading the saturation curve

Throughput plateaus at concurrency 4 and *declines* at 16 — 48.6 tok/s, 10%
below the peak. Past the plateau there is nothing left to win, and queue
management starts costing real throughput.

Latency breaks at the same point. The queueing arithmetic holds again: at
concurrency 8 the median TTFT of 4820 ms matches one complete generation
(4738 ms at concurrency 4), because half the requests wait for a slot. At
concurrency 16 it is three such waves.

**Operating point: concurrency 4, equal to the slot count.** Going from 4 to 8
buys 0.7% throughput and costs a factor of 7.4 on TTFT p95.

The practical consequence for a production deployment is an admission-control
rule rather than a tuning knob: cap in-flight requests at the slot count and
reject or shed beyond it. Letting the queue grow inside the engine converts a
capacity problem into a latency problem and hides it from the caller.

### 1.4 Why batching helps on CPU too

The working hypothesis before measuring was that CPU inference is
compute-bound, so batching would add little. The data refuted it: 2.3× on
identical cores.

Generating one token requires reading all 1.3 GB of weights, which do not fit
in L3 cache. That memory sweep is amortised across every sequence in the batch,
and the matrix-vector products become matrix-matrix products, which map far
better onto SIMD units. CPU inference is substantially memory-bandwidth-bound,
not purely compute-bound.

---

## Method notes

Things that would make these numbers wrong if ignored.

**Cold start is not comparable across runs.** Model load measured 28 s, then
12 s, then 3 s. The last two reflect a warm page cache holding the 1.3 GB model
file, not a configuration change. Only the first is a true cold start. Dropping
caches to force the issue was rejected: on a shared host it would evict every
other workload's cache too.

**Percentiles need samples.** The one-slot series uses 2 requests per
concurrency level — a p95 over two samples is not a p95. It is kept here because
the effect size is large and the arithmetic is unambiguous, but it is marked as
provisional and will be re-run at `--repeat 5`.

**Shared host.** All runs are confined by `cpuset`, a hard memory limit, swap
disabled for the container, and lowered CPU and I/O priority. A watchdog
(`guard.sh`) stops the container if host load, available memory or disk usage
cross a threshold, and logs the reason. A benchmark run without this isolation
on a shared machine is doubly wrong: it endangers the neighbouring workloads,
and it measures their noise as much as the model.

---

## Architecture decisions

| Date | Decision | Rationale | Revised |
|---|---|---|---|
| 2026-11-21 | Start on CPU with a 1B model | Build intuition for cost per token before introducing the GPU variable | |
| 2026-11-21 | Disposable cluster, never a shared one | Inference OOMs happen at node level, not pod level | |
| 2026-11-21 | `num_thread` pinned to the cpuset size | Thread pool ignores cgroup limits; oversubscription cost 19× | |
| 2026-11-21 | Reject host load average as the isolation signal | Counts runnable threads, inflated by a confined workload | |

---

## Running it

Requirements: Docker with Compose ≥ 2.24, Python 3.9+. `bench.py` has no
external Python dependencies. For step 2, `kubectl` and `k3d`.

```bash
make check                       # docker, compose, cpuset, memory, disk
make up && make pull
make bench
```

On a shared host, start the watchdog in a second terminal first:

```bash
DOCKER="sudo docker" ./guard.sh --load 6 --min-free 2048
```

Adapt `.env` (model storage path) and the `cpuset` in
`docker-compose.override.yml` to the machine before the first run.

---

## Roadmap

- [x] CPU baseline, thread alignment, saturation curve
- [ ] One-slot series re-run at `--repeat 5`
- [ ] Kubernetes: cold start, probes, persistent model volume
- [ ] KServe: `InferenceService`, canary rollout between model versions
- [ ] vLLM on a rented GPU: same protocol, continuous batching, cost per
      million tokens against the CPU baseline
- [ ] Gateway: per-team quotas, multi-model routing, cost per token
- [ ] Automated evaluation in CI, blocking on quality regression

---

## Files

| File | Purpose |
|---|---|
| `bench.py` | Benchmark harness — TTFT, p50/p95, throughput, knee and saturation detection |
| `Modelfile` | Model variant with `num_thread` aligned to the cpuset |
| `docker-compose.yml` | Inference server, with model sizing guidance in comments |
| `docker-compose.override.yml` | Shared-host guardrails: cpuset, memory cap, no swap, localhost only |
| `guard.sh` | Watchdog — load, memory, disk thresholds; stops the container and logs why |
| `ollama-k8s.yaml`, `kind-cluster.yaml` | Step 2 |
| `results/` | Timestamped CSVs, never overwritten |
