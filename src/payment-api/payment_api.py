import json
import os
import boto3

sqs = boto3.client("sqs")

QUEUE_URL = os.environ["PAYMENT_QUEUE_URL"]


def lambda_handler(event, context):

    body = json.loads(event.get("body") or "{}")

    payment_id = body.get("payment_id")
    customer_id = body.get("customer_id")
    amount = body.get("amount")
    currency = body.get("currency")

    if not all([payment_id, customer_id, amount, currency]):
        return {
            "statusCode": 400,
            "body": json.dumps({
                "message": "Missing required payment fields"
            })
        }

    payment = {
        "payment_id": payment_id,
        "customer_id": customer_id,
        "amount": amount,
        "currency": currency
    }

    response = sqs.send_message(
        QueueUrl=QUEUE_URL,
        MessageBody=json.dumps(payment)
    )

    print(json.dumps({
        "event": "PAYMENT_ACCEPTED",
        "payment_id": payment_id,
        "sqs_message_id": response["MessageId"],
        "request_id": context.aws_request_id
    }))

    return {
        "statusCode": 202,
        "body": json.dumps({
            "message": "Payment accepted",
            "payment_id": payment_id,
            "request_id": context.aws_request_id
        })
    }
