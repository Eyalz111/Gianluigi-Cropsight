"""
Upload secrets from .env to GCP Secret Manager.

Reads your local .env file and creates each secret in Google Cloud
Secret Manager, so Cloud Run can access them at startup.

Prerequisites:
    1. gcloud CLI installed and authenticated (gcloud init)
    2. Secret Manager API enabled
    3. .env file exists in the project root

Usage:
    cd C:\\Users\\nogas\\Desktop\\gianluigi
    python scripts/upload_secrets.py

Safe to re-run: existing secrets get a new version.
"""

import subprocess
import sys
import os

# The 25 secrets that cloudbuild.yaml expects
SECRETS = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "SUPABASE_URL",
    "SUPABASE_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_GROUP_CHAT_ID",
    "TELEGRAM_EYAL_CHAT_ID",
    "EYAL_TELEGRAM_ID",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_REFRESH_TOKEN",
    "EYAL_EMAIL",
    "ROYE_EMAIL",
    "PAOLO_EMAIL",
    "YORAM_EMAIL",
    "CROPSIGHT_OPS_FOLDER_ID",
    "RAW_TRANSCRIPTS_FOLDER_ID",
    "MEETING_SUMMARIES_FOLDER_ID",
    "MEETING_PREP_FOLDER_ID",
    "WEEKLY_DIGESTS_FOLDER_ID",
    "DOCUMENTS_FOLDER_ID",
    "TASK_TRACKER_SHEET_ID",
    "STAKEHOLDER_TRACKER_SHEET_ID",
    "CROPSIGHT_CALENDAR_COLOR_ID",
    "GIANLUIGI_EMAIL",
]


def read_env(env_path: str) -> dict[str, str]:
    """Read key=value pairs from .env file."""
    values = {}
    with open(env_path, "r") as f:
        for line in f:
            line = line.strip()
            # Skip comments and empty lines
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            # Remove surrounding quotes if present
            value = value.strip().strip("'\"")
            values[key] = value
    return values


def _gcloud_cmd() -> str:
    """Return the correct gcloud command name for this OS."""
    # On Windows, gcloud is installed as gcloud.cmd
    if sys.platform == "win32":
        return "gcloud.cmd"
    return "gcloud"


def run_gcloud(args: list[str], input_data: str = None) -> tuple[int, str]:
    """Run a gcloud command and return (return_code, output)."""
    result = subprocess.run(
        [_gcloud_cmd()] + args,
        input=input_data,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout + result.stderr


def main():
    # Check .env exists
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if not os.path.exists(env_path):
        print("ERROR: .env file not found. Run this from the project root.")
        sys.exit(1)

    # Check gcloud is available
    try:
        subprocess.run([_gcloud_cmd(), "version"], capture_output=True, check=True)
    except FileNotFoundError:
        print("ERROR: gcloud CLI not installed.")
        sys.exit(1)

    # Get project ID
    code, project = run_gcloud(["config", "get-value", "project"])
    project = project.strip()
    if not project or code != 0:
        print("ERROR: No GCP project selected. Run: gcloud init")
        sys.exit(1)

    print("=" * 50)
    print("Uploading secrets to GCP Secret Manager")
    print(f"Project: {project}")
    print("=" * 50)
    print()

    env_values = read_env(env_path)

    success = 0
    skipped = 0
    failed = 0

    for secret_name in SECRETS:
        value = env_values.get(secret_name, "")

        # Skip empty or placeholder values
        if not value or value.startswith("your_"):
            print(f"SKIP: {secret_name} (no value or placeholder in .env)")
            skipped += 1
            continue

        # Check if secret already exists
        code, _ = run_gcloud([
            "secrets", "describe", secret_name,
            "--project", project,
        ])

        if code == 0:
            # Secret exists — add new version
            print(f"EXISTS: {secret_name} (adding new version)")
            code, output = run_gcloud([
                "secrets", "versions", "add", secret_name,
                "--data-file=-",
                "--project", project,
            ], input_data=value)
        else:
            # Create new secret
            print(f"CREATE: {secret_name}")
            code, output = run_gcloud([
                "secrets", "create", secret_name,
                "--data-file=-",
                "--project", project,
            ], input_data=value)

        if code == 0:
            success += 1
        else:
            print(f"  FAILED: {output.strip()}")
            failed += 1

    print()
    print("=" * 50)
    print(f"Done! Created/updated: {success}, Skipped: {skipped}, Failed: {failed}")
    print("=" * 50)
    print()
    print("Next step: Deploy with Cloud Build:")
    print("  gcloud builds submit --config cloudbuild.yaml .")


if __name__ == "__main__":
    main()
