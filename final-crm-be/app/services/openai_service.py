"""
OpenAI Service
Handles OpenAI API interactions including chat completions and vision
"""
import base64
import json
from typing import List, Dict, Any
from openai import OpenAI
from app.config import settings


class OpenAIService:
    """Service for OpenAI API operations"""

    def __init__(self):
        """Initialize OpenAI client"""
        self.client = OpenAI(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL
        )

    def extract_text_from_image(self, img_bytes: bytes, mime: str) -> str:
        """
        Extract text from image using GPT-4 Vision

        Args:
            img_bytes: Image bytes
            mime: Image MIME type

        Returns:
            Extracted text
        """
        data_url = f"data:{mime};base64," + base64.b64encode(img_bytes).decode()
        r = self.client.responses.create(
            model="gpt-4o-mini",
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Extract only the text found in the image. Output text only, no extras."},
                    {"type": "input_image", "image_url": data_url}
                ]
            }],
            max_output_tokens=4000
        )
        return "".join(
            c.text for o in r.output if o.type == "message"
            for c in o.content if c.type == "output_text"
        ).strip()

    def rerank_documents(
        self,
        query: str,
        candidates: List[str],
        top_n: int = 3,
        model: str = "gpt-3.5-turbo"
    ) -> List[Dict[str, Any]]:
        """
        Rerank documents using OpenAI

        Args:
            query: Search query
            candidates: List of candidate documents
            top_n: Number of top results
            model: Model to use for reranking

        Returns:
            List of reranked documents with scores
        """
        if not candidates:
            return []

        system = "You are a reranker that scores each passage for relevance to the query between 0 and 1."
        user = f"Query: {query}\n\nPassages:\n" + "\n\n".join(
            [f"[{i}] {doc}" for i, doc in enumerate(candidates)]
        )
        scoring_instructions = (
            "Return ONLY minified JSON array on a single line with objects {\"index\":int,\"score\":float}. "
            "No code fences, no prose, no keys other than index and score."
        )

        resp = self.client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user + "\n\n" + scoring_instructions},
            ],
            temperature=0,
        )

        raw = resp.choices[0].message.content or ""
        payload = self._extract_json_array(raw)

        try:
            scores = json.loads(payload)
            assert isinstance(scores, list)
        except Exception as e:
            raise ValueError(f"Failed to parse reranker output: {e}")

        scores.sort(key=lambda x: x["score"], reverse=True)
        return scores[:min(top_n, len(scores))]

    @staticmethod
    def _extract_json_array(text: str) -> str:
        """Extract JSON array from text with potential markdown fences"""
        text = text.strip()

        # Remove markdown code fences if present
        if text.startswith("```"):
            first = text.find("\n")
            last = text.rfind("```")
            if first != -1 and last != -1:
                text = text[first + 1:last].strip()

        # Find array JSON
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            return text[start:end + 1]

        return text
