from __future__ import annotations

import json
from typing import Any, Dict, List, Literal, Optional, Union, Type

import aiohttp
from pydantic import BaseModel, Field, ValidationError

from app.config import settings


class TableColumn(BaseModel):
    key: str = Field(..., description="Key in each row")
    label: str = Field(..., description="Column label for UI")


class UITableResponse(BaseModel):
    type: Literal["table"] = "table"
    title: Optional[str] = None
    columns: List[TableColumn]
    data: List[Dict[str, Any]]


class UITextResponse(BaseModel):
    type: Literal["text"] = "text"
    title: Optional[str] = None
    text: str


UIResponse = Union[UITableResponse, UITextResponse]


class UIFormatResult(BaseModel):
    response: UIResponse
    narrative: Optional[str] = None


def _extract_proxy_content(result: Dict[str, Any]) -> str:
    return (
        result.get("choices", [{}])[0].get("message", {}).get("content")
        or result.get("reply")
        or result.get("content")
        or ""
    )


def _strip_code_fences(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("```"):
        # remove first line ``` or ```json
        if "\n" in s:
            s = s.split("\n", 1)  # FIX: was[1][2]
        else:
            return ""
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def _validate_model(model_cls: Type[BaseModel], data: Any) -> BaseModel:
    """
    Pydantic v2: model_validate
    Pydantic v1: parse_obj
    """
    mv = getattr(model_cls, "model_validate", None)
    if callable(mv):
        return mv(data)  # pydantic v2
    po = getattr(model_cls, "parse_obj", None)
    if callable(po):
        return po(data)  # pydantic v1
    raise AttributeError(f"No validation method found on {model_cls}")


def _fallback_text(raw_answer: str, preferred_title: Optional[str]) -> UIFormatResult:
    return UIFormatResult(
        response=UITextResponse(
            type="text",
            title=preferred_title,
            text=raw_answer or "Maaf, tidak ada respons.",
        ),
        narrative=None,
    )


async def format_to_ui_via_proxy(
    *,
    user_query: str,
    raw_answer: str,
    organization_id: Optional[str],
    preferred_title: Optional[str] = None,
    timeout_total: int = 60,
    max_retries: int = 1,
) -> UIFormatResult:
    base = (settings.PROXY_BASE_URL or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("PROXY_BASE_URL is not set")
    proxy_url = f"{base}/chat/filemanager"

    system_prompt = (
        "Kamu adalah UI Response Formatter untuk aplikasi web.\n"
        "Output HARUS berupa SATU JSON object valid. Jangan markdown. Jangan pakai ```.\n"
        "Pilih salah satu bentuk berikut:\n"
        "TEXT: {\"type\":\"text\",\"title\":null|string,\"text\":string}\n"
        "TABLE: {\"type\":\"table\",\"title\":null|string,"
        "\"columns\":[{\"key\":string,\"label\":string}],\"data\":[{...}]}\n"
    )

    user_payload = {
        "preferred_title": preferred_title,
        "user_query": user_query,
        "raw_answer": raw_answer,
    }

    payload: Dict[str, Any] = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "files": [],
        "temperature": 0.0,
        "organization_id": organization_id,
        "provider": "openai",
        "response_format": {"type": "json_object"},
    }

    timeout = aiohttp.ClientTimeout(total=timeout_total)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for _attempt in range(max_retries + 1):
            async with session.post(proxy_url, json=payload) as resp:
                body_text = await resp.text()
                if resp.status != 200:
                    payload["messages"].append(
                        {"role": "user", "content": "Gagal. Keluarkan ulang hanya JSON final."}
                    )
                    continue

                try:
                    result = json.loads(body_text)
                except Exception:
                    payload["messages"].append(
                        {"role": "user", "content": "Balasan server bukan JSON. Keluarkan ulang hanya JSON final."}
                    )
                    continue

            content = _strip_code_fences(_extract_proxy_content(result))

            try:
                obj = json.loads(content)
            except Exception:
                payload["messages"].append(
                    {"role": "user", "content": "Output kamu tidak valid JSON. Keluarkan ulang hanya JSON final."}
                )
                continue

            # Validate by discriminator (stable for Union)
            try:
                t = (obj.get("type") or "").strip().lower()
                if t == "table":
                    ui = _validate_model(UITableResponse, obj)
                else:
                    ui = _validate_model(UITextResponse, obj)
                return UIFormatResult(response=ui, narrative=None)
            except (ValidationError, AttributeError):
                payload["messages"].append(
                    {"role": "user", "content": "JSON tidak sesuai schema TEXT/TABLE. Perbaiki, keluarkan hanya JSON final."}
                )
                continue

    return _fallback_text(raw_answer, preferred_title)
