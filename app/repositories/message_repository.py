"""SANDBOX-ONLY reconstruction of message_repository.py - not a deliverable"""
import logging
from sqlalchemy.orm import Session
from app.models.database import Message, Conversation

logger = logging.getLogger(__name__)


class MessageRepository:
    @staticmethod
    def create(db: Session, conversation_id: int, user_id: int, role: str, content: str, channel: str = "web"):
        try:
            message = Message(
                conversation_id=conversation_id,
                user_id=user_id,
                role=role,
                content=content,
                channel=channel,
            )
            db.add(message)
            db.flush()
            return message
        except Exception as e:
            logger.error(f"Failed to create message: {e}")
            return None

    @staticmethod
    def get_or_create_conversation(db: Session, user_id: int, channel: str):
        try:
            conv = db.query(Conversation).filter(
                Conversation.user_id == user_id,
                Conversation.channel == channel,
            ).first()
            if not conv:
                conv = Conversation(user_id=user_id, channel=channel)
                db.add(conv)
                db.flush()
            return conv
        except Exception as e:
            logger.error(f"Failed to get/create conversation: {e}")
            return None

    @staticmethod
    def get_last_n(db: Session, user_id: int, n: int = 10):
        try:
            return db.query(Message).filter(
                Message.user_id == user_id
            ).order_by(Message.created_at.desc()).limit(n).all()[::-1]
        except Exception as e:
            logger.error(f"Failed to get messages: {e}")
            return []

    @staticmethod
    def get_last_500(db: Session, user_id: int):
        try:
            return db.query(Message).filter(
                Message.user_id == user_id
            ).order_by(Message.created_at.desc()).limit(500).all()[::-1]
        except Exception as e:
            logger.error(f"Failed to get last 500 messages: {e}")
            return []

    @staticmethod
    def get_conversation_history(db: Session, conversation_id: int, limit: int = 50):
        try:
            return db.query(Message).filter(
                Message.conversation_id == conversation_id
            ).order_by(Message.created_at).limit(limit).all()
        except Exception as e:
            logger.error(f"Failed to get conversation history: {e}")
            return []