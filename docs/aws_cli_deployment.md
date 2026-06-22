# AWS CLI Deployment for Batch Fargate Evaluators

This guide creates the minimal AWS resources for running Vis Arena evaluation
jobs on AWS Batch with Fargate. It assumes the FastAPI backend already runs on
EC2 and uses S3 for storage.

## Variables

```bash
export AWS_REGION=us-east-1
export AWS_ACCOUNT_ID=123456789012
export PROJECT_NAME=vis-arena
export VPC_ID=vpc-xxxxxxxx
export SUBNET_IDS=subnet-aaaaaaa,subnet-bbbbbbb
export BATCH_SECURITY_GROUP_ID=sg-xxxxxxxx
export S3_BUCKET=vis-arena-prod
export ECR_REPO=${PROJECT_NAME}-evaluator-runner
export BATCH_QUEUE_NAME=${PROJECT_NAME}-evaluator-queue
export BATCH_JOB_DEFINITION_NAME=${PROJECT_NAME}-evaluator-runner
```

## ECR Repository

```bash
aws ecr create-repository \
  --region "$AWS_REGION" \
  --repository-name "$ECR_REPO"

aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin \
    "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
```

## Build and Push Runner Image

Run from the repository root:

```bash
docker build \
  -f infra/evaluator/Dockerfile \
  -t "$ECR_REPO:latest" \
  .

docker tag "$ECR_REPO:latest" \
  "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_REPO:latest"

docker push \
  "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_REPO:latest"
```

## IAM Roles and Policies

Create the Batch service role:

```bash
aws iam create-role \
  --role-name "${PROJECT_NAME}-batch-service-role" \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "batch.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'

aws iam attach-role-policy \
  --role-name "${PROJECT_NAME}-batch-service-role" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole
```

Create the ECS task execution role:

```bash
aws iam create-role \
  --role-name "${PROJECT_NAME}-batch-execution-role" \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "ecs-tasks.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'

aws iam attach-role-policy \
  --role-name "${PROJECT_NAME}-batch-execution-role" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
```

Create the runner task role with S3 access limited to the arena bucket:

```bash
aws iam create-role \
  --role-name "${PROJECT_NAME}-runner-task-role" \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "ecs-tasks.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'

cat >/tmp/${PROJECT_NAME}-runner-s3-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "s3:GetObject",
      "s3:PutObject",
      "s3:ListBucket"
    ],
    "Resource": [
      "arn:aws:s3:::$S3_BUCKET",
      "arn:aws:s3:::$S3_BUCKET/*"
    ]
  }]
}
EOF

aws iam put-role-policy \
  --role-name "${PROJECT_NAME}-runner-task-role" \
  --policy-name "${PROJECT_NAME}-runner-s3" \
  --policy-document "file:///tmp/${PROJECT_NAME}-runner-s3-policy.json"
```

Add permissions to the backend EC2 instance role:

```bash
cat >/tmp/${PROJECT_NAME}-backend-batch-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "batch:SubmitJob",
      "batch:DescribeJobs",
      "batch:TerminateJob"
    ],
    "Resource": "*"
  }]
}
EOF

aws iam put-role-policy \
  --role-name "<BACKEND_EC2_ROLE_NAME>" \
  --policy-name "${PROJECT_NAME}-submit-batch" \
  --policy-document "file:///tmp/${PROJECT_NAME}-backend-batch-policy.json"
```

## Batch Compute Environment

Public subnet v1:

```bash
aws batch create-compute-environment \
  --region "$AWS_REGION" \
  --compute-environment-name "${PROJECT_NAME}-fargate" \
  --type MANAGED \
  --state ENABLED \
  --service-role "arn:aws:iam::$AWS_ACCOUNT_ID:role/${PROJECT_NAME}-batch-service-role" \
  --compute-resources "type=FARGATE,maxvCpus=8,subnets=[$SUBNET_IDS],securityGroupIds=[$BATCH_SECURITY_GROUP_ID]"
```

For stricter networking, use private subnets plus NAT or VPC endpoints for ECR,
S3, CloudWatch Logs, and backend connectivity.

## Batch Job Queue

```bash
aws batch create-job-queue \
  --region "$AWS_REGION" \
  --job-queue-name "$BATCH_QUEUE_NAME" \
  --state ENABLED \
  --priority 1 \
  --compute-environment-order order=1,computeEnvironment="${PROJECT_NAME}-fargate"
```

