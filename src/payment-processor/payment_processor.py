import json
import time
import boto3
import os
import uuid
from botocore.exceptions import ClientError


dynamodb = boto3.resource("dynamodb")
lambda_client = boto3.client("lambda")

TABLE_NAME = os.environ["PAYMENTS_TABLE"]
table = dynamodb.Table(TABLE_NAME)

PROCESSING_DELAY_SECONDS = float(
    os.environ.get("PROCESSING_DELAY_SECONDS", "0")
)

PROVIDER_FUNCTION_NAME = os.environ.get(
    "PROVIDER_FUNCTION_NAME",
    "PaymentProviderLambda"
)

def process_record(record):

    if PROCESSING_DELAY_SECONDS > 0:
        time.sleep(PROCESSING_DELAY_SECONDS)

    payment = json.loads(record["body"])
    payment_id = payment["payment_id"]

    lease_owner = str(uuid.uuid4())

    now = int(time.time())
    new_lease_until = now + 60

    print(json.dumps({
        "event": "PAYMENT_CLAIM_ATTEMPT",
        "payment_id": payment_id,
        "now": now,
        "new_lease_until": new_lease_until
    }))

    claimed = False

    # ---------------------------------------------------------
    # STEP 1: Try to claim a brand-new payment
    # ---------------------------------------------------------

    try:

        table.put_item(
            Item={
                "payment_id": payment_id,
                "customer_id": payment["customer_id"],
                "amount": payment["amount"],
                "currency": payment["currency"],
                "status": "IN_PROGRESS",
                "lease_until": new_lease_until,
                "lease_owner": lease_owner
            },
            ConditionExpression="attribute_not_exists(payment_id)"
        )

        print(json.dumps({
            "event": "PAYMENT_CLAIMED_NEW",
            "payment_id": payment_id,
            "lease_until": new_lease_until
        }))

        claimed = True

    except ClientError as e:

        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise

        # -----------------------------------------------------
        # Payment already exists
        # -----------------------------------------------------

        existing = table.get_item(
            Key={"payment_id": payment_id},
            ConsistentRead=True
        ).get("Item")

        if not existing:
            raise Exception(
                f"Payment {payment_id} disappeared after conditional failure"
            )

        # -----------------------------------------------------
        # Validate idempotency payload
        # -----------------------------------------------------

        same_payment = (
            existing.get("customer_id") == payment["customer_id"]
            and existing.get("amount") == payment["amount"]
            and existing.get("currency") == payment["currency"]
        )

        if not same_payment:
                print(json.dumps({
                "event": "IDEMPOTENCY_CONFLICT",
                "payment_id": payment_id
            }))
                return

        existing_status = existing.get("status")
        existing_lease = int(existing.get("lease_until", 0))

        # -----------------------------------------------------
        # Already completed
        # -----------------------------------------------------

        if existing_status == "COMPLETED":

            print(json.dumps({
                "event": "DUPLICATE_PAYMENT",
                "payment_id": payment_id,
                "reason": "ALREADY_COMPLETED"
            }))

            return

        # -----------------------------------------------------
        # Another processor currently owns the payment
        # -----------------------------------------------------

        if existing_status == "IN_PROGRESS" and existing_lease > now:

            print(json.dumps({
                "event": "PAYMENT_ALREADY_IN_PROGRESS",
                "payment_id": payment_id,
                "lease_until": existing_lease,
                "now": now
            }))

            raise Exception(
                f"Payment {payment_id} has an active lease; retry later"
            )

        # -----------------------------------------------------
        # Lease expired - attempt to reclaim
        # -----------------------------------------------------

        if existing_status == "IN_PROGRESS" and existing_lease <= now:

            try:

                table.update_item(
                    Key={"payment_id": payment_id},
                    UpdateExpression="""
                        SET lease_until = :new_lease,
                            lease_owner = :lease_owner
                    """,
                    ConditionExpression="""
                        #status = :in_progress
                        AND lease_until <= :now
                    """,
                    ExpressionAttributeNames={
                        "#status": "status"
                    },
                    ExpressionAttributeValues={
                        ":new_lease": new_lease_until,
                        ":lease_owner": lease_owner,
                        ":in_progress": "IN_PROGRESS",
                        ":now": now
                    }
                )

                print(json.dumps({
                    "event": "PAYMENT_RECLAIMED",
                    "payment_id": payment_id,
                    "old_lease_until": existing_lease,
                    "new_lease_until": new_lease_until
                }))

                claimed = True

            except ClientError as e:

                if e.response["Error"]["Code"] == "ConditionalCheckFailedException":

                    print(json.dumps({
                        "event": "RECLAIM_LOST_RACE",
                        "payment_id": payment_id
                    }))

                    raise Exception(
                        f"Lost lease reclaim race for payment {payment_id}"
                    )

                raise

    # ---------------------------------------------------------
    # STEP 2: Only the lease owner can continue
    # ---------------------------------------------------------

    if not claimed:
        raise Exception(
            f"Payment {payment_id} was not claimed; retry later"
        )

    # ---------------------------------------------------------
    # STEP 3: Controlled failure BEFORE provider call
    # ---------------------------------------------------------

    if payment_id == os.environ.get("FORCE_FAILURE_PAYMENT"):

        print(json.dumps({
            "event": "CONTROLLED_FAILURE",
            "payment_id": payment_id
        }))

        raise Exception("Intentional failure after idempotency claim")

    # ---------------------------------------------------------
    # STEP 4: Call Payment Provider
    # ---------------------------------------------------------

    print(json.dumps({
        "event": "PROCESSING_PAYMENT",
        "payment_id": payment_id
    }))

    provider_response = lambda_client.invoke(
        FunctionName=PROVIDER_FUNCTION_NAME,
        InvocationType="RequestResponse",
        Payload=json.dumps({
            "payment_id": payment_id,
            "amount": payment["amount"],
            "currency": payment["currency"]
        }).encode()
    )

    provider_payload = json.loads(
        provider_response["Payload"].read()
    )

    print(json.dumps({
        "event": "PROVIDER_RESPONSE",
        "payment_id": payment_id,
        "provider_response": provider_payload,
        "function_error": provider_response.get("FunctionError")
    }))

    # ---------------------------------------------------------
    # STEP 5: CRITICAL FIX
    #
    # Invoke API success != Provider Lambda success
    #
    # FunctionError means the provider Lambda itself failed.
    # Do NOT mark the payment as COMPLETED.
    # ---------------------------------------------------------

    if provider_response.get("FunctionError"):

        print(json.dumps({
            "event": "PROVIDER_LAMBDA_ERROR",
            "payment_id": payment_id,
            "provider_error": provider_payload
        }))

        raise Exception(
            f"Payment provider Lambda failed: {provider_payload}"
        )

    # ---------------------------------------------------------
    # STEP 6: Validate provider response
    # ---------------------------------------------------------

    provider_status_code = provider_payload.get("statusCode")

    provider_body = json.loads(
        provider_payload.get("body") or "{}"
    )

    provider_business_status = provider_body.get("status")

    print(json.dumps({
        "event": "PROVIDER_RESULT_VALIDATED",
        "payment_id": payment_id,
        "status_code": provider_status_code,
        "provider_status": provider_business_status
    }))

    # Provider considers both of these successful:
    #
    # CHARGED
    # ALREADY_PROCESSED
    #
    # ALREADY_PROCESSED is important during retries because
    # the provider may have charged the payment during the
    # previous attempt.

    if (
        provider_status_code != 200
        or provider_business_status not in (
            "CHARGED",
            "ALREADY_PROCESSED"
        )
    ):

        print(json.dumps({
            "event": "PROVIDER_BUSINESS_FAILURE",
            "payment_id": payment_id,
            "provider_response": provider_payload
        }))

        raise Exception(
            f"Payment provider did not successfully process payment: "
            f"{provider_payload}"
        )

    # ---------------------------------------------------------
    # STEP 7: Controlled failure AFTER provider success
    # ---------------------------------------------------------

    if payment_id == os.environ.get("FAIL_AFTER_PROVIDER"):

        print(json.dumps({
            "event": "CONTROLLED_FAILURE_AFTER_PROVIDER",
            "payment_id": payment_id
        }))

        raise Exception(
            "Intentional failure after provider success"
        )

    # ---------------------------------------------------------
    # STEP 8: Mark payment COMPLETED
    #
    # Fencing:
    # Only the processor that owns the current lease can
    # complete the payment.
    # ---------------------------------------------------------

    table.update_item(
        Key={"payment_id": payment_id},
        UpdateExpression="SET #status = :status",
        ConditionExpression="""
            #status = :in_progress
            AND lease_owner = :lease_owner
        """,
        ExpressionAttributeNames={
            "#status": "status"
        },
        ExpressionAttributeValues={
            ":status": "COMPLETED",
            ":in_progress": "IN_PROGRESS",
            ":lease_owner": lease_owner
        }
    )

    print(json.dumps({
        "event": "PAYMENT_COMPLETED",
        "payment_id": payment_id,
        "provider_status": provider_business_status
    }))

def lambda_handler(event, context):

    print(json.dumps({
        "event": "BATCH_RECEIVED",
        "record_count": len(event.get("Records", [])),
        "request_id": context.aws_request_id
    }))

    batch_item_failures = []

    for record in event.get("Records", []):
        try:
            process_record(record)

        except Exception as e:
            print(json.dumps({
                "event": "PAYMENT_RECORD_FAILED",
                "message_id": record.get("messageId"),
                "error": str(e)
            }))

            batch_item_failures.append({
                "itemIdentifier": record["messageId"]
            })

    return {
        "batchItemFailures": batch_item_failures
    }
