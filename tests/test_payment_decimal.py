import json
import unittest
from decimal import Decimal


class TestPaymentDecimalHandling(unittest.TestCase):

    def test_decimal_amount_is_parsed_as_decimal(self):
        message_body = '''
        {
            "payment_id": "payment-test-001",
            "customer_id": "customer-test-001",
            "amount": 125.50,
            "currency": "INR"
        }
        '''

        payment = json.loads(
            message_body,
            parse_float=Decimal
        )

        self.assertIsInstance(payment["amount"], Decimal)
        self.assertEqual(payment["amount"], Decimal("125.50"))


if __name__ == "__main__":
    unittest.main()
