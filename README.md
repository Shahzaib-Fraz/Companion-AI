# 🤖 AI Companion Platform - Complete Working Code

Pure chat-based AI companion with unified Web + WhatsApp support.

## 🚀 Quick Start (5 minutes)

### 1. Setup
```bash
./setup.sh  # Linux/Mac
setup.bat   # Windows
```

### 2. Configure
```bash
# Edit .env with your API keys:
nano .env
```

Add:
- GROQ_API_KEY (from console.groq.com)
- QDRANT_URL & QDRANT_API_KEY (from cloud.qdrant.io)
- MAILTRAP_API_TOKEN (from mailtrap.io)
- JWT_SECRET (generate random)

### 3. Run
```bash
# Terminal 1
python -m uvicorn app.main:app --reload

# Terminal 2
cd frontend
streamlit run app.py
```

### 4. Open
```
http://localhost:8501
```

## ✨ Features

✅ Pure chat-based authentication (no forms!)
✅ Unified Web + WhatsApp context
✅ Last 40 messages in PostgreSQL
✅ Qdrant vectors for premium users
✅ Smart context generation
✅ Email reminders via Mailtrap
✅ APScheduler for background jobs
✅ Tier-based features (Free vs Premium)
✅ Streamlit frontend
✅ Production-ready code

## 📋 Requirements Met

1. ✅ Pure chat-based onboarding (email, name, language, timezone, WhatsApp, tier)
2. ✅ No login/signup forms - everything through chat
3. ✅ Unified context across Web + WhatsApp
4. ✅ PostgreSQL for message storage (last 40 messages)
5. ✅ Qdrant for premium users (context + preferences)
6. ✅ APScheduler for reminders
7. ✅ Mailtrap for emails
8. ✅ Groq LLM integration
9. ✅ Tier-based quality (Free vs Premium)
10. ✅ Database auto-populates after onboarding
11. ✅ Streamlit frontend
12. ✅ Just edit .env - everything else works

## 🗄️ Database (Auto-Created)

- users
- user_profiles  
- conversations
- messages (last 40 kept)
- reminders
- refresh_tokens

## 🎯 Onboarding Flow

1. App opens → Chat: "What's your email?"
2. User types email
3. If new → Ask: Name, Language, Timezone, WhatsApp, Tier
4. Database populated
5. JWT token issued
6. Chat continues naturally

## 📊 Technology

- FastAPI (Backend)
- SQLAlchemy (ORM)
- PostgreSQL (Local)
- Qdrant (Premium memory)
- Groq (LLM)
- APScheduler (Reminders)
- Mailtrap (Email)
- Streamlit (Frontend)

## ✅ Status: Production Ready

All code is complete, tested, and ready to deploy.

Just extract, setup, and run!
