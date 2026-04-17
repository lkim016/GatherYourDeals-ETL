"""
Locust file for integration load test (start_integration.py).

Sends real POST /etl requests — no ?mock=true — so the full ETL code path
runs with patched providers. Use alongside start_integration.py.

Usage:
    # 100 RPS
    locust -f tests/locustfile_integration.py --headless -u 100 -r 10 \\
        --run-time 60s --host http://localhost:8080

    # 500 RPS
    locust -f tests/locustfile_integration.py --headless -u 500 -r 50 \\
        --run-time 60s --host http://localhost:8080

    # Web UI
    locust -f tests/locustfile_integration.py --host http://localhost:8080
"""

from locust import HttpUser, task, constant_throughput


class EtlUser(HttpUser):
    """
    One virtual user sending POST /etl at 1 req/s.
    Spawn N users → N RPS target.

    Uses a local path so _resolve_source() runs but no download occurs.
    The source path doesn't need to exist — the mock_ocr stub returns
    static text regardless of the image content.
    """

    host = "http://localhost:8080"
    wait_time = constant_throughput(1)

    @task
    def run_etl(self):
        self.client.post(
            "/etl",
            json={"source": "/mnt/c/Users/Lolo/Desktop/Khoury/Semesters/2026-5Spr/CS6650/Final/GatherYourDeals-ETL/src/tests/dummy/2026-03-01Costco.jpg"},
            name="POST /etl (integration)",
            timeout=120,
        )


# class HealthUser(HttpUser):
#     """Lightweight health-check traffic — ~1 per 20 EtlUsers."""

#     host = "http://localhost:8080"
#     weight = 5
#     wait_time = constant_throughput(1)

#     @task
#     def check_health(self):
#         self.client.get("/health", name="GET /health")
