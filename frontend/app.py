"""
Pure Chat - User sends first message, Bot asks questions
Email → Name → Language → Timezone → WhatsApp → Tier → Chat
All through pure conversation, no forms!
"""
import streamlit as st
from api_client import APIClient

# Page config
st.set_page_config(page_title="AI Companion", layout="centered")

# Initialize API client
if "api_client" not in st.session_state:
    st.session_state.api_client = APIClient()

# Health check (only once)
if "health_checked" not in st.session_state:
    try:
        st.session_state.api_client.health_check()
        st.session_state.health_checked = True
    except Exception as e:
        st.error(f"❌ Backend not running!\nStart it with:\npython -m uvicorn app.main:app --reload")
        st.stop()

# Session state
if "user_id" not in st.session_state:
    st.session_state.user_id = None

if "access_token" not in st.session_state:
    st.session_state.access_token = None

if "onboarding_completed" not in st.session_state:
    st.session_state.onboarding_completed = False

if "messages" not in st.session_state:
    st.session_state.messages = []

# Main UI
st.title("🤖 AI Companion")

# Display all chat messages
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.write(message["content"])

# Chat input - this is the ONLY input!
user_input = st.chat_input("Type your message...")

if user_input:
    # Add user message to chat
    st.session_state.messages.append({
        "role": "user",
        "content": user_input
    })
    
    # Display user message
    with st.chat_message("user"):
        st.write(user_input)
    
    # Send to backend
    try:
        response = st.session_state.api_client.send_message(
            user_input,
            user_id=st.session_state.user_id
        )
        
        # Extract bot response
        bot_response = response.get("response", "No response")
        
        # Add to chat
        st.session_state.messages.append({
            "role": "assistant",
            "content": bot_response
        })
        
        # Update auth state if new user
        if "access_token" in response:
            st.session_state.access_token = response.get("access_token")
            st.session_state.user_id = response.get("user_id")
        
        # Update onboarding state
        st.session_state.onboarding_completed = response.get("onboarding_completed", False)
        
        # Display bot response
        with st.chat_message("assistant"):
            st.write(bot_response)
        
        # Rerun to show response and update state
        st.rerun()
    
    except Exception as e:
        error_msg = f"❌ Error: {str(e)}"
        st.session_state.messages.append({
            "role": "assistant",
            "content": error_msg
        })
        with st.chat_message("assistant"):
            st.error(error_msg)