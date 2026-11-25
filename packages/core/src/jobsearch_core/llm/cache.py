from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _prompt_hash(system_prompt: str, user_prompt: str) -> str:
    raw = json.dumps({"system": system_prompt, "user": user_prompt}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def load_cached(cache_dir: Path, system_prompt: str, user_prompt: str) -> str | None:
    key = _prompt_hash(system_prompt, user_prompt)
    cache_file = cache_dir / f"{key}.json"
    if cache_file.exists():
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        return data["response"]
    return None


def save_cached(
    cache_dir: Path, system_prompt: str, user_prompt: str, response: str
) -> str:
    key = _prompt_hash(system_prompt, user_prompt)
    cache_file = cache_dir / f"{key}.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        json.dumps({"response": response, "hash": key}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return key
