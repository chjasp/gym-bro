from vertexai.generative_models import GenerativeModel
from typing import List, Dict, Optional

class AIService:
    def __init__(self, model_name: str):
        self.model = GenerativeModel(model_name)

    def should_send_message(self, chat_history: List[Dict]) -> bool:
        # ... existing should_send_message logic ...
        pass

    def generate_proactive_message(self, user_data: dict, chat_history: List[Dict]) -> Optional[str]:
        # ... existing generate_proactive_message logic ...
        pass

    def generate_chat_response(self, chat_history: List[Dict], user_message: str) -> str:
        context = "\n".join([
            f"{msg['role']}: {msg['content']}" 
            for msg in chat_history
        ])
        prompt = f"Previous conversation:\n{context}\n\nUser: {user_message}"
        response = self.model.generate_content(prompt)
        return response.text if response.text else None