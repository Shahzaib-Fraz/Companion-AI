# 🤖 AI Companion Platform

A FastAPI backend for AI conversations with **memory**, **reminders**, and **unified Web + WhatsApp** support.

## 🚀 Quick Start

### 1. Prerequisites
```bash
python 3.10+
postgresql 13+
```

### 2. Setup
```bash
# Create .env file
cp .env.example .env

# Edit .env with your credentials:
# - DATABASE_URL
# - GROQ_API_KEY
# - QDRANT_URL + API_KEY
# - BREVO_API_KEY (emails)
# - WHATSAPP tokens
```

### 3. Install & Run
```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

**Server running**: http://localhost:8000

---

## 📁 Key Files

```
app/
├── services/
│   ├── scheduler_service.py    ✨ Unified scheduler (reminders + summaries)
│   ├── llm_service.py
│   ├── memory_service.py
│   └── embedding_service.py
├── api/
│   ├── chat_routes.py
│   ├── reminder_routes.py
│   └── whatsapp_routes.py
└── repositories/
    └── message_repository.py

main.py                         ✨ Updated with scheduler
```

---

## 🔄 How It Works

**Every 1 minute:**
- Check for due reminders → Send via Email/WhatsApp

**Every 6 hours:**
- Get last 500 messages per user
- Generate conversation summary
- Extract user preferences (premium)
- Store in Qdrant

---

## 📚 API Endpoints

```bash
# Chat
POST /chat/message
Authorization: Bearer {token}

# Reminders
POST /reminders
GET /reminders

# WhatsApp
POST /whatsapp/webhook

# Health
GET /health
GET /health/detailed
```

---

## 📖 Documentation

- **API Docs**: http://localhost:8000/docs
- **API ReDoc**: http://localhost:8000/redoc

---

## 🔧 Change Scheduler Timing

Edit `main.py`:

```python
scheduler.add_job(
    ...,
    minutes=1,  # ← Change to 2, 5, 10, etc
    ...
)
```

---

## 🚀 Deploy

```bash
# Docker
docker-compose up -d

# Production
gunicorn -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 main:app
```

---

## ✅ Check Status

```bash
# Health check
curl http://localhost:8000/health

# Logs
tail -f app.log
```

---

## ⚡ Features

✅ Unified Web + WhatsApp chat  
✅ Smart reminders (email/WhatsApp)  
✅ Conversation summaries (every 6 hours)  
✅ User preferences (premium)  
✅ Rate limiting & health checks  
✅ Production ready  

---

## 🛠️ Tech Stack

| Component | Tech |
|-----------|------|
| **API** | FastAPI |
| **Database** | PostgreSQL |
| **Vector DB** | Qdrant |
| **LLM** | Groq (Qwen 2.5) |
| **Scheduler** | APScheduler |
| **Email** | Brevo |
| **WhatsApp** | Cloud API |

---

## 🔐 Security

✅ JWT authentication  
✅ Rate limiting  
✅ CORS configured  
✅ SQL injection prevention  
✅ HTTPS ready  

---

## 🚀 Environment Variables

```env
# Database
DATABASE_URL=postgresql://user:password@localhost/db

# LLM & Embeddings
GROQ_API_KEY=your_key
GROQ_MODEL=qwen-2.5
EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
EMBEDDING_DIMENSION=384

# Vector DB
QDRANT_URL=https://your-qdrant.qdrant.io:6333
QDRANT_API_KEY=your_key

# Email
BREVO_API_KEY=your_key
BREVO_SENDER_EMAIL=noreply@example.com

# WhatsApp
WHATSAPP_API_URL=https://graph.instagram.com/v18.0
WHATSAPP_BUSINESS_ACCOUNT_ID=your_id
WHATSAPP_ACCESS_TOKEN=your_token
WHATSAPP_VERIFY_TOKEN=your_token

# Auth
JWT_SECRET_KEY=your_secret
JWT_ALGORITHM=HS256

# CORS
ALLOWED_ORIGINS=["http://localhost:3000"]
```

---

## 📊 Scheduler Details

### Reminders (Every 1 minute)
```
- Query due reminders from PostgreSQL
- Dispatch via Email/WhatsApp
- Mark as sent (prevents duplicates)
```

### Summaries (Every 6 hours)
```
- Fetch last 500 messages per user
- Get previous summary from Qdrant
- LLM integrates old + new context
- Store updated summary in Qdrant
- Extract preferences (premium users)
```

---

## 🧪 Testing

### Manual Trigger
```python
# test_scheduler.py
import asyncio
from app.core.database import SessionLocal
from app.services.scheduler_service import get_reminder_scheduler_service

async def test():
    db = SessionLocal()
    service = get_reminder_scheduler_service(db)
    await service.process_message_summaries_if_due()

asyncio.run(test())
```

### Check Health
```bash
curl http://localhost:8000/health/detailed
```

---

## 🐛 Troubleshooting

### Scheduler Not Running
```python
from main import scheduler
print(scheduler.running)  # Should be True
```

### Reminders Not Sending
```bash
# Check logs
tail -f app.log | grep "dispatch\|❌"
```

### Summaries Not Generating
```bash
# Check user has 50+ messages
# Verify Qdrant connection
# Check GROQ_API_KEY
```

---

## 📞 Support

- **Docs**: http://localhost:8000/docs
- **Health**: http://localhost:8000/health
- **Status**: http://localhost:8000/

---

**Version**: 1.0.0  
**Status**: ✅ Production Ready  
**Last Updated**: September 14, 2025