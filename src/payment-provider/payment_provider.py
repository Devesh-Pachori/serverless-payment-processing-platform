import json
import time
import os
import boto3
from botocore.exceptions import ClientError


dynamodb = boto3.resource("dynamodb")

TABLE_NAME = os.environ["PROVIDER_TRANSACTIONS_TABLE"]
table = dynamodb.Table(TABLE_NAME)


def lambda_handler(event, context):

    payment_id = event["payment_id"]
    amount = event["amount"]
    currency = event["currency"]

    print(json.dumps({
        "event": "PROVIDER_REQUEST",
        "payment_id": payment_id,
        "amount": amount,
        "currency": currency,
        "request_id": context.aws_request_id
    }))

    # Check whether this payment was already processed
    existing = table.get_item(
        Key={"payment_id": payment_id},
        ConsistentRead=True
    ).get("Item")

    if existing:

        print(json.dumps({
            "event": "PROVIDER_DUPLICATE",
            "payment_id": payment_id
        }))

        return {
            "statusCode": 200,
            "body": json.dumps({
                "payment_id": payment_id,
                "status": "ALREADY_PROCESSED"
            })
        }

    # Attempt to create the transaction atomically
    try:

        table.put_item(
            Item={
                "payment_id": payment_id,
                "amount": amount,
                "currency": currency,
                "status": "CHARGED",
                "charged_at": int(time.time())
            },
            ConditionExpression="attribute_not_exists(payment_id)"
        )

        print(json.dumps({
            "event": "PROVIDER_CHARGE_SUCCESS",
            "payment_id": payment_id
        }))

        # Controlled timeout experiment.
        # The payment is already persisted as CHARGED.
        # We deliberately wait 35 seconds after the charge.
        if payment_id == os.environ.get("SIMULATE_TIMEOUT_PAYMENT"):

            print(json.dumps({
                "event": "CONTROLLED_TIMEOUT_AFTER_CHARGE",
                "payment_id": payment_id
            }))

            time.sleep(35)

        return {
            "statusCode": 200,
            "body": json.dumps({
                "payment_id": payment_id,
                "status": "CHARGED"
            })
        }

    except ClientError as e:

        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":

            print(json.dumps({
                "event": "PROVIDER_DUPLICATE_RACE",
                "payment_id": payment_id
            }))

            return {
                "statusCode": 200,
                "body": json.dumps({
                    "payment_id": payment_id,
                    "status": "ALREADY_PROCESSED"
                })
            }

        raise
