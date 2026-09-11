"""
Pure Chat - login/signup, then Name → Language → Timezone → Tier → WhatsApp
via conversation. Email/password now handled by dedicated auth forms, not chat.
"""
import streamlit as st
from api_client import APIClient

st.set_page_config(page_title="AI Companion", layout="centered")

if "api_client" not in st.session_state:
    st.session_state.api_client = APIClient()

if "health_checked" not in st.session_state:
    try:
        st.session_state.api_client.health_check()
        st.session_state.health_checked = True
    except Exception:
        st.error("❌ Backend not running!\nStart it with:\npython -m uvicorn app.main:app --reload")
        st.stop()

if "user_id" not in st.session_state:
    st.session_state.user_id = None
if "access_token" not in st.session_state:
    st.session_state.access_token = None
if "onboarding_completed" not in st.session_state:
    st.session_state.onboarding_completed = False
if "messages" not in st.session_state:
    st.session_state.messages = []


# --------------------------------------------------------------- auth gate
def show_auth_screen():
    st.title("🤖 AI Companion")
    st.caption("Log in or create an account to start chatting.")

    login_tab, signup_tab = st.tabs(["Log In", "Sign Up"])

    with login_tab:
        with st.form("login_form"):
            email = st.text_input("Email", key="login_email")
            password = st.text_input("Password", type="password", key="login_password")
            submitted = st.form_submit_button("Log In")

        if submitted:
            if not email or not password:
                st.error("Enter both email and password.")
            else:
                try:
                    result = st.session_state.api_client.login(email, password)
                    st.session_state.access_token = result["access_token"]
                    st.session_state.user_id = result["user_id"]
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

    with signup_tab:
        with st.form("signup_form"):
            email = st.text_input("Email", key="signup_email")
            password = st.text_input("Password (min 8 characters)", type="password", key="signup_password")
            confirm = st.text_input("Confirm password", type="password", key="signup_confirm")
            submitted = st.form_submit_button("Sign Up")

        if submitted:
            if not email or not password:
                st.error("Enter both email and password.")
            elif len(password) < 8:
                st.error("Password must be at least 8 characters.")
            elif password != confirm:
                st.error("Passwords don't match.")
            else:
                try:
                    result = st.session_state.api_client.signup(email, password)
                    st.session_state.access_token = result["access_token"]
                    st.session_state.user_id = result["user_id"]
                    st.rerun()
                except Exception as e:
                    st.error(str(e))


if not st.session_state.access_token:
    show_auth_screen()
    st.stop()


# ---------------------------------------------------------------- chat UI
with st.sidebar:
    if st.button("Log out"):
        st.session_state.access_token = None
        st.session_state.user_id = None
        st.session_state.onboarding_completed = False
        st.session_state.messages = []
        st.rerun()

st.title("🤖 AI Companion")

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.write(message["content"])

user_input = st.chat_input("Type your message...")

if user_input:
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.write(user_input)

    try:
        response = st.session_state.api_client.send_message(
            user_input, access_token=st.session_state.access_token
        )

        bot_response = response.get("response", "No response")

        st.session_state.messages.append({"role": "assistant", "content": bot_response})
        st.session_state.onboarding_completed = response.get("onboarding_completed", False)

        with st.chat_message("assistant"):
            st.write(bot_response)

        st.rerun()

    except Exception as e:
        error_msg = f"❌ Error: {str(e)}"
        st.session_state.messages.append({"role": "assistant", "content": error_msg})
        with st.chat_message("assistant"):
            st.error(error_msg)