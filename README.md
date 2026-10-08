```text
# Serverless Payment Processing Platform


A production-style AWS serverless payment processing platform demonstrating
asynchronous payment processing, idempotency, failure recovery, retry/DLQ
handling, partial batch failure, and Infrastructure as Code using AWS SAM.

```text

## Project Architecture

```text

                         Client
                           |
                           | POST /payments
                           v
                 +----------------------+
                 | API Gateway HTTP API |
                 +----------+-----------+
                            |
                            v
                 +----------------------+
                 | PaymentApiLambda     |
                 +----------+-----------+
                            |
                            | SendMessage
                            v
                 +----------------------+
                 | SQS PaymentQueue     |
                 +----------+-----------+
                            |
                     Event Source Mapping
                            |
                            v
                 +----------------------+
                 | PaymentProcessor     |
                 | Lambda               |
                 +----+-------------+---+
                      |             |
                      |             | Invoke
                      |             v
                      |      +----------------------+
                      |      | PaymentProvider      |
                      |      | Lambda               |
                      |      +----------+-----------+
                      |                 |
                      v                 v
              +---------------+   +---------------------------+
              | SamPayments   |   | ProviderTransactions      |
              | DynamoDB      |   | DynamoDB                  |
              +---------------+   +---------------------------+


```text

                      PaymentQueue
                           |
                    Retry exhaustion
                           |
                           v
                    +--------------+
                    | PaymentDLQ   |
                    +--------------+


Problem Statement

Payment processing should not depend on a synchronous request completing
every downstream operation successfully.

This project uses an asynchronous architecture where the API accepts a
payment request and places it onto Amazon SQS. A Lambda-based processor
then handles the payment and invokes a simulated payment provider.

The design focuses on reliability, idempotency, retry behavior, failure
recovery, and controlled processing of payment messages.


Payment Processing Flow

1. Client sends POST /payments.
2. API Gateway invokes PaymentApiLambda.
3. PaymentApiLambda validates the request and sends the payment message
   to PaymentQueue.
4. The API returns HTTP 202 Accepted.
5. SQS invokes PaymentProcessorLambda.
6. The processor claims the payment using a DynamoDB-based lease.
7. The processor invokes PaymentProviderLambda.
8. The provider records the transaction in
   SamPaymentProviderTransactions.
9. The processor validates the provider response.
10. The payment is marked COMPLETED in SamPayments.


The API is intentionally asynchronous. HTTP 202 Accepted means the
request was accepted for processing; it does not mean the payment has
already completed.


AWS Services

- Amazon API Gateway HTTP API
- AWS Lambda
- Amazon SQS
- Amazon SQS Dead Letter Queue
- Amazon DynamoDB
- AWS CloudFormation through AWS SAM
- Amazon CloudWatch Logs


Reliability and Failure Handling

Idempotency

The payment ID is used as the idempotency key.

A new payment is created using a conditional DynamoDB write:

attribute_not_exists(payment_id)


If the payment already exists, the processor compares the incoming
customer, amount, and currency values with the stored payment.


The application handles:

- Duplicate completed payment
- Idempotency payload conflict
- Payment currently being processed
- Expired processing lease

A duplicate completed payment does not invoke the provider again.

Lease-Based Processing

Payments are initially stored with:

status = IN_PROGRESS
lease_owner
lease_until

The lease prevents multiple processors from simultaneously owning the
same payment.

If a processor fails and the lease expires, another processing attempt
can reclaim the payment.

The final COMPLETED update is protected using the current
lease_owner, providing fencing against stale processors.


Retry and Dead Letter Queue

The SQS queue is configured with:
maxReceiveCount = 3

Messages that continue to fail after the configured receive count are
sent to the payment DLQ.

The project also demonstrates DLQ recovery by moving failed messages
back to the processing queue after the underlying failure condition
has been corrected.

Partial Batch Failure

The processor Lambda uses:
FunctionResponseTypes:
  - ReportBatchItemFailures

with an SQS batch size of 5.
When one record in a batch fails, the processor returns the failed
message ID through batchItemFailures rather than treating the entire
batch as failed.

This allows successful records to remain processed while the failed
record can be retried independently.

Provider Failure Handling

The processor does not assume that a successful Lambda invocation means
the provider operation succeeded.

It explicitly checks:
- Lambda FunctionError
- Provider HTTP status
- Provider business status

The provider statuses CHARGED and ALREADY_PROCESSED are treated as
successful outcomes.

Controlled Failure Injection

The application supports controlled failure injection through environment
variables for testing retry and recovery behavior.

