from unittest.mock import patch

from django.test import TestCase


class HealthCheckGatewayBypassTests(TestCase):
    """The gateway middleware must not run its DB-backed rate limiting for health
    checks. Liveness/readiness probes hit /healthz constantly, so a transient DB
    issue must not fail the probe and cycle otherwise-healthy pods."""

    def test_healthz_bypasses_rate_limiting(self):
        with patch(
            "api.middleware.gateway.GatewayControlMiddleware._check_rate_limit"
        ) as check_rate_limit:
            response = self.client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"success": True, "message": "healthy"})
        check_rate_limit.assert_not_called()

    def test_non_health_path_still_rate_limited(self):
        # A path that skips API-key validation but is not a health check should
        # still flow through the rate limiter — the bypass is health-only.
        with patch(
            "api.middleware.gateway.GatewayControlMiddleware._check_rate_limit",
            return_value={"blocked": False, "limit": 0, "remaining": -1, "reset": 0},
        ) as check_rate_limit:
            self.client.get("/api/v1/core/payments/callbacks/")

        check_rate_limit.assert_called()
