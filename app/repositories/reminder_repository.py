"""Reminder data access layer"""
from sqlalchemy.orm import Session
from sqlalchemy import desc
from app.models.database import Reminder

class ReminderRepository:
    @staticmethod
    def create(db: Session, user_id: int, content: str, scheduled_at):
        reminder = Reminder(
            user_id=user_id,
            content=content,
            scheduled_at=scheduled_at
        )
        db.add(reminder)
        db.commit()
        db.refresh(reminder)
        return reminder
    
    @staticmethod
    def get_by_user(db: Session, user_id: int):
        return db.query(Reminder).filter(
            Reminder.user_id == user_id
        ).order_by(desc(Reminder.created_at)).all()
    
    @staticmethod
    def get_pending(db: Session):
        from datetime import datetime
        return db.query(Reminder).filter(
            Reminder.scheduled_at <= datetime.utcnow(),
            Reminder.sent_at == None
        ).all()
    
    @staticmethod
    def mark_sent(db: Session, reminder_id: int):
        from datetime import datetime
        reminder = db.query(Reminder).filter(Reminder.id == reminder_id).first()
        if reminder:
            reminder.sent_at = datetime.utcnow()
            db.commit()
        return reminder