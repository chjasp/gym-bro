import logging
from datetime import datetime
from google.cloud import firestore
from typing import List, Dict, Optional

class Database:
    def __init__(self, project_id: str):
        self.db = firestore.Client(project=project_id)
        
    def store_chat_message(self, telegram_id: str, role: str, content: str):
        try:
            user_doc_ref = self.db.collection("users").document(telegram_id)
            message = {
                "role": role,
                "content": content,
                "timestamp": datetime.utcnow().isoformat()
            }
            user_doc_ref.update({
                "chat_history": firestore.ArrayUnion([message])
            })
        except Exception as e:
            logging.error(f"Error storing chat message: {e}")

    def get_chat_history(self, telegram_id: str, limit: int = 10) -> list:
        try:
            user_doc = self.db.collection("users").document(telegram_id).get()
            if user_doc.exists:
                chat_history = user_doc.to_dict().get("chat_history", [])
                return chat_history[-limit:]
            return []
        except Exception as e:
            logging.error(f"Error retrieving chat history: {e}")
            return []

    def get_all_users(self):
        return self.db.collection("users").stream()