"""
Migration Readiness Evaluator
------------------------------
Lambda handler behind an IAM-authenticated API Gateway endpoint.

Scoring model (questionnaire-based):
  - The DBA answers 8 fixed questions (Q01-Q08). Each answer is
    "A", "B", or "C".
  - Every question uses the same point scale: A = 0, B = 5, C = 10
    risk points -- no per-question weighting.
  - totalRiskPoints = sum of all 8 answers (range 0-80).
  - readinessScore = 100 - totalRiskPoints (range 20-100).
  - Risk level thresholds:
        readinessScore 80-100 -> LOW    -> low-risk/    -> no email
        readinessScore 60-79  -> MEDIUM -> medium-risk/ -> no email
        readinessScore < 60   -> HIGH   -> high-risk/   -> email via
                                            EventBridge -> SNS

What this function does, in order:
  1. Parses and validates the incoming answers.
  2. Scores each answer, sums the risk points, derives readinessScore
     and risk level, and builds human-readable findings for any
     answer that wasn't "A".
  3. Writes the DBA's original submitted answers *and* the calculated
     assessment together to S3, under a risk-tier prefix. That S3
     write is what EventBridge + SNS react to downstream -- this
     function does not talk to SNS or EventBridge directly.
  4. Returns the assessment to API Gateway -> Postman synchronously,
     including confirmation that the record was stored in S3.

No external dependencies -- only the boto3 SDK that ships with the
Lambda Python runtime.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")

REPORTS_BUCKET = os.environ["REPORTS_BUCKET"]  # e.g. oracle-migration-reports-<accountid>

# ---------------------------------------------------------------------------
# The questionnaire
# ---------------------------------------------------------------------------
# Every question uses the same A/B/C -> 0/5/10 point scale. This dict
# is the single source of truth for field names, question text, and
# what each option means -- used both to validate answers and to
# generate the findings text in the response.

POINTS = {"A": 0, "B": 5, "C": 10}

QUESTIONS = [
    {
        "field": "oracleVersion",
        "id": "Q01",
        "text": "Oracle database version",
        "options": {"A": "19c or newer", "B": "12c or 18c", "C": "11g or older"},
    },
    {
        "field": "databaseSize",
        "id": "Q02",
        "text": "Approximate database size",
        "options": {"A": "100 GB or less", "B": "101-500 GB", "C": "More than 500 GB"},
    },
    {
        "field": "downtimeTolerance",
        "id": "Q03",
        "text": "Acceptable migration downtime",
        "options": {"A": "More than 4 hours", "B": "1-4 hours", "C": "Less than 1 hour"},
    },
    {
        "field": "architecture",
        "id": "Q04",
        "text": "Current database architecture",
        "options": {"A": "Single instance", "B": "Data Guard", "C": "RAC or RAC with Data Guard"},
    },
    {
        "field": "specialFeatures",
        "id": "Q05",
        "text": "Use of special Oracle features",
        "options": {
            "A": "None or minimal",
            "B": "Some LOBs or materialized views",
            "C": "Heavy LOBs, Spatial, or advanced features",
        },
    },
    {
        "field": "externalDependencies",
        "id": "Q06",
        "text": "External dependencies",
        "options": {"A": "Few and documented", "B": "Several but documented", "C": "Many or undocumented"},
    },
    {
        "field": "sensitiveDataStatus",
        "id": "Q07",
        "text": "Sensitive-data status",
        "options": {
            "A": "No sensitive data",
            "B": "Sensitive data is encrypted",
            "C": "Sensitive data is not encrypted",
        },
    },
    {
        "field": "targetReadiness",
        "id": "Q08",
        "text": "AWS target readiness",
        "options": {"A": "Selected and tested", "B": "Selected but not tested", "C": "Target not selected"},
    },
]

QUESTION_FIELDS = [q["field"] for q in QUESTIONS]

RISK_LEVEL_LOW_MIN = 80     # readinessScore >= this  -> LOW
RISK_LEVEL_MEDIUM_MIN = 60  # readinessScore >= this and < LOW min -> MEDIUM
                             # readinessScore < this -> HIGH


class ValidationError(Exception):
    """Raised when the request body fails basic shape/value checks.

    Note: API Gateway request validation (JSON Schema model) should
    catch most of this before it ever reaches Lambda -- this is a
    defense-in-depth backstop, not the primary control.
    """


def validate_payload(body: dict) -> None:
    if "migrationId" not in body or not isinstance(body["migrationId"], str) or not body["migrationId"]:
        raise ValidationError("migrationId is required and must be a non-empty string")

    answers = body.get("answers")
    if not isinstance(answers, dict):
        raise ValidationError("answers must be an object with one A/B/C value per question")

    missing = [f for f in QUESTION_FIELDS if f not in answers]
    if missing:
        raise ValidationError(f"Missing answer(s) for: {', '.join(missing)}")

    invalid = [f for f in QUESTION_FIELDS if answers[f] not in POINTS]
    if invalid:
        raise ValidationError(f"Answer(s) must be 'A', 'B', or 'C': {', '.join(invalid)}")


def evaluate_readiness(answers: dict) -> dict:
    """Sums risk points across all 8 answers, derives the readiness
    score and risk level, and builds findings for every answer that
    wasn't the safest option ('A')."""

    total_risk_points = 0
    findings = []

    for question in QUESTIONS:
        choice = answers[question["field"]]
        points = POINTS[choice]
        total_risk_points += points

        if choice != "A":
            findings.append(
                f"{question['id']} {question['text']}: {question['options'][choice]} "
                f"(+{points} risk points)"
            )

    readiness_score = 100 - total_risk_points

    if readiness_score >= RISK_LEVEL_LOW_MIN:
        risk_level = "LOW"
        next_action = "Proceed with standard migration runbook"
    elif readiness_score >= RISK_LEVEL_MEDIUM_MIN:
        risk_level = "MEDIUM"
        next_action = "Recommended: review with migration architect before scheduling"
    else:
        risk_level = "HIGH"
        next_action = "Cloud migration architect review required"

    if not findings:
        findings.append("No risk factors identified -- all answers were the lowest-risk option")

    return {
        "totalRiskPoints": total_risk_points,
        "readinessScore": readiness_score,
        "riskLevel": risk_level,
        "findings": findings,
        "nextAction": next_action,
    }


