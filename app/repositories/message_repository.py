"""Message data access layer"""
from sqlalchemy.orm import Session
from sqlalchemy import desc
from app.models.database import Message, Conversation

class MessageRepository:
    @staticmethod
    def create(db: Session, conversation_id: int, user_id: int, role: str, content: str, channel: str = "web"):
        message = Message(
            conversation_id=conversation_id,
            user_id=user_id,
            role=role,
            content=content,
            channel=channel
        )
        db.add(message)
        db.commit()
        db.refresh(message)
        return message
    
    @staticmethod
    def get_last_n(db: Session, user_id: int, n: int = 10):
        messages = db.query(Message).filter(
            Message.user_id == user_id
        ).order_by(desc(Message.created_at)).limit(n).all()
        return messages[::-1]  # Reverse to get oldest first
    
    @staticmethod
    def get_last_40(db: Session, user_id: int):
        """Get last 40 messages for history"""
        messages = db.query(Message).filter(
            Message.user_id == user_id
        ).order_by(desc(Message.created_at)).limit(40).all()
        return messages[::-1]
    
    @staticmethod
    def get_or_create_conversation(db: Session, user_id: int, channel: str = "web"):
        conversation = db.query(Conversation).filter(
            Conversation.user_id == user_id,
            Conversation.channel == channel
        ).first()
        
        if not conversation:
            conversation = Conversation(user_id=user_id, channel=channel)
            db.add(conversation)
            db.commit()
            db.refresh(conversation)
        return conversation
