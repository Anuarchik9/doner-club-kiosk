"""No live iiko calls, orders, payments or customer data in these regressions."""
import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
from unittest.mock import Mock, patch

import requests
import app as gateway
from iiko_reference_cache import ReferenceCache, REFERENCE_FIELDS


def response(data, status=200, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(data).encode()
    result.headers.update(headers or {})
    result.url = "https://api-ru.iiko.services/test"
    return result


class ReferenceCacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.cache = ReferenceCache(clock=lambda: self.now, wall_clock=lambda: 1000.0)
        self.path = "/api/1/payment_types"
        self.payload = {"organizationIds": ["arai"]}
        self.loader = Mock(return_value=response({"paymentTypes": []}))

    def get(self, payload=None, credential="key"):
        return self.cache.get(self.path, payload or self.payload, credential, self.loader)

    def test_two_points_keep_separate_data_under_repeated_requests(self):
        for point in ("arai", "republic"):
            self.loader.return_value = response({"paymentTypes": [{"id": point}]})
            payload = {"organizationIds": [point]}
            for _ in range(100):
                self.assertEqual(self.get(payload).json()["paymentTypes"][0]["id"], point)
        self.assertEqual(self.loader.call_count, 2)

    def test_simultaneous_cold_requests_make_one_upstream_call(self):
        started, release = threading.Event(), threading.Event()
        def fetch():
            started.set()
            self.assertTrue(release.wait(5))
            return response({"paymentTypes": []})
        self.loader.side_effect = fetch
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(self.get) for _ in range(8)]
            self.assertTrue(started.wait(5))
            release.set()
            self.assertTrue(all(future.result(timeout=5).ok for future in futures))
        self.loader.assert_called_once()

    def test_expiry_refetches_and_does_not_return_old_mapping(self):
        self.get()
        self.now += 600
        self.loader.return_value = response({"paymentTypes": [{"id": "new"}]})
        self.assertEqual(self.get().json()["paymentTypes"], [{"id": "new"}])
        self.assertEqual(self.loader.call_count, 2)

    def test_returned_data_cannot_mutate_cache(self):
        self.get().json()["paymentTypes"].append({"id": "wrong"})
        self.assertEqual(self.get().json()["paymentTypes"], [])

    def test_credential_rotation_does_not_reuse_payment_mapping(self):
        self.get(credential="old")
        self.get(credential="new")
        self.assertEqual(self.loader.call_count, 2)

    def test_429_cooldown_spans_organizations_and_honours_full_retry_after(self):
        self.loader.return_value = response({"error": "rate limit"}, 429, {"Retry-After": "900"})
        self.assertEqual(self.get().status_code, 429)
        self.now += 899
        self.assertEqual(self.get({"organizationIds": ["republic"]}).status_code, 429)
        self.loader.assert_called_once()
        self.now += 1
        self.loader.return_value = response({"paymentTypes": []})
        self.assertTrue(self.get().ok)
        self.assertEqual(self.loader.call_count, 2)

    def test_http_date_retry_after_and_other_method_are_independent(self):
        self.loader.return_value = response({}, 429, {"Retry-After": formatdate(1060, usegmt=True)})
        self.get()
        other = Mock(return_value=response({"discounts": []}))
        self.assertTrue(self.cache.get("/api/1/discounts", self.payload, "key", other).ok)
        self.now += 59
        self.assertEqual(self.get().status_code, 429)
        self.loader.assert_called_once()
        self.now += 1
        self.loader.return_value = response({"paymentTypes": []})
        self.assertTrue(self.get().ok)

    def test_expired_data_is_not_used_after_failure(self):
        self.get()
        self.now += 600
        self.loader.return_value = response({}, 503)
        self.assertEqual(self.get().status_code, 503)
        self.assertEqual(self.get().status_code, 503)
        self.assertEqual(self.loader.call_count, 2)
        self.now += 30
        self.loader.return_value = response({"paymentTypes": [{"id": "updated"}]})
        self.assertEqual(self.get().json()["paymentTypes"][0]["id"], "updated")

    def test_malformed_success_is_not_cached_as_a_valid_empty_mapping(self):
        self.loader.return_value = response({"correlationId": "partial"})
        with self.assertRaises(ValueError):
            self.get()
        self.assertEqual(self.cache.diagnostics()["entries"], 0)
        self.now += 30
        self.loader.return_value = response({"paymentTypes": []})
        self.assertTrue(self.get().ok)

    def test_timeout_does_not_cause_a_queue_of_repeated_requests(self):
        self.loader.side_effect = requests.Timeout("upstream unavailable")
        for _ in range(5):
            with self.assertRaises(requests.Timeout):
                self.get()
        self.loader.assert_called_once()

    def test_invalid_json_failure_can_be_replayed_without_constructor_errors(self):
        broken = response({})
        broken._content = b"not json"
        self.loader.return_value = broken
        for _ in range(3):
            with self.assertRaises(ValueError):
                self.get()
        self.loader.assert_called_once()

    def test_orders_balances_and_stop_lists_cannot_be_cached(self):
        for path in ("/api/1/order/create", "/api/1/order/by_id", "/api/1/order/close",
                     "/api/1/stop_lists", "/api/1/terminal_groups/is_alive"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.cache.get(path, {}, "key", self.loader)
        self.loader.assert_not_called()

    def test_cache_size_is_bounded(self):
        self.cache.max_entries = 4
        for number in range(20):
            self.get({"organizationIds": [str(number)]})
        self.assertEqual(self.cache.diagnostics()["entries"], 4)


class ReferenceIntegrationTests(unittest.TestCase):
    def setUp(self):
        gateway._iiko_reference_cache.clear()
        self.addCleanup(gateway._iiko_reference_cache.clear)

    def test_real_resolvers_reuse_all_three_references_for_both_points(self):
        def fetch(path, payload, **kwargs):
            point = payload["organizationIds"][0]
            data = {
                "/api/1/terminal_groups": {"terminalGroups": [{"organizationId": point,
                    "items": [{"id": point + "-terminal", "name": point}]}]},
                "/api/1/payment_types": {"paymentTypes": [{"id": point + "-payment",
                    "name": "CALL CENTRE BASE", "paymentTypeKind": "Card", "isDeleted": False}]},
                "/api/1/discounts": {"discounts": [{"organizationId": point,
                    "items": [{"id": point + "-discount", "name": "10%", "percent": 10, "mode": "Percent"}]}]},
            }
            return response(data[path])
        with patch.object(gateway, "iiko_kiosk_post", side_effect=fetch) as send:
            for _ in range(50):
                for point in ("arai", "republic"):
                    groups, _ = gateway.get_terminal_groups_for_organization(point)
                    payments, error = gateway._call_centre_payment_types(point)
                    discounts, discount_error = gateway._call_centre_discounts(point)
                    self.assertIsNone(error)
                    self.assertIsNone(discount_error)
                    self.assertEqual(groups[0]["id"], point + "-terminal")
                    self.assertEqual(payments[0]["id"], point + "-payment")
                    self.assertEqual(discounts[0]["id"], point + "-discount")
            self.assertEqual(send.call_count, 6)

    def test_terminal_liveness_is_not_cached(self):
        with patch.object(gateway, "iiko_kiosk_post", side_effect=[
                response({"isAliveStatus": [{"terminalGroupId": "terminal", "isAlive": True}]}),
                response({"isAliveStatus": [{"terminalGroupId": "terminal", "isAlive": False}]})]) as send:
            self.assertTrue(gateway.get_terminal_groups_alive("arai", ["terminal"])["terminal"])
            self.assertFalse(gateway.get_terminal_groups_alive("arai", ["terminal"])["terminal"])
            self.assertEqual(send.call_count, 2)

    def test_each_order_reaches_its_own_point_and_is_never_cached(self):
        def department(point):
            organization = {"ARAI": gateway.ARAI_ORGANIZATION_ID,
                            "RESPUBLIKA": gateway.REPUBLIC_ORGANIZATION_ID}[point]
            return {"organizationId": organization, "code": point}, []

        def fetch(path, payload, **kwargs):
            if path == "/api/1/terminal_groups":
                organization = payload["organizationIds"][0]
                return response({"terminalGroups": [{"organizationId": organization,
                    "items": [{"id": organization + "-terminal"}]}]})
            self.assertEqual(path, "/api/1/order/create")
            self.assertEqual(payload["terminalGroupId"], payload["organizationId"] + "-terminal")
            return response({"correlationId": "offline-command", "orderInfo": {"id": "offline-order"}})

        with patch.dict(os.environ, {"IIKO_KIOSK_API_KEY": "offline-test",
                                    "CALL_CENTRE_SERVICE_TOKEN": "offline-test"}), \
             patch.object(gateway, "find_department", side_effect=department), \
             patch.object(gateway, "check_order_availability", return_value=None), \
             patch.object(gateway, "_wait_command", return_value={"state": "Success"}), \
             patch.object(gateway, "iiko_kiosk_post", side_effect=fetch) as send:
            for _ in range(2):
                for point in ("ARAI", "RESPUBLIKA"):
                    organization = department(point)[0]["organizationId"]
                    result = gateway.app.test_client().post("/kiosk-test-order",
                        headers={"X-Call-Centre-Service-Token": "offline-test"}, json={
                        "point": point, "confirm": "SEND_TEST_ORDER",
                        "terminalGroupId": organization + "-terminal",
                        "items": [{"productId": "offline-product", "amount": 1}]})
                    self.assertEqual(result.status_code, 200, result.json)
            creates = [call for call in send.call_args_list if call.args[0] == "/api/1/order/create"]
            self.assertEqual(len(creates), 4)
            self.assertEqual([call.args[1]["organizationId"] for call in creates],
                             [gateway.ARAI_ORGANIZATION_ID, gateway.REPUBLIC_ORGANIZATION_ID] * 2)
            self.assertEqual(send.call_count, 6)

    def test_transport_does_not_immediately_repeat_a_reference_429(self):
        for path in REFERENCE_FIELDS:
            with self.subTest(path=path), patch.object(gateway.requests, "post", return_value=response({}, 429)) as send:
                self.assertEqual(gateway._post(path, {}, lambda: "offline-test-token").status_code, 429)
                send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
