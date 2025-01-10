from pydantic import BaseModel
from typing import Optional

class UserProfile(BaseModel):
    telegram_id: str
    whoop_access_token: Optional[str] = None
    chat_history: Optional[list] = []