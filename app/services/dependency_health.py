import logging
from datetime import datetime
from typing import Dict, Any

logger = logging.getLogger(__name__)


class DependencyHealth:
    """Track health status of external dependencies"""
    
    def __init__(self):
        self.dependencies = {
            "postgres": {"status": "unknown", "last_checked": None, "error": None},
            "groq": {"status": "unknown", "last_checked": None, "error": None},
            "qdrant": {"status": "unknown", "last_checked": None, "error": None},
            "email": {"status": "unknown", "last_checked": None, "error": None},
            "scheduler": {"status": "unknown", "last_checked": None, "error": None},
        }
    
    def set_status(self, dependency: str, status: str, error: str = None):
        """
        Update dependency status
        
        Args:
            dependency: Name of dependency (postgres, groq, qdrant, email, scheduler)
            status: "up" or "down"
            error: Error message if down
        """
        if dependency not in self.dependencies:
            logger.warning(f"Unknown dependency: {dependency}")
            return
        
        prev_status = self.dependencies[dependency]["status"]
        self.dependencies[dependency]["status"] = status
        self.dependencies[dependency]["last_checked"] = datetime.utcnow()
        self.dependencies[dependency]["error"] = error
        
        # Log state changes
        if status == "down":
            logger.error(
                f"❌ DEPENDENCY DOWN: {dependency}",
                extra={"error": error, "prev_status": prev_status}
            )
        elif status == "up" and prev_status == "down":
            logger.info(f"✅ {dependency.upper()} recovered")
    
    def get_status(self, dependency: str) -> str:
        """Get current status of dependency"""
        if dependency in self.dependencies:
            return self.dependencies[dependency]["status"]
        return "unknown"
    
    def all_up(self) -> bool:
        """Check if ALL dependencies are up"""
        return all(d["status"] == "up" for d in self.dependencies.values())
    
    def critical_up(self) -> bool:
        """
        Check if CRITICAL dependencies are up
        Critical = PostgreSQL + Groq (chat must work)
        """
        critical = ["postgres", "groq"]
        return all(self.dependencies[dep]["status"] == "up" for dep in critical)
    
    def memory_available(self) -> bool:
        """Check if memory feature is available (requires Qdrant)"""
        return self.dependencies["qdrant"]["status"] == "up"
    
    def reminders_available(self) -> bool:
        """Check if reminders feature is available (requires email + scheduler)"""
        return (self.dependencies["email"]["status"] == "up" and
                self.dependencies["scheduler"]["status"] == "up")
    
    def get_all_statuses(self) -> Dict[str, Any]:
        """Return all dependency statuses"""
        return self.dependencies
    
    def health_summary(self) -> Dict[str, Any]:
        """Return health check summary for APIs"""
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "all_dependencies_up": self.all_up(),
            "chat_available": self.critical_up(),
            "memory_available": self.memory_available(),
            "reminders_available": self.reminders_available(),
            "dependencies": self.get_all_statuses(),
        }


# Global singleton instance
dependency_health = DependencyHealth()