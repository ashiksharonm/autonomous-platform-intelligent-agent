import json
import re
import urllib.request

from agent import config
from agent.config import GROQ_API_KEY, GROQ_MODEL

_client = None


def setup_groq():
    global _client
    if _client is None:
        from groq import Groq  # imported lazily so mock/gemini modes never need it

        _client = Groq(api_key=GROQ_API_KEY)
    return _client


def provider():
    """Resolve the LLM provider: explicit LLM_PROVIDER, else the first free-tier key found, else mock."""
    choice = config.LLM_PROVIDER
    if choice != "auto":
        if choice not in ("groq", "gemini", "mock"):
            raise ValueError(f"LLM_PROVIDER must be auto, groq, gemini or mock (got {choice!r})")
        return choice
    if config.GROQ_API_KEY:
        return "groq"
    if config.GEMINI_API_KEY:
        return "gemini"
    return "mock"


def _http_post_json(url, headers, payload, timeout=60):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", **headers}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed https endpoint)
        return json.loads(response.read())


def _gemini_message(sys_prompt, user_msg, temp_val):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent"
    data = _http_post_json(
        url,
        {"x-goog-api-key": config.GEMINI_API_KEY},
        {
            "systemInstruction": {"parts": [{"text": sys_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
            "generationConfig": {"temperature": temp_val},
        },
    )
    return "".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"])


def clean_json_string(raw_text):
    found_block = re.search(r"```(?:json)?\s*(.*?)\s*```", raw_text, re.DOTALL)
    
    if found_block:
        return found_block.group(1)
    else:
        return raw_text


def clean_python_code(raw_text):
    found_code = re.search(r"```(?:python)?\s*(.*?)\s*```", raw_text, re.DOTALL)
    
    if found_code:
        return found_code.group(1)
    else:
        return raw_text


def send_message(sys_prompt, user_msg, temp_val=0.0):
    if provider() == "gemini":
        return _gemini_message(sys_prompt, user_msg, temp_val)
    client = setup_groq()
    
    api_response = client.chat.completions.create(
        model=GROQ_MODEL,
        temperature=temp_val,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_msg},
        ],
    )
    
    return api_response.choices[0].message.content


def chat_json(sys_prompt, user_msg, max_tries=1):
    if provider() == "mock":
        from agent import mock_llm

        return mock_llm.plan(sys_prompt, user_msg)
    parse_error = None
    
    for try_num in range(max_tries + 1):
        if try_num == 0:
            current_prompt = user_msg
        else:
            current_prompt = user_msg + "\n\nYour previous response failed to parse as JSON: " + str(parse_error) + "\nReturn ONLY a valid JSON object, no prose, no markdown fences."
            
        raw_reply = send_message(sys_prompt, current_prompt, temp_val=0.0)
        
        try:
            cleaned_text = clean_json_string(raw_reply)
            parsed_data = json.loads(cleaned_text)
            return parsed_data
        except json.JSONDecodeError as err:
            parse_error = str(err)
            
    raise ValueError("LLM did not return valid JSON after " + str(max_tries + 1) + " attempts: " + str(parse_error))


def generate_code(sys_prompt, user_msg):
    if provider() == "mock":
        from agent import mock_llm

        return mock_llm.generate_code(sys_prompt, user_msg)
    raw_reply = send_message(sys_prompt, user_msg, temp_val=0.0)
    final_code = clean_python_code(raw_reply)
    
    return final_code