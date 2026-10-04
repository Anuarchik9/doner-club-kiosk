import unittest
from unittest.mock import Mock, patch

import requests
import app as kiosk
from availability import AvailabilityUnavailable, crm_state, catalog_stop_ids


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.client = kiosk.app.test_client()
        self.department = {"organizationId": "org", "code": "Arai"}
        self.state = {"acceptsOrders": True, "acceptsDelivery": False, "pickupMinutes": 30}

    def test_crm_reads_both_authenticated_resources(self):
        with patch('availability.requests.get') as get:
            get.side_effect = [Mock(json=lambda: {"ok": True, "location": self.state}),
                               Mock(json=lambda: {"ok": True, "items": [{"productCode": "DONER"}]})]
            state, codes = crm_state('https://crm.example', 'secret', 'ARAI')
            self.assertEqual(codes, {'doner'})
            self.assertEqual(state['pickupMinutes'], 30)
            self.assertEqual(get.call_count, 2)
            self.assertEqual(get.call_args.kwargs['headers'], {'X-API-Key': 'secret'})

    def test_unavailable_or_malformed_crm_fails_closed(self):
        for payload in (None, {}, {"ok": True, "location": {}}, {"ok": False}):
            with self.subTest(payload=payload), patch('availability.requests.get', return_value=Mock(json=lambda: payload)):
                with self.assertRaises(AvailabilityUnavailable):
                    crm_state('https://crm.example', 'secret', 'ARAI')
        with patch('availability.requests.get', side_effect=requests.Timeout):
            with self.assertRaises(AvailabilityUnavailable):
                crm_state('https://crm.example', 'secret', 'ARAI')

    def test_union_and_sku_modifier_mapping(self):
        menu = {"items": [{"itemId": "DISH", "sku": "123", "modifiers": [{"itemId": "EXTRA", "sku": "456"}]}]}
        with patch.object(kiosk, 'crm_state', return_value=(self.state, {'123', '456'})), \
             patch.object(kiosk, 'get_stop_lists', return_value={'terminalGroupStopLists': [{'items': [{'productId': 'IIKO', 'balance': 0}]}]}), \
             patch.object(kiosk, 'get_external_menus', return_value={'externalMenus': [{'id': 'menu'}]}), \
             patch.object(kiosk, 'get_external_menu_by_id', return_value=menu):
            stopped, _, state = kiosk.combined_availability(self.department)
            self.assertTrue({'dish', 'extra', 'iiko'}.issubset(stopped))
            self.assertTrue(state['acceptsOrders'])

    def test_order_guards_both_endpoints_without_sending(self):
        for route, confirm in (('/kiosk-test-order', 'SEND_TEST_ORDER'), ('/kiosk-test-paid-order', 'SEND_PAID_TEST_ORDER')):
            for stopped, state, expected in ((['dish'], self.state, 'PRODUCT_STOPPED'),
                                             (['extra'], self.state, 'PRODUCT_STOPPED'),
                                             ([], {'acceptsOrders': False}, 'POINT_CLOSED')):
                with self.subTest(route=route, expected=expected), \
                     patch.dict(kiosk.os.environ, {'IIKO_KIOSK_API_KEY': 'test'}), \
                     patch.object(kiosk, '_validate_point_terminal', return_value=({'organizationId': 'org', 'department': self.department}, None, 200)), \
                     patch.object(kiosk, 'combined_availability', return_value=(stopped, [], state)), \
                     patch.object(kiosk, 'iiko_kiosk_post') as send:
                    response = self.client.post(route, json={'confirm': confirm, 'acknowledgeFinancialEffect': True,
                        'paymentSum': 1000, 'terminalGroupId': 'terminal', 'tableId': 'table',
                        'items': [{'productId': 'DISH', 'amount': 1, 'modifiers': [{'productId': 'EXTRA', 'amount': 1}]}]})
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.json['code'], expected)
                    send.assert_not_called()

    def test_delivery_stop_does_not_block_kiosk(self):
        with patch.object(kiosk, 'combined_availability', return_value=([], [], self.state)):
            self.assertIsNone(kiosk.check_order_availability({'department': self.department}, [{'productId': 'dish'}]))

    def test_endpoint_never_serves_success_on_dependency_failure(self):
        with patch.object(kiosk, 'find_department', return_value=(self.department, [])), \
             patch.object(kiosk, 'combined_availability', side_effect=AvailabilityUnavailable('Unavailable')):
            response = self.client.get('/kiosk-stop-list')
            self.assertEqual(response.status_code, 503)
            self.assertFalse(response.json['success'])
            self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_manual_resume_preserves_iiko_stop(self):
        with patch.object(kiosk, 'crm_state', return_value=(self.state, set())), \
             patch.object(kiosk, 'get_stop_lists', return_value={'terminalGroupStopLists': [{'items': [{'productId': 'dish', 'balance': 0}, {'productId': 'available', 'balance': 2}]}]}):
            self.assertEqual(kiosk.combined_availability(self.department)[0], ['dish'])