def risk_prefix(risk_level: str) -> str:
    return {"LOW": "low-risk", "MEDIUM": "medium-risk", "HIGH": "high-risk"}[risk_level]


def save_report(migration_id: str, submitted_answers: dict, assessment: dict) -> str:
    """Writes one JSON object to S3 containing BOTH the DBA's original
    submitted answers and the calculated assessment -- an audit trail
    of what was asked and what was decided. This PutObject is the
    event EventBridge listens for -- everything downstream
    (EventBridge rule, SNS, architect email) is triggered by this
    write, not by the Lambda return value."""

    key = f"{risk_prefix(assessment['riskLevel'])}/{migration_id}.json"

    record = {
        "migrationId": migration_id,
        "submittedAnswers": submitted_answers,
        "assessment": assessment,
    }

    s3.put_object(
        Bucket=REPORTS_BUCKET,
        Key=key,
        Body=json.dumps(record, indent=2).encode("utf-8"),
        ContentType="application/json",
        ServerSideEncryption="AES256",
    )
    logger.info("Saved report to s3://%s/%s", REPORTS_BUCKET, key)
    return key


def handler(event, context):
    request_id = getattr(context, "aws_request_id", str(uuid.uuid4()))
    logger.info("Processing request %s", request_id)

    try:
        raw_body = event.get("body") or "{}"
        body = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError):
        return _response(400, {"error": "Request body must be valid JSON"})

    try:
        validate_payload(body)
    except ValidationError as exc:
        return _response(400, {"error": str(exc)})

    migration_id = body["migrationId"]
    answers = body["answers"]
    result = evaluate_readiness(answers)
    assessed_at = datetime.now(timezone.utc).isoformat()

    # The assessment as it will be stored in S3 (part of the audit record).
    assessment = {
        "totalRiskPoints": result["totalRiskPoints"],
        "readinessScore": result["readinessScore"],
        "riskLevel": result["riskLevel"],
        "findings": result["findings"],
        "nextAction": result["nextAction"],
        "assessedAt": assessed_at,
    }

    try:
        s3_key = save_report(migration_id, answers, assessment)
    except ClientError:
        logger.exception("Failed to write report to S3")
        return _response(502, {
            "migrationId": migration_id,
            "error": "Assessment computed but could not be persisted",
            "storage": {"status": "FAILED"},
        })

    # The DBA's original answers are not echoed back -- they were just
    # submitted, so there's nothing new to repeat. The response is the
    # assessment plus explicit confirmation that it was stored.
    response_payload = {
        "migrationId": migration_id,
        "readinessScore": assessment["readinessScore"],
        "riskLevel": assessment["riskLevel"],
        "findings": assessment["findings"],
        "nextAction": assessment["nextAction"],
        "storage": {
            "status": "STORED",
            "bucket": REPORTS_BUCKET,
            "key": s3_key,
            "message": "Assessment stored successfully.",
        },
    }

    return _response(200, response_payload)


def _response(status_code: int, payload: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }
