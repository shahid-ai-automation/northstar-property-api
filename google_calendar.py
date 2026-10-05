import os
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

CLIENT_SECRETS_FILE = "client_secret_497968336098-09cglrllthv02a6vdudq8hsh6fq14gdn.apps.googleusercontent.com.json"

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.send"
]

REDIRECT_URI = "http://localhost:8000/oauth2callback"


def create_google_flow():
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI
    )

    return flow


def get_gmail_service():
    credentials = Credentials.from_authorized_user_file(
        "token.json",
        SCOPES
    )

    service = build("gmail", "v1", credentials=credentials)

    return service