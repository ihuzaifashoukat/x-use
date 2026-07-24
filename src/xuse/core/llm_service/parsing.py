import json
import re
from typing import Optional, Dict, Any, Tuple


def extract_json_from_response_text(text: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Attempt to extract and parse a JSON object from a model response that
    may include prose or markdown fences.
    """
    if not text:
        return None, "Empty response"

    fence_pattern = re.compile(r"```json\s*(.*?)```", re.DOTALL | re.IGNORECASE)
    m = fence_pattern.search(text)
    candidate: Optional[str] = None
    if m:
        candidate = m.group(1).strip()
    else:
        start = text.find('{')
        if start != -1:
            depth = 0
            in_string = False
            escaped = False
            for i in range(start, len(text)):
                ch = text[i]
                if in_string:
                    # Braces inside a JSON string literal must not affect the
                    # depth balance; handle backslash escapes so an escaped
                    # quote does not end the string early.
                    if escaped:
                        escaped = False
                    elif ch == '\\':
                        escaped = True
                    elif ch == '"':
                        in_string = False
                    continue
                if ch == '"':
                    in_string = True
                elif ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        candidate = text[start : i + 1]
                        break

    if not candidate:
        return None, "No JSON candidate found in response"

    try:
        parsed = json.loads(candidate)
    except Exception:
        cleaned = (
            candidate.replace('“', '"')
            .replace('”', '"')
            .replace('’', "'")
            .replace('`', '')
        )
        try:
            parsed = json.loads(cleaned)
        except Exception as e2:
            return None, f"JSON parse failed: {e2} | Original candidate: {candidate[:2000]}"

    if not isinstance(parsed, dict):
        # Only object-shaped JSON satisfies the extraction contract; arrays
        # and scalars go down the error path so callers get a retry instead
        # of a value that crashes on .get().
        return None, f"JSON candidate is not an object (got {type(parsed).__name__})"
    return parsed, None

