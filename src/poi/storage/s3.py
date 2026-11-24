from __future__ import annotations

from typing import Any

import boto3
from botocore.exceptions import ClientError


class S3Store:
    def __init__(self, bucket: str, prefix: str = "raw/", client: Any = None) -> None:
        self.bucket = bucket
        self.prefix = prefix
        self.client = client or boto3.client("s3")

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self.prefix + key, Body=data)

    def get(self, key: str) -> bytes:
        obj = self.client.get_object(Bucket=self.bucket, Key=self.prefix + key)
        body: bytes = obj["Body"].read()
        return body

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self.prefix + key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True
