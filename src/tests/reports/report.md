# Report
## Experiment 1: Load Test Integration & Baseline Calibration

### Purpose
The objective of this test suite is to validate the internal orchestration of the ETL pipeline under realistic latency distributions while controlling for external variables.

* **Experiment 1b (Baseline Calibration):** This phase isolates the system from the cost and variability of external cloud providers. By using calibrated mocks for OCR and LLM stages, we establish a performance ceiling and validate near-linear scaling of Uvicorn workers.
* **Experiment 1c (Stability & Backpressure):** Building on the baseline, this test introduces application-level semaphores to serialize high-concurrency traffic. We enforce a 15-request cap on the OCR stage to simulate "Backpressure," ensuring the service does not exceed provider rate-limits or exhaust system memory under a 100-user load.

## Experiment 1b: peak performance metrics
| Configuration | Total Requests | Peak RPS | P50 (Median) | P99 (Max) | Status |
| :---: | :---: | :---: | :---: | :---: | :---: |
| W = 1 | 60 | 1.00 | 8,300 ms | 8,400 ms | Stable (Low Load) |
| W = 2 | 243 | 5.00 | 8,300 ms | 14,000 ms |  Healthy |
| W = 4 | 525 | 10.40 | 10,000 ms | 13,000 ms | High Performance |

### Findings:

1. Scaling Efficiency: Moving from 1 to 4 workers resulted in a 10.4x increase in throughput (from 1.0 to 10.4 RPS). This near-linear scaling indicates that the ETL logic is successfully utilizing parallel worker processes without significant resource contention in the baseline environment.

2. Latency Consistency: At its peak performance (W=4, 100 Users), the P99 latency (13,000 ms) remained well within the safety margin of a standard 30-second gateway timeout, despite the 10x increase in load.

3. Saturation Points: The raw data shows that saturation begins when users exceed the worker capacity by a significant margin (e.g., W=1 at 50 users or W=2 at 100 users). This baseline confirms that for a 100-user load, $W=4$ is the minimum requirement for a "Stable" status.

## Experiment 1c - peak performance metrics
| Configuration | Total Requests | Peak RPS | P50 (Median) | P99 (Max) | Status |
| :---: | :---: | :---: | :---: | :---: | :---: |
| W = 1 | 191 | 0.60 | 45,000 ms | 70,000 ms | Congestive Collapse |
| W = 2 | 393 | 2.70 | 27,000 ms | 33,000 ms | Saturated |
| W = 4 | 665 | 5.60 | 16,000 ms | 22,000 ms | Stable / Healthy |
| W = 8 | 813 | 9.70 | 13,000 ms | 19,000 ms | Optimal |


### Findings:
1. The System is Worker-Bound, not Hardware-Bound. The near-linear jump in throughput from W=4 (5.6 RPS) to W=8 (9.7 RPS) proves that the underlying infrastructure (CPU/RAM) and database are not yet the bottleneck. The primary constraint was the number of available Uvicorn worker "slots" to process the blocking ETL logic.

2. Elimination of the "Timeout Cliff." In W=1 and W=2 configurations, the tail-end latency (P99) exceeded 30 seconds. In a production environment, this would result in 504 Gateway Timeouts. $W=8$ reduced the P99 to 19 seconds, providing an 11-second safety buffer for real-world network jitter.

3. Effective Serialization via Semaphores (Backpressure). By enforcing a concurrency cap (semaphore), the application remained at 0% failures across all tests. Instead of crashing the service by attempting 100 simultaneous LLM/OCR connections, the system correctly queued requests, trading temporary latency for total system stability.

4. Optimal Throughput vs. Latency Balance. The "Sweet Spot" for this architecture was found at $W=8$. At this level, median wait times (P50) dropped to ~13s, where the "waiting room" delay is only a small fraction (~4.5s) of the total processing time.

**Conclusion: Architectural Verification:**
Experiment 1c confirms that the ETL-Process is Production-Ready at the W=8 worker tier. This configuration allows the microservice to absorb a "rush hour" of 100 concurrent users while keeping response times well within industry-standard timeout thresholds (30s).

# Experiment Test 2 - Provider-Model Optimization & Cost-Efficiency Benchmarking
Provider-Model Quality, Cost, Token Use, Latency

Purpose: This experiment is to perform a comparative performance audit between Large Language Model (LLM) providers to resolve the accuracy, latency, cost dilemma. These benchmarks are to find the Service Level Objectives (SLOs) of this ETL process. I discovered that by switching to Gemma, I could increase extraction accuracy by 1.5% while reducing my API overhead by 99%, ultimately selecting a model that is both more precise and more reliable for a high-concurrency production environment.

## Phase I - Provider-Model Field-Level Accuracy & Use
Date range of tests leading to these results: 2026-03-23 - 2026-03-31
- 2 result captured in detail
### Summary Analysis
Avg field level accuracy:

| Provider | Model | Avg Score | Key Strength | Primary Weakness |
| :---: | :---: | :---: | :---: | :---: |
| OpenRouter| Claude-Haiku-4.5 | 63.5% | Complex Layouts (Multi-column) | Date Extraction (frequent ✗) |
| Clod | Qwen2.5-7B-7B | 58.5% | Standard single-item receipts | Multi-column Price/Qty confusion |

Avg Model Use:

| Provider | Model | Receipts | Fail rate | E2E P50 (ms) | E2E P95 (ms) | Throughput | LLM P50 (ms) | LLM P95 (ms) | Avg lat (ms) | Avg in tok | Avg out tok | Cost/receipt | Total cost |
|----------|-------|:--------:|:---------:|-------------:|-------------:|:----------:|-------------:|-------------:|-------------:|-----------:|------------:|-------------:|:----------:|
|clod | Qwen2.5-7B-Instruct-Turbo | 9 | 0/9 (0%) | 11,872 | 18,466 | ~5/min | 3,624 | 9,366 | 4,618 | 988,476 | $0.0003 | $0.0095 | $0.0095 |
openrouter | claude-haiku-4.5 | 9 | 0/9 (0%) | 15,368 | 32,523 | ~4/min | 4,072 | 14,568 | 16,654 | 1,002 | 562 | $0.0038 | $0.1028 |

1. The Accuracy vs. Cost Gap
Marginal Accuracy Gain: Claude-Haiku-4.5 is ~5% more accurate than Qwen2.5 (63.5% vs 58.5%). It is better suited for the "heavy lifting" of complex, multi-column layouts where Qwen struggles with column alignment.

- Exponential Cost Difference: Despite only a 5% lead in accuracy, Claude is 12.6x more expensive per receipt ($0.0038 vs $0.0003).

- Efficiency: For standard, single-item receipts, Qwen provides nearly the same value at a fraction of the price.

2. Latency and Throughput
E2E Responsiveness: Qwen2.5 is noticeably faster across the board. Its E2E P50 (11.8s) is roughly 3.5 seconds faster than Claude’s (15.3s).

- Tail Latency Stability: The difference becomes even more pronounced at the P95 level. Claude’s P95 (32.5s) is nearly double that of Qwen’s (18.4s), suggesting that Claude may hang or struggle significantly more on complex documents, whereas Qwen's processing time remains more predictable.

- LLM Processing: Qwen’s actual LLM inference time (Avg Latency 4.6s) is drastically lower than Claude's (16.6s), indicating that much of Claude's E2E time is spent on the model generation itself rather than pre-processing.

3. Token Efficiency
Output Verbosity: Claude-Haiku-4.5 uses more output tokens (562) compared to Qwen (476) for the same 9 receipts. This suggests Claude might be more verbose in its formatting or including extra metadata, which contributes to both the higher cost and the increased latency.
- Input Handling: Both models are roughly equal in input token consumption (~1,000 tokens), which is expected if they are processing the same source text/OCR data.

4. Qualitative Performance
The "Date" Blindspot: Interestingly, the more "capable" model (Claude) has a specific weakness in Date Extraction. If your pipeline relies heavily on chronological sorting, this is a critical failure point.

- The "Layout" Blindspot: Qwen’s weakness is structural (multi-column price/qty).

#### Summary:
Use Qwen2.5-7B for high-volume, standard retail receipts where speed and cost are the priority. It offers the best "bang for your buck."

Use Claude-Haiku-4.5 as a specialized "fallback" for receipts that fail initial validation or those identified as having complex, non-standard layouts.

Data Correction: Since Claude struggles with dates, you might consider a regex-based post-processing step or a specific prompting instruction to stabilize date extraction if you stick with that model.


## Phase II - Provider-Model Field-Level Accuracy & Use
Date range of tests leading to these results: 2026-03-31 - 2026-04-11 

2 result captured in detail
### Summary Analysis
Avg field level accuracy:

| Provider | Model | Avg Score | Key Strength | Primary Weakness |
| :---: | :---: | :---: | :---: | :---: |
| Clod | gemma-3n-E4B-it | 71.9% | High Item Name & Amount precision | Occasional store address (Lat/Lon coordinate) misses |
| OpenRouter | Qwen-2.5-7B-Instruct | 72.7% | Precision on specific fields when successful | "High Fail Rate (Frequent ""no output"")"
| OpenRouter | Claude-Haiku-4.5 | 70.4% | Consistent Store & Item Name matching | Date Extraction (frequent ✗) |
| Clod | Qwen2.5-7B-Turbo | 69.8% | Reliable throughput and name matching | Coordinate (Lat/Lon) & Item List validation |

Avg Model Use:

