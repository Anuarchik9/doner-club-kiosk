import os
import unittest
from unittest.mock import Mock, patch

import app as kiosk


class SelfServiceTests(unittest.TestCase):
    def setUp(self):
        self.client = kiosk.app.test_client()
        self.target = {'organizationId': 'org', 'terminalGroupId': 'terminal', 'department': {}}
        self.card = {'id': 'card', 'name': 'Bank card', 'paymentTypeKind': 'Card', 'isProcessedExternally': True}

    def test_target_never_reads_or_selects_tables(self):
        with patch.object(kiosk, 'find_department', return_value=({'organizationId': 'org'}, [])), \
             patch.object(kiosk, 'get_terminal_groups_for_organization', return_value=([{'id': 'terminal'}], None)), \
             patch.object(kiosk, 'get_terminal_groups_alive', return_value={'terminal': True}), \
             patch.object(kiosk, 'get_restaurant_sections_for_terminal_groups') as tables, \
             patch.dict(os.environ, {'IIKO_KIOSK_ARAI_TABLE_ID': 'old-table'}):
            target, error, status = kiosk._resolve_kiosk_order_target('Arai')
            self.assertIsNone(error)
            self.assertEqual(status, 200)
            self.assertNotIn('tableId', target)
            tables.assert_not_called()

    def test_unpaid_test_does_not_set_payment_or_close(self):
        with patch.dict(os.environ, {'IIKO_KIOSK_API_KEY': 'test'}), \
             patch.object(kiosk, '_validate_point_terminal', return_value=(self.target, None, 200)), \
             patch.object(kiosk, 'check_order_availability', return_value=None), \
             patch.object(kiosk, '_wait_command', return_value={'state': 'Success'}), \
             patch.object(kiosk, 'iiko_kiosk_post', return_value=Mock(ok=True, json=lambda: {'orderInfo': {'id': 'order'}})) as post:
            response = self.client.post('/kiosk-test-order', json={'confirm': 'SEND_TEST_ORDER',
                'terminalGroupId': 'terminal', 'tableId': 'legacy-ignored',
                'payments': [{'paymentTypeId': 'untrusted'}],
                'items': [{'productId': 'p', 'amount': 1}]})
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(post.call_count, 1)
            self.assertEqual(post.call_args.args[0], '/api/1/order/create')
            order = post.call_args.args[1]['order']
            for field in ('tableIds', 'payments', 'comment', 'sourceKey', 'orderTypeId'):
                self.assertNotIn(field, order)
            self.assertEqual(order['tabName'], '')

    def test_call_centre_preserves_explicit_payment_and_discount(self):
        confirmed = {'creationStatus': 'Success', 'order': {'sum': 900, 'number': 42}}
        with patch.dict(os.environ, {'CALL_CENTRE_API_KEY': 'test', 'IIKO_KIOSK_API_KEY': 'test'}), \
             patch.object(kiosk, '_resolve_kiosk_order_target', return_value=(self.target, None, 200)), \
             patch.object(kiosk, 'check_order_availability', return_value=None), \
             patch.object(kiosk, '_resolve_call_centre_payment', return_value=(self.card, None)), \
             patch.object(kiosk, '_call_centre_discounts', return_value=([{'id': 'discount', 'name': '10%'}], None)), \
             patch.object(kiosk, '_call_centre_discount_supported', return_value=True), \
             patch.object(kiosk, '_call_centre_discount_amount', return_value=100), \
             patch.object(kiosk, '_table_order_by_id', side_effect=[None, confirmed, confirmed]), \
             patch.object(kiosk, '_wait_command', return_value={'state': 'Success'}), \
             patch.object(kiosk, '_close_kiosk_order', return_value=({'state': 'Success'}, None, 200)), \
             patch.object(kiosk, 'iiko_kiosk_post', return_value=Mock(ok=True, json=lambda: {'correlationId': 'c'})) as post:
            response = self.client.post('/call-centre-order', headers={'X-Call-Centre-Key': 'test'}, json={
                'confirm': 'CLIENT_PAID', 'requestId': 'r', 'paymentSum': 900,
                'discountId': 'discount', 'discountSum': 100,
                'items': [{'productId': 'p', 'amount': 1, 'price': 1000}]})
            self.assertEqual(response.status_code, 200, response.json)
            order = post.call_args.args[1]['order']
            self.assertNotIn('tableIds', order)
            self.assertNotIn('comment', order)
            self.assertEqual(order['payments'][0]['paymentTypeId'], 'card')
            self.assertEqual(order['payments'][0]['sum'], 900)
            self.assertEqual(order['discountsInfo']['discounts'][0]['sum'], 100)

    def test_technical_payment_names_are_rejected(self):
        for name in ('Kiosk', 'Analytics'):
            with self.subTest(name=name), patch.dict(os.environ, {'IIKO_KIOSK_ARAI_PAYMENT_TYPE_ID': 'bad'}), \
                 patch.object(kiosk, '_call_centre_payment_types', return_value=([
                     {'id': 'bad', 'name': name, 'paymentTypeKind': 'Card'}], None)):
                payment, error = kiosk._resolve_kiosk_payment('org', 'terminal', 'Arai')
                self.assertIsNone(payment)
                self.assertEqual(error['code'], 'KIOSK_PAYMENT_MAPPING_INVALID')

    def test_paid_kiosk_without_table_preserves_confirmed_payment(self):
        confirmed = {'creationStatus': 'Success', 'order': {'sum': 1000, 'number': 42}}
        with patch.dict(os.environ, {'KIOSK_LIVE_PAYMENTS_ENABLED': 'true', 'IIKO_KIOSK_API_KEY': 'test', 'KIOSK_BRIDGE_ORDER_TOKEN': 'test'}), \
             patch.object(kiosk, '_resolve_kiosk_order_target', return_value=(self.target, None, 200)), \
             patch.object(kiosk, 'check_order_availability', return_value=None), \
             patch.object(kiosk, '_resolve_kiosk_payment', return_value=(self.card, None)), \
             patch.object(kiosk, '_table_order_by_id', side_effect=[None, confirmed, confirmed]), \
             patch.object(kiosk, '_wait_command', return_value={'state': 'Success'}), \
             patch.object(kiosk, '_close_kiosk_order', return_value=({'state': 'Success'}, None, 200)), \
             patch.object(kiosk, '_record_kiosk_order_in_crm', return_value={'ok': True}), \
             patch.object(kiosk, 'iiko_kiosk_post', return_value=Mock(ok=True, json=lambda: {'correlationId': 'c'})) as post:
            response = self.client.post('/kiosk-paid-order', headers={'X-Kiosk-Bridge-Token': 'test'}, json={
                'paymentProcessId': 'p', 'paymentSubStatus': 'CardTransactionSuccess', 'paymentSum': 1000,
                'items': [{'productId': 'p', 'amount': 1}]})
            self.assertEqual(response.status_code, 200, response.json)
            order = post.call_args.args[1]['order']
            self.assertNotIn('tableIds', order)
            self.assertNotIn('comment', order)
            self.assertEqual(order['payments'][0]['sum'], 1000)
            self.assertEqual(order['payments'][0]['paymentTypeId'], 'card')

    def test_readiness_blocks_charge_before_invalid_mapping(self):
        with patch.dict(os.environ, {'KIOSK_LIVE_PAYMENTS_ENABLED': 'true', 'IIKO_KIOSK_API_KEY': 'test', 'KIOSK_BRIDGE_ORDER_TOKEN': 'test'}), \
             patch.object(kiosk, '_resolve_kiosk_order_target', return_value=(self.target, None, 200)), \
             patch.object(kiosk, '_resolve_kiosk_payment', return_value=(None, {'code': 'KIOSK_PAYMENT_MAPPING_INVALID'})):
            self.assertFalse(self.client.get('/kiosk-payment-readiness').json['ready'])


if __name__ == '__main__':
    unittest.main()