## Batch Job Definition

```bash
aws batch register-job-definition \
  --region "$AWS_REGION" \
  --job-definition-name "$BATCH_JOB_DEFINITION_NAME" \
  --type container \
  --platform-capabilities FARGATE \
  --container-properties "{
    \"image\": \"$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_REPO:latest\",
    \"executionRoleArn\": \"arn:aws:iam::$AWS_ACCOUNT_ID:role/${PROJECT_NAME}-batch-execution-role\",
    \"jobRoleArn\": \"arn:aws:iam::$AWS_ACCOUNT_ID:role/${PROJECT_NAME}-runner-task-role\",
    \"resourceRequirements\": [
      {\"type\": \"VCPU\", \"value\": \"2\"},
      {\"type\": \"MEMORY\", \"value\": \"4096\"}
    ],
    \"networkConfiguration\": {\"assignPublicIp\": \"ENABLED\"},
    \"logConfiguration\": {
      \"logDriver\": \"awslogs\",
      \"options\": {
        \"awslogs-group\": \"/aws/batch/job\",
        \"awslogs-region\": \"$AWS_REGION\",
        \"awslogs-stream-prefix\": \"$PROJECT_NAME\"
      }
    }
  }" \
  --timeout attemptDurationSeconds=2400
```

## Backend EC2 Environment

Configure the backend process:

```bash
export VIS_ARENA_EXECUTOR_MODE=aws_batch_fargate
export VIS_ARENA_AWS_BATCH_REGION="$AWS_REGION"
export VIS_ARENA_AWS_BATCH_JOB_QUEUE="$BATCH_QUEUE_NAME"
export VIS_ARENA_AWS_BATCH_JOB_DEFINITION="$BATCH_JOB_DEFINITION_NAME"
export VIS_ARENA_AWS_BATCH_RUNNER_IMAGE="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_REPO:latest"
export VIS_ARENA_AWS_BATCH_JOB_VCPUS=2
export VIS_ARENA_AWS_BATCH_JOB_MEMORY=4096
export VIS_ARENA_AWS_BATCH_JOB_TIMEOUT_SECONDS=2400
export VIS_ARENA_PUBLIC_BASE_URL=https://arena.example.com
export VIS_ARENA_S3_BUCKET="$S3_BUCKET"
export VIS_ARENA_S3_REGION="$AWS_REGION"
```

Restart the backend after changing the environment.

## Cloud Smoke Test

1. Upload and finalize one known-good submission through the CLI or frontend.
2. Confirm a Batch job was submitted:

```bash
aws batch list-jobs \
  --region "$AWS_REGION" \
  --job-queue "$BATCH_QUEUE_NAME" \
  --job-status RUNNING
```

3. Watch CloudWatch logs under `/aws/batch/job`.
4. Verify the backend job changes to `succeeded` or `failed`.
5. Verify S3 contains `jobs/<job-id>/generation`, `jobs/<job-id>/evaluation`,
   previews, reports, and logs.
6. Open the frontend preview and leaderboard entry.

## Cleanup for Development Environments

```bash
aws batch update-job-queue \
  --region "$AWS_REGION" \
  --job-queue "$BATCH_QUEUE_NAME" \
  --state DISABLED

aws batch delete-job-queue \
  --region "$AWS_REGION" \
  --job-queue "$BATCH_QUEUE_NAME"

aws batch update-compute-environment \
  --region "$AWS_REGION" \
  --compute-environment "${PROJECT_NAME}-fargate" \
  --state DISABLED

aws batch delete-compute-environment \
  --region "$AWS_REGION" \
  --compute-environment "${PROJECT_NAME}-fargate"

aws ecr delete-repository \
  --region "$AWS_REGION" \
  --repository-name "$ECR_REPO" \
  --force

aws iam detach-role-policy \
  --role-name "${PROJECT_NAME}-batch-service-role" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole

aws iam delete-role-policy \
  --role-name "${PROJECT_NAME}-runner-task-role" \
  --policy-name "${PROJECT_NAME}-runner-s3"

aws iam delete-role --role-name "${PROJECT_NAME}-batch-service-role"
aws iam delete-role --role-name "${PROJECT_NAME}-batch-execution-role"
aws iam delete-role --role-name "${PROJECT_NAME}-runner-task-role"
```
