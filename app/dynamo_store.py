"""DynamoDB journey storage for serverless hosting. Writes are serialised with a lease lock, like SQLite's BEGIN IMMEDIATE."""
import json
import time
import uuid
from contextlib import contextmanager

LOCK_ID = "lock#journeys"
LEASE_SECONDS = 20


class DynamoStore:
    def __init__(self, table_name, client=None):
        if client is None:
            import boto3
            client = boto3.client("dynamodb")
        self.table, self.db = table_name, client

    @contextmanager
    def connection(self):
        yield self

    @contextmanager
    def write(self):
        owner = uuid.uuid4().hex
        deadline = time.time() + 30
        while True:
            now = time.time()
            try:
                self.db.put_item(TableName=self.table, Item={"id": {"S": LOCK_ID}, "owner": {"S": owner}, "expires": {"N": str(now + LEASE_SECONDS)}},
                                 ConditionExpression="attribute_not_exists(id) OR expires < :now", ExpressionAttributeValues={":now": {"N": str(now)}})
                break
            except self.db.exceptions.ConditionalCheckFailedException:
                if now > deadline:
                    raise TimeoutError("Journey store is busy")
                time.sleep(0.05)
        try:
            yield self
        finally:
            try:
                self.db.delete_item(TableName=self.table, Key={"id": {"S": LOCK_ID}}, ConditionExpression="#o = :owner",
                                    ExpressionAttributeNames={"#o": "owner"}, ExpressionAttributeValues={":owner": {"S": owner}})
            except self.db.exceptions.ConditionalCheckFailedException:
                pass  # Lease expired and was taken over; nothing to release.

    def put(self, db, journey):
        self.db.put_item(TableName=self.table, Item={"id": {"S": journey["id"]}, "kind": {"S": "journey"},
                                                     "created": {"S": journey["created_at"]}, "payload": {"S": json.dumps(journey)}})
        self.db.put_item(TableName=self.table, Item={"id": {"S": "event#" + journey["event_id"]}, "journey": {"S": journey["id"]}})

    def get(self, db, journey_id):
        item = self.db.get_item(TableName=self.table, Key={"id": {"S": journey_id}}, ConsistentRead=True).get("Item")
        return json.loads(item["payload"]["S"]) if item and "payload" in item else None

    def find_event(self, db, event_id):
        item = self.db.get_item(TableName=self.table, Key={"id": {"S": "event#" + event_id}}, ConsistentRead=True).get("Item")
        return item["journey"]["S"] if item else None

    def all(self):
        items, start = [], None
        while True:
            kwargs = {"TableName": self.table, "ConsistentRead": True, "FilterExpression": "kind = :j", "ExpressionAttributeValues": {":j": {"S": "journey"}}}
            if start:
                kwargs["ExclusiveStartKey"] = start
            page = self.db.scan(**kwargs)
            items += page.get("Items", [])
            start = page.get("LastEvaluatedKey")
            if not start:
                break
        journeys = [json.loads(i["payload"]["S"]) for i in items]
        return sorted(journeys, key=lambda j: j["created_at"], reverse=True)
