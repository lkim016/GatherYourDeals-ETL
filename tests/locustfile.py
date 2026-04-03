"""
Experiment 1 Phase B — Service-layer Load Test
===============================================
Targets POST /etl with a mocked backend (mock=true query param) to measure
FastAPI + uvicorn throughput at 100 RPS and 500 RPS across W=1,2,4 workers.

No real API calls are made — the service swaps the pipeline for asyncio.sleep
stubs matching observed baseline latencies. Cost: $0.00.

Usage
-----
  # Start the service in mock mode (separate terminal):
  uvicorn app:app --host 0.0.0.0 --port 8080 --workers 1

  # Run at 100 RPS with 1 worker:
  locust -f experiments/locustfile.py --headless -u 100 -r 10 \
      --run-time 60s --host http://localhost:8080

  # Run at 500 RPS:
  locust -f experiments/locustfile.py --headless -u 500 -r 50 \
      --run-time 60s --host http://localhost:8080

  # Web UI (interactive):
  locust -f experiments/locustfile.py --host http://localhost:8080

Notes
-----
- Each Locust user sends 1 req/s (constant_throughput(1)); spawn N users → N RPS
- The service must be started with ?mock=true support in POST /etl
  (see app.py — mock mode sleeps for a lognormal-sampled duration instead of
  running the real pipeline).
- Use --workers W flag to uvicorn to test W=1, 2, 4 server workers.
  Each configuration is a separate Locust run.
"""

from locust import HttpUser, task, constant_throughput


# ---------------------------------------------------------------------------
# Single-user behaviour
# ---------------------------------------------------------------------------

class EtlUser(HttpUser):
    """
    One virtual user that sends POST /etl at a constant rate of 1 req/s.
    Spawn N users → N RPS target throughput.

    mock=true tells the service to skip real OCR/LLM/geocode and instead
    sleep for a lognormal draw (P50≈9.2s, matching total baseline latency),
    so no credits are burned.
    """

    weight = 5
    # 1 request per second per user — spawning 100 users → target 100 RPS
    wait_time = constant_throughput(1)

    @task
    def run_etl(self):
        """Send one ETL request in mock mode."""
        self.client.post(
            "/etl",
            json={"source": "https://example.com/receipt.jpg"},
            params={"mock": "true"},
            name="POST /etl (mock)",
            # Allow up to 60 s for the mocked pipeline sleep
            timeout=120,
        )


# ---------------------------------------------------------------------------
# Health-check user (optional — 5% of traffic)
# ---------------------------------------------------------------------------

# class HealthUser(HttpUser):
#     """
#     Lightweight health-check traffic to verify the service stays alive
#     under load.  Weight=5 means ~1 health user per 20 EtlUsers.
#     """

#     weight = 1
#     wait_time = constant_throughput(1)

#     @task
#     def check_health(self):
#         self.client.get("/health", name="GET /health")
