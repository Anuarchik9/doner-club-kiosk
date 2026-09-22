import os
import unittest
from unittest.mock import Mock, patch

import requests
import app as kiosk


class KioskTests(unittest.TestCase):
    def setUp(self):
        self.client = kiosk.app.test_client()
        self.payload = {
            "confirm": "SEND_PAID_TEST_ORDER", "acknowledgeFinancialEffect": True,
            "terminalGroupId": "terminal", "tableId": "table", "paymentSum": 1500,
            "items": [{"productId": "doner", "amount": 1}],
        }
        self.env = patch.dict(os.environ, {"IIKO_KIOSK_API_KEY": "test-only"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.validate = patch.object(kiosk, "_validate_point_terminal_table", return_value=(
            {"organizationId": "org"}, None, 200)).start()
        self.post = patch.object(kiosk, "iiko_kiosk_post").start()
        self.wait = patch.object(kiosk, "_wait_command").start()
        self.addCleanup(patch.stopall)
        self.post.return_value = Mock(ok=True, json=lambda: {
            "correlationId": "command", "orderInfo": {"id": "order"}})

    def paid(self):
        return self.client.post('/kiosk-test-paid-order', json=self.payload)

    def test_static_routes(self):
        for path in ('/', '/kiosk', '/static/favicon.svg', '/health'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                response.close()

    def test_non_object_json(self):
        for path in ('/kiosk-test-order', '/kiosk-test-paid-order'):
            for value in ([1], 'bad', 1):
                with self.subTest(path=path, value=value):
                    self.assertEqual(self.client.post(path, json=value).status_code, 400)
        self.post.assert_not_called()

    def test_confirmation_required(self):
        self.payload['acknowledgeFinancialEffect'] = False
        self.assertEqual(self.paid().status_code, 400)
        self.post.assert_not_called()

    def test_invalid_sums(self):
        for value in ('NaN', 'Infinity', '-Infinity', 0, -1, 5001, True, 'bad'):
            with self.subTest(value=value):
                self.payload['paymentSum'] = value
                self.assertEqual(self.paid().status_code, 400)
        self.post.assert_not_called()

    def test_invalid_items_rejected_before_iiko(self):
        for items in ([None], 'bad', [dict(productId='p', amount='NaN')],
                      [dict(productId='p', amount=True)],
                      [dict(productId='p', amount=1, modifiers=[dict(productId='m', amount=0)])],
                      [dict(productId='p', amount=1, modifiers=[None])],
                      [dict(productId='p', amount=1), dict(amount=1)]):
            with self.subTest(items=items):
                self.payload['items'] = items
                self.assertEqual(self.paid().status_code, 400)
                unpaid = dict(self.payload, confirm='SEND_TEST_ORDER')
                self.assertEqual(self.client.post('/kiosk-test-order', json=unpaid).status_code, 400)
        self.validate.assert_not_called()
        self.post.assert_not_called()

    def test_preserves_size_and_modifiers(self):
        items = kiosk._normalize_order_items([dict(productId='p', productSizeId='s', amount=2,
            modifiers=[dict(productId='m', productGroupId='g', amount=1)])])
        self.assertEqual(items[0]['productSizeId'], 's')
        self.assertEqual(items[0]['amount'], 2)
        self.assertEqual(items[0]['modifiers'][0]['productGroupId'], 'g')

    def test_pending_create_never_closes(self):
        for state in (None, {'state': 'InProgress'}):
            with self.subTest(state=state):
                self.post.reset_mock()
                self.wait.return_value = state
                response = self.paid()
                self.assertEqual(response.status_code, 202)
                self.assertFalse(response.json['success'])
                self.assertEqual(self.post.call_count, 1)

    def test_failed_create_never_closes(self):
        self.wait.return_value = {'state': 'Error'}
        self.assertEqual(self.paid().status_code, 502)
        self.assertEqual(self.post.call_count, 1)

    def test_close_status(self):
        for state, status in ((None, 202), ({'state': 'InProgress'}, 202),
                              ({'state': 'Error'}, 502), ({'state': 'Success'}, 200)):
            with self.subTest(state=state):
                self.wait.side_effect = [{'state': 'Success'}, state]
                response = self.paid()
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json['success'], status == 200)

    def test_unpaid_status(self):
        for state, status in ((None, 202), ({'state': 'Error'}, 502), ({'state': 'Success'}, 200)):
            with self.subTest(state=state):
                self.wait.return_value = state
                response = self.client.post('/kiosk-test-order', json=dict(self.payload, confirm='SEND_TEST_ORDER'))
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json['success'], status == 200)

    def test_missing_config(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.client.get('/kiosk-menu').status_code, 503)
            self.assertEqual(self.client.get('/kiosk-iiko-config').status_code, 503)

    def test_blank_point_does_not_select_first_department(self):
        with patch.object(kiosk, 'get_departments') as get:
            self.assertEqual(kiosk.find_department(' '), (None, []))
            get.assert_not_called()

    def test_menu_timeout(self):
        with patch.object(kiosk, 'find_department', side_effect=requests.Timeout):
            self.assertEqual(self.client.get('/kiosk-menu').status_code, 504)

    def test_menu_normalization(self):
        menu = {'itemCategories': [{'id': 'hidden', 'isHidden': True}, {
            'id': 'food', 'name': 'Food', 'items': [
                {'itemId': 'p', 'name': 'Doner', 'itemSizes': [
                    {'sizeId': 'small', 'sizeName': 'Small', 'prices': [{'organizationId': 'org', 'price': 1200}]},
                    {'sizeId': 'large', 'isHidden': True}]},
                {'itemId': 'hidden', 'isHidden': True}]}]}
        categories, products = kiosk.normalize_external_menu(menu, 'org')
        self.assertEqual(len(categories), 1)
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]['price'], 1200)
        self.assertEqual(products[0]['sizeId'], 'small')

    def test_invalid_menu_prices(self):
        for value in ('NaN', 'Infinity', -1, True, None):
            with self.subTest(value=value):
                self.assertIsNone(kiosk._menu_price([{'organizationId': 'org', 'price': value}], 'org'))
        self.assertEqual(kiosk._menu_price([{'organizationId': 'org', 'price': 0}], 'org'), 0)


if __name__ == '__main__':
    unittest.main()
