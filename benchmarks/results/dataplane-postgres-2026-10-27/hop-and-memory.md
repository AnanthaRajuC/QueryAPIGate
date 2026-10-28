# The forwarded hop, and memory per process (2026-10-28, 07:41-07:57)

Each latency the middle of 3 runs of 30 s; memory is VmRSS after the cells. Rows in hop-and-memory-2026-10-28.jsonl.

    rust-data-plane+cache write_writer c=1 q=None: p50 1.43 ms p99 2.28 ms
    rust-data-plane+cache write_writer c=8 q=400: p50 1.77 ms p99 5.04 ms
    rust-data-plane+cache lookup_plain c=1 q=None: p50 1.38 ms p99 3.04 ms
    rust-data-plane+cache lookup_plain c=8 q=400: p50 1.58 ms p99 4.71 ms
    memory rust-data-plane+cache after load: gunicorn processes [48, 52] MB, data plane None MB
    rust-data-plane+dp+cache write_writer c=1 q=None: p50 1.78 ms p99 3.52 ms
    rust-data-plane+dp+cache write_writer c=8 q=400: p50 2.12 ms p99 4.40 ms
    rust-data-plane+dp+cache lookup_plain c=1 q=None: p50 0.18 ms p99 0.64 ms
    rust-data-plane+dp+cache lookup_plain c=8 q=400: p50 0.63 ms p99 1.38 ms
    memory rust-data-plane+dp+cache after load: gunicorn processes [48, 64, 64] MB, data plane 19 MB
