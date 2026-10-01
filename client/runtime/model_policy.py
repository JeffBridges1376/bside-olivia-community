"""Encode trusted policy fields as server references; never rewrite source facts."""
from __future__ import annotations

import copy
import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

PREFIX = "[[olivia-policy:v1:"
CONTROL_FIELDS = frozenset({"instructions", "instruction", "contract", "choice_contract",
    "rules", "question_rules", "grounding", "life_rhythm", "evidence_use",
    "character_participation", "forbidden_rules", "checks", "response"})


@lru_cache(maxsize=1)
def aliases():
    data = json.loads(Path(__file__).with_name("model_policy_aliases.json").read_text("utf-8"))
    return sorted(data.items(), key=lambda pair: len(pair[0]), reverse=True)


def managed(url):
    # A BYOK provider never receives Olivia's internal reference syntax.
    from llm_gateway import _RELAY_HOST
    return urlsplit(url).hostname == _RELAY_HOST


def encode_text(value):
    if not isinstance(value, str):
        return value
    # Match a whole instruction or a known static prefix. Preserve dynamic suffixes.
    parts=[]
    remaining=value
    for _ in range(64):
        matched=next(((original,policy_id) for original,policy_id in aliases() if remaining.startswith(original)),None)
        if matched is None:
            break
        original,policy_id=matched
        parts.append(PREFIX+policy_id+"]]" )
        remaining=remaining[len(original):]
    return ''.join(parts)+remaining


def encode_controls(value):
    if isinstance(value, list):
        return [encode_controls(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = copy.deepcopy(value)
    for key in CONTROL_FIELDS & result.keys():
        item = result[key]
        if isinstance(item, str):
            result[key] = encode_text(item)
        elif isinstance(item, dict):
            result[key] = {k: encode_text(v) for k, v in item.items()}
        elif isinstance(item, list):
            result[key] = [encode_text(v) for v in item]
    return result


def encode_system(value):
    if not isinstance(value, str):
        return value
    value = encode_text(value)
    def block(match):
        try:
            data = json.loads(match[2])
        except ValueError:
            return match[0]
        if isinstance(data, list):
            data = [encode_text(item) for item in data]
        elif isinstance(data, dict):
            data = encode_controls(data)
        return f"<{match[1]}>\n" + json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", r"\u003c").replace(">", r"\u003e") + f"\n</{match[1]}>"
    # Encode application rules; leave persona, dialogue, world and time facts intact.
    return re.sub(r"<(forbidden|grounding|evidence_use|character_participation)>\s*(.*?)\s*</\1>", block, value, flags=re.S)


def encode_chat(body, url):
    if not managed(url):
        return body
    result = copy.deepcopy(body)
    for message in result.get("messages", []):
        if message.get("role") in {"system", "developer"}:
            message["content"] = encode_system(message.get("content"))
    return result


def encode_decision(packet, url):
    if not managed(url):
        return packet
    result = copy.deepcopy(packet)
    result["state"] = encode_controls(result["state"])
    for question in result["questions"].values():
        question["instructions"] = encode_text(question["instructions"])
    return result
