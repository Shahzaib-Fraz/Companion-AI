"""API Client for AI Companion Frontend"""
import requests
import os
from dotenv import load_dotenv

load_dotenv()

class APIClient:
    def __init__(self):
        self.base_url = os.getenv("API_URL", "http://127.0.0.1:8000")
        self.timeout = 120

    def health_check(self):
        try:
            response = requests.get(f"{self.base_url}/health", timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.ConnectionError:
            raise Exception("Cannot connect to backend. Make sure it's running on http://127.0.0.1:8000")
        except Exception as e:
            raise Exception(f"Health check failed: {str(e)}")

    def signup(self, email: str, password: str):
        """Create a new account. Returns {access_token, user_id}."""
        try:
            response = requests.post(
                f"{self.base_url}/auth/signup",
                json={"email": email, "password": password},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            detail = self._error_detail(e)
            raise Exception(detail)
        except requests.exceptions.ConnectionError:
            raise Exception("Cannot connect to backend.")

    def login(self, email: str, password: str):
        """Log into an existing account. Returns {access_token, user_id}."""
        try:
            response = requests.post(
                f"{self.base_url}/auth/login",
                json={"email": email, "password": password},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            detail = self._error_detail(e)
            raise Exception(detail)
        except requests.exceptions.ConnectionError:
            raise Exception("Cannot connect to backend.")

    def send_message(self, message: str, access_token: str, channel: str = "web"):
        """
        Send a message to the chat API. Requires a valid access_token —
        the backend now rejects unauthenticated chat requests.
        """
        try:
            url = f"{self.base_url}/chat/message"
            payload = {"message": message, "channel": channel}
            headers = {"Authorization": f"Bearer {access_token}"}

            response = requests.post(url, json=payload, headers=headers, timeout=self.timeout)
            response.raise_for_status()
            return response.json()

        except requests.exceptions.Timeout:
            raise Exception("Request timed out. Backend may be slow or unresponsive.")
        except requests.exceptions.ConnectionError:
            raise Exception("Cannot connect to backend. Make sure it's running.")
        except requests.exceptions.HTTPError as e:
            raise Exception(self._error_detail(e))
        except Exception as e:
            raise Exception(f"API error: {str(e)}")

    @staticmethod
    def _error_detail(e: requests.exceptions.HTTPError) -> str:
        try:
            data = e.response.json()
            return data.get("detail") or data.get("error") or str(e)
        except Exception:
            return str(e)