| Provider | Model | Receipts | Fail rate | E2E P50 (ms) | E2E P95 (ms) | Throughput | LLM P50 (ms) | LLM P95 (ms) | Avg lat (ms) | Avg in tok | Avg out tok | Cost/receipt | Total cost |
|----------|-------|:--------:|:---------:|-------------:|-------------:|:----------:|-------------:|-------------:|-------------:|-----------:|------------:|-------------:|:----------:|
| clod | Qwen2.5-7B-Instruct-Turbo | 20 | 0/20 (0%) | 30,687 | 77,828 | ~2/min | 22,776 | 70,302 | 26,494 | 8,836 | 2,884 | $0.0030 | $0.1247 |
| clod | gemma-3n-E4B-it | 11 | 0/20 (0%) | 75,149 | 135,444 | ~1/min | 64,381 | 123,494 | 68,244 | 8,123 | 1,643 | $0.0002 | $0.0027 |
| openrouter | claude-haiku-4.5 | 20 | 0/20 (0%) | 59,415 | 159,501 | ~1/min | 51,670 | 145,082 | 60,869 | 8,946 | 4,544 | $0.0317 | $0.6492 |
| openrouter | qwen-2.5-7b-instruct | 10 | 10/20 (50%) | 74,495 | 107,080 | ~1/min | 65,909 | 101,250 | 66,893 | 7,058 | 2,356 | $0.0005 | $0.0052 |

1. The Accuracy vs. Cost Gap
The Gemma Advantage: gemma-3n-E4B-it has emerged as the high-accuracy leader (71.9%), slightly edging out Claude-Haiku-4.5. It breaks the traditional "pay for precision" model by offering top-tier extraction at a near-zero cost floor ($0.0002).
- The Cost-Accuracy Paradox: Claude-Haiku-4.5 is the most expensive model in the set ($0.0317/receipt), yet it ranks third in accuracy. You are essentially paying a 150x premium compared to Gemma for lower field-level performance.
- The Qwen Middle Ground: Qwen2.5-7B-Turbo (via Clod) maintains a respectable 69.8% accuracy. While slightly lower than Gemma, its cost is similarly negligible, making the "Clod" provider models significantly more efficient than the "OpenRouter" alternatives for this specific task.

2. Latency and Throughput
The Speed Champion: Qwen2.5-7B-Turbo is the clear winner for real-time applications, with an E2E P50 of 30.6s and a throughput of ~2/min. It is twice as fast as the other models in the lineup.
- The "Heavy Hitter" Slowdown: gemma-3n-E4B-it and Claude-Haiku-4.5 both show significant latency, with P50s around 60–75s. However, Claude’s P95 (159.5s) indicates a much higher risk of "hanging" on difficult receipts, whereas the Qwen models stay much more predictable.
- LLM Inference Efficiency: The Clod-based Qwen model spends the least amount of time in actual inference (Avg Latency 8.8s), whereas Claude and Gemma spend significantly longer (60s+) generating their structured responses.

3. Token Efficiency
High Verbosity Costs: Claude-Haiku-4.5 is the most verbose model by far, generating 4,544 output tokens for a 20-receipt set. This high token count is the primary driver of its $0.64 total cost.
- Lean Extraction: gemma-3n-E4B-it is highly efficient with its output (1,643 tokens), providing structured data without unnecessary "chatter." This suggests Gemma's prompt-following for JSON or structured output is more dialed-in for ETL tasks.
- Input Consistency: All models are processing roughly the same input volume (7k–8k tokens), confirming that the performance variance is due to model architecture and provider-side inference handling rather than the data source.

4. Qualitative Performance
Reliability vs. Precision: The OpenRouter Qwen-2.5-7B-Instruct is technically the most "accurate" when it works, but a 50% fail rate (No Output) makes it a liability for an automated pipeline.
- Structural Blindspots: * Claude: Persistently fails at Date Extraction.
- Gemma: Struggles with Store Address (Lat/Lon) logic but excels at the core "Items" and "Amounts."
- Qwen: Best for standard retail but struggles with the Item List validation on complex layouts.

#### Summary:
Primary ETL Choice: clod / gemma-3n-E4B-it. It provides the highest accuracy score, the most efficient token usage, and the lowest cost. It is the best "production-grade" model for this pipeline.

Real-time / High-Volume Choice: clod / Qwen2.5-7B-Turbo. Use this if the 30s vs. 75s latency difference is critical for your user experience, as it remains highly reliable and fast.

Deprecated: OpenRouter / Claude-Haiku-4.5. Based on these numbers, the high cost and systemic date failure make it the least viable option for your current receipt processing logic.


## Synthesis with Test 2 (Provider Optimization)
The stability findings from Experiment 1c directly support the model selection in Experiment 2. To handle a 100-user concurrent load effectively:
- Compute Tier: Deploy with $W=8$ Uvicorn workers.
- Model Tier: Utilize Gemma-3n-E4B-it via Clod for the highest accuracy and lowest cost.
- Safety Net: Maintain existing concurrent semaphores to protect against provider rate-limits and resource exhaustion..

## Limitations:
- Provider Constraints: Identified that identical models perform differently across API providers (e.g., Clod vs. OpenRouter) due to infrastructure tiers (account type).

- Schema Sensitivity: Bulk-retailer receipts (Costco) occasionally trigger ValueError due to non-standard layouts.

- Next Steps: Future iterations will implement Graceful Degradation (extracting headers even if line-items fail) and Adaptive Backpressure to adjust worker counts dynamically based on queue depth.


