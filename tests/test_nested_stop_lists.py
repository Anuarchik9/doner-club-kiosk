import unittest
from unittest.mock import patch
import app as kiosk
from availability import AvailabilityUnavailable


class NestedStopListTests(unittest.TestCase):
    def payload(self):
        return {"terminalGroupStopLists": [
            {"organizationId": "other", "items": [
                {"terminalGroupId": "other-terminal", "items": [
                    {"productId": "other-dish", "balance": 0}]}]},
            {"organizationId": "ORG", "items": [
                {"terminalGroupId": "terminal", "items": [
                    {"productId": "bubble", "balance": 0},
                    {"productId": "flavour", "balance": -1},
                    {"productId": "available", "balance": 5}]}]}]}

    def test_real_api_nesting_and_organization_scope(self):
        stopped, items = kiosk.normalize_stop_list(self.payload(), "org")
        self.assertEqual(stopped, ["bubble", "flavour"])
        self.assertEqual(len(items), 3)
        self.assertTrue(all(i["terminalGroupId"] == "terminal" for i in items))
        self.assertFalse(items[-1]["stopped"])

    def test_nested_empty_is_valid(self):
        data = {"terminalGroupStopLists": [{"organizationId": "org",
                "items": [{"terminalGroupId": "terminal", "items": []}]}]}
        self.assertEqual(kiosk.normalize_stop_list(data, "org"), ([], []))

    def test_invalid_structure_does_not_look_like_empty_stop_list(self):
        for child in ({}, {"terminalGroupId": "terminal"}, None):
            data = {"terminalGroupStopLists": [{"organizationId": "org", "items": [child]}]}
            with self.subTest(child=child), self.assertRaises(ValueError):
                kiosk.normalize_stop_list(data, "org")

    def test_nested_stops_reach_endpoint_and_order_guard(self):
        department = {"organizationId": "org", "code": "Arai"}
        with patch.object(kiosk, "crm_state", return_value=({"acceptsOrders": True}, set())), \
             patch.object(kiosk, "get_stop_lists", return_value=self.payload()), \
             patch.object(kiosk, "find_department", return_value=(department, [])):
            response = kiosk.app.test_client().get("/kiosk-stop-list")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["stoppedProductIds"], ["bubble", "flavour"])
            for order in ([{"productId": "bubble"}],
                          [{"productId": "dish", "modifiers": [{"productId": "flavour"}]}]):
                result = kiosk.check_order_availability({"department": department}, order)
                self.assertEqual(result["code"], "PRODUCT_STOPPED")

    def test_malformed_nested_response_blocks_checkout(self):
        with patch.object(kiosk, "crm_state", return_value=({"acceptsOrders": True}, set())), \
             patch.object(kiosk, "get_stop_lists", return_value={"terminalGroupStopLists": [
                 {"organizationId": "org", "items": [{"terminalGroupId": "terminal"}]}]}):
            with self.assertRaises(AvailabilityUnavailable):
                kiosk.combined_availability({"organizationId": "org", "code": "Arai"})
