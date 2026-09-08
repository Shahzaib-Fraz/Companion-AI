"""API Client for AI Companion Frontend"""
import requests
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

class APIClient:
    def __init__(self):
        self.base_url = os.getenv("API_URL", "http://127.0.0.1:8000")
        self.timeout = 120
    
    def health_check(self):
        """
        Check if backend is running
        Called ONLY ONCE at app startup
        """
        try:
            response = requests.get(
                f"{self.base_url}/health",
                timeout=self.timeout
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.ConnectionError:
            raise Exception("Cannot connect to backend. Make sure it's running on http://127.0.0.1:8000")
        except Exception as e:
            raise Exception(f"Health check failed: {str(e)}")
    
    def send_message(self, message: str, user_id: int = None, channel: str = "web"):
        """
        Send a message to the chat API
        
        Args:
            message: User message text
            user_id: User ID (None for email auth, set after auth)
            channel: Communication channel (web, whatsapp, etc)
        
        Returns:
            dict: Response from backend
        """
        try:
            url = f"{self.base_url}/chat/message"
            
            payload = {
                "message": message,
                "channel": channel
            }
            
            headers = {}
            
            # Add auth token if user is authenticated
            if user_id:
                # Get token from session (passed from Streamlit)
                import streamlit as st
                if "access_token" in st.session_state:
                    headers["Authorization"] = f"Bearer {st.session_state.access_token}"
            
            response = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=self.timeout
            )
            
            response.raise_for_status()
            return response.json()
        
        except requests.exceptions.Timeout:
            raise Exception("Request timed out. Backend may be slow or unresponsive.")
        except requests.exceptions.ConnectionError:
            raise Exception("Cannot connect to backend. Make sure it's running.")
        except requests.exceptions.HTTPError as e:
            error_data = {}
            try:
                error_data = e.response.json()
            except:
                error_data = {"error": str(e)}
            raise Exception(f"Backend error: {error_data.get('error', str(e))}")
        except Exception as e:
            raise Exception(f"API error: {str(e)}")