Examples include:

FORCE_FAILURE_PAYMENT
FAIL_AFTER_PROVIDER

These are disabled in the deployed normal configuration.

Infrastructure as Code

The AWS resources are defined using AWS SAM in:
infrastructure/template.yaml

The SAM template defines:
- DynamoDB tables
- SQS queue
- SQS DLQ
- Lambda functions
- API Gateway HTTP API
- SQS event source mapping
- Lambda environment variables
- IAM permissions
- Retry/DLQ configuration


The application can therefore be deployed using Infrastructure as Code
rather than manually creating the AWS resources.

Build

$env:PYTHONNOUSERSITE = "1"
sam build --template-file infrastructure/template.yaml


Validate

sam validate --template-file infrastructure/template.yaml


Deploy

sam deploy `
  --template-file .aws-sam\\build\\template.yaml `
  --stack-name sam-payment-processing-platform `
  --region ap-south-1 `
  --capabilities CAPABILITY_IAM `
  --resolve-s3


Project Structure


serverless-payment-processing-platform/

|

+-- infrastructure/

|   +-- template.yaml

|

+-- src/

|   +-- payment-api/

|   |   +-- payment_api.py

|   |

|   +-- payment-processor/

|   |   +-- payment_processor.py

|   |

|   +-- payment-provider/

|       +-- payment_provider.py

|
```text
+-- .gitignore
+-- README.md
```


Testing and Validation

The project was tested against several failure and reliability scenarios.
Successful End-to-End Payment

A final production-configuration test was performed using:
PAY-FINAL-001

The API returned:
Payment accepted

The payment was subsequently verified in DynamoDB as:
status = COMPLETED

The corresponding provider transaction was verified as:
status = CHARGED

Duplicate Payment

A duplicate payment ID was submitted and the processor detected the
existing completed payment without invoking the provider again.


Idempotency Conflict
The same payment ID was submitted with different payment data.

The processor detected the conflict and logged:
IDEMPOTENCY_CONFLICT

The original payment state remained unchanged.


Retry and DLQ

Controlled failures were used to demonstrate:

PaymentQueue
   |
   | 
   | repeated processing failure
   | 
   v
PaymentDLQ


After the failure condition was removed, the failed payment was
redriven and successfully completed.


Partial Batch Failure


A batch of five payment messages was processed with one intentionally
failing payment.

The failed message was retried independently and eventually reached
the DLQ after repeated failures.

The message was subsequently redriven after the failure condition was
removed and the payment completed successfully.


Concurrency and Backlog Testing

The project was also tested with controlled Lambda concurrency and
processing delays.

The experiments demonstrated:

- Reserved concurrency limits concurrent Lambda executions.
- SQS absorbs temporary workload spikes.
- Processing throughput depends on concurrency and processing duration
- Increasing Lambda concurrency must consider downstream provider capacity.
- Queue depth and oldest-message age are useful indicators of backlog pressure.


These experiments were performed as engineering tests; the final
application configuration does not retain the artificial processing
delay or restrictive lab concurrency setting.


Key Design Decisions

Why SQS?

SQS decouples API request handling from payment processing and provides
a durable buffer between the API and processor.

Why asynchronous processing?

Payment processing can involve downstream dependencies and retries.
Returning 202 Accepted allows the API to remain responsive while the
payment is processed asynchronously.

Why DynamoDB?

DynamoDB provides a serverless persistence layer for payment state and
supports conditional writes required for idempotency and lease
management.

Why a DLQ?

A DLQ prevents repeatedly failing messages from blocking normal
processing and provides a controlled recovery path for poison or
persistent failure messages.

Why partial batch failure?

When processing SQS batches, only failed records should require retry.
ReportBatchItemFailures prevents successful records from unnecessarily
being processed again.

Key Engineering Concepts Demonstrated

- AWS Lambda
- Amazon SQS
- Dead Letter Queues
- DynamoDB conditional writes
- Idempotency
- Lease-based processing
- Fencing
- Partial batch failure
- Asynchronous APIs
- Retry and recovery
- Lambda concurrency
- SQS backlog management
- Downstream dependency protection
- Least-privilege IAM
- AWS SAM / CloudFormation
- Python
- Git and GitHub

Future Improvements

Potential production enhancements include:

- Automated CI/CD using GitHub Actions
- CloudWatch dashboards and alarms
- AWS X-Ray distributed tracing
- Automated unit and integration test suites
- Multi-environment deployment strategy
- Terraform-based infrastructure for a separate implementation
- Stronger authentication and authorization for the public API
