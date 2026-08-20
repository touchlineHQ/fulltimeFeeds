import os
import sys
from pathlib import Path
import boto3
from botocore.config import Config

def sync_to_r2():
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    bucket_name = os.environ.get("R2_BUCKET_NAME")

    if not account_id or not bucket_name:
        print("Missing R2 environment variables.")
        sys.exit(1)

    # Disable payload signing to fix R2 SignatureDoesNotMatch errors on subsequent files
    s3 = boto3.client(
        service_name="s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            s3={"payload_signing_enabled": False}
        )
    )

    directories = ["calendars", "feeds"]
    total_uploaded = 0
    total_failed = 0

    for directory in directories:
        dir_path = Path(directory)
        if not dir_path.exists():
            print(f"Directory '{directory}' does not exist, skipping...")
            continue

        print(f"\n--- Syncing directory: {directory} to R2 ---")

        for file_path in dir_path.rglob("*"):
            if file_path.is_file():
                r2_key = file_path.as_posix()

                # Set explicit Content-Type headers for R2
                extra_args = {}
                if file_path.suffix == ".ics":
                    extra_args["ContentType"] = "text/calendar; charset=utf-8"
                elif file_path.suffix == ".json":
                    extra_args["ContentType"] = "application/json"

                try:
                    s3.upload_file(
                        Filename=str(file_path),
                        Bucket=bucket_name,
                        Key=r2_key,
                        ExtraArgs=extra_args if extra_args else None
                    )
                    total_uploaded += 1
                    print(f" Uploaded: {r2_key}")
                except Exception as e:
                    total_failed += 1
                    print(f" ❌ Failed to upload {r2_key}: {e}")

    print(f"\nSync Complete! Successfully uploaded: {total_uploaded} files. Failures: {total_failed}")

if __name__ == "__main__":
    sync_to_r2()
