"""Unified LLM dispatcher.

One async function, one return shape, three backends selected by a string.
The provider choice is made upstream and passed in.

Return shape: {"text": str, "usd": float | None, "tokens": int | None}
"""
import asyncio
from typing import Any

async def _call_litellm(prompt: str, *, model: str, api_key: str,
                        timeout_s: float, **_) -> dict:
    # import litellm

    # resp = await litellm.acompletion(
    #     model=model,
    #     api_key=api_key,
    #     timeout=timeout_s,
    #     messages=[{"role": "user", "content": prompt}],
    # )
    # usage = getattr(resp, "usage", None)
    # return resp.choices[0].message.content or ""
    return



async def _call_claude(prompt: str, *, timeout_s: float, **_) -> dict:
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    text, usd, tokens = "", None, None
    async with asyncio.timeout(timeout_s):
        async for msg in query(prompt=prompt.user_prompt, options=ClaudeAgentOptions(allowed_tools=[])):
            if isinstance(msg, ResultMessage):
                text = msg.result or ""
    return text


async def _call_antigravity(prompt: Any, *, project: str, location: str,
                            model: str, timeout_s: float, **_) -> str:
    from google.antigravity import Agent, LocalAgentConfig

    config = LocalAgentConfig(
        vertex=True, project=project, location=location, model=model, system_instructions=prompt.system_prompt
    )
    # 1. Initialize the agent connection safely
    async with Agent(config) as agent:
        try:
            # 2. Wrap only the API interaction inside the timeout
            async with asyncio.timeout(timeout_s):
                resp = await agent.chat(prompt.user_prompt)
                text = await resp.text()
        except TimeoutError:
            print(f"The Antigravity SDK did not respond within {timeout_s} seconds.")
            return "Timeout failure."

    return text

async def _call_gemini(
    prompt: Any,  # Accepts prompt object containing .system_prompt and .user_prompt
    *, 
    project: str, 
    location: str,
    model: str, 
    timeout_s: float, 
    **_  # Silently swallows extra configuration kwargs
) -> str:
    # Inline import for the official unified google-genai library
    from google import genai
    from google.genai import types

    # 1. Initialize the client using the exact same authentication keys
    client = genai.Client(
        vertexai=True,        # Directs traffic to Google Cloud Vertex AI infrastructure
        project=project,      # Google Cloud Project ID string
        location=location     # target deployment region (e.g., 'us-central1')
    )

    try:
        # Use generate_content directly on the model endpoint for single-turn execution
        response = await client.aio.models.generate_content(
            model=model,
            contents=prompt.user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=prompt.system_prompt,
            )
        )
        return response.text
        # # 2. Wrap only the API interaction inside the timeout boundary
        # async with asyncio.timeout(timeout_s):
        #     # Create a structured chat session mapping your system instructions
        #     chat = client.aio.models.generate_content(
        #         model=model,
        #         config=types.GenerateContentConfig(
        #             system_instruction=prompt.system_prompt,
        #         )
        #     )
            
        #     # Send the user prompt asynchronously down the connection channel
        #     # response = await chat.send_message_async(prompt.user_prompt)
        #     return response.text

    except TimeoutError:
        print(f"The Gemini agent did not respond within {timeout_s} seconds.")
        return "Timeout failure."



_HANDLERS = {
    "litellm": _call_litellm,
    "claude": _call_claude,
    "antigravity": _call_antigravity,
    "gemini": _call_gemini,
}


async def call_llm(provider: str, prompt: str, *, timeout_s: float = 90.0, **kwargs) -> dict:
    """provider: "litellm" | "claude" | "antigravity".

    Provider-specific kwargs:
      litellm     - model, api_key
      claude      - none (auth from environment)
      antigravity - project, location, model
    """
    try:
        handler = _HANDLERS[provider]
    except KeyError:
        raise ValueError(f"unknown provider: {provider!r} (choose from {list(_HANDLERS)})")

    return await handler(prompt, timeout_s=timeout_s, **kwargs)