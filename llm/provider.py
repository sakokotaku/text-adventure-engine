"""
llm/provider.py
---------------
统一 LLM 调用入口，支持多 Provider 配置。

config.json 格式：
  {
    "active_provider": "deepseek",
    "providers": {
      "deepseek": {
        "base_url": "https://api.deepseek.com",
        "api_key": "sk-...",
        "model": "deepseek-chat",
        "max_tokens": 2048,
        "temperature": 0.9
      },
      "openai": {
        "base_url": "https://api.openai.com",
        "api_key": "sk-...",
        "model": "gpt-4o",
        "max_tokens": 2048,
        "temperature": 0.9
      },
      "claude": {
        "base_url": "https://api.anthropic.com",
        "api_key": "sk-ant-...",
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 2048,
        "temperature": 0.9
      }
    },
    "stream": false,
    "context": {
      "recent_turns": 6,
      "summary_threshold": 20
    }
  }
"""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_PATH   = Path(__file__).resolve().parent.parent / "config.json"
_config_cache: dict | None = None

_DEFAULTS: dict = {
    "active_provider": "deepseek",
    "providers": {
        "deepseek": {
            "base_url": "https://api.deepseek.com",
            "api_key": "",
            "model": "deepseek-chat",
            "max_tokens": 2048,
            "temperature": 0.9,
        }
    },
    "stream": False,
    "context": {"recent_turns": 6, "summary_threshold": 20},
}


def load_config() -> dict:
    """加载配置文件（带内存缓存，进程内只读一次磁盘）"""
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    if CONFIG_PATH.exists():
        _config_cache = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    else:
        _config_cache = _DEFAULTS.copy()
    return _config_cache


def get_provider_cfg(provider_name: str | None = None, config: dict | None = None) -> dict:
    """
    获取指定 provider 的配置。
    如果 provider_name 为 None，返回 active_provider 的配置。
    """
    if config is None:
        config = load_config()
    
    if provider_name is None:
        provider_name = config.get("active_provider", "deepseek")
    
    providers = config.get("providers", {})
    if provider_name not in providers:
        raise ValueError(
            f"config.json 中 providers 里没有 '{provider_name}'，"
            f"可用: {list(providers.keys())}"
        )
    
    return providers[provider_name]


def list_providers(config: dict | None = None) -> list:
    """列出所有可用的 provider 名称"""
    if config is None:
        config = load_config()
    return list(config.get("providers", {}).keys())


def is_streaming(config: dict | None = None) -> bool:
    """是否启用流式输出"""
    if config is None:
        config = load_config()
    return bool(config.get("stream", False))


def get_context_config(config: dict | None = None) -> dict:
    """获取上下文配置"""
    if config is None:
        config = load_config()
    return config.get("context", {"recent_turns": 6, "summary_threshold": 20})


def is_debug(config: dict | None = None) -> bool:
    """是否启用调试模式（config.json 中设置 "debug": true）。"""
    if config is None:
        config = load_config()
    return bool(config.get("debug", False))


def generate(
    system_prompt: str,
    user_prompt: str,
    provider_name: str | None = None,
    force_stream: bool | None = None,
    max_tokens_override: int | None = None,
) -> str:
    """
    统一生成入口。

    Args:
        system_prompt: 系统提示词
        user_prompt: 用户提示词
        provider_name: 指定 provider，None 则使用 active_provider
        force_stream: None=读 config，True/False=强制覆盖
        max_tokens_override: 临时覆盖 max_tokens（不修改原始 config）

    Returns:
        生成的文本内容
    """
    config = load_config()
    pcfg   = get_provider_cfg(provider_name, config)
    stream = is_streaming(config) if force_stream is None else force_stream

    if max_tokens_override:
        pcfg = dict(pcfg)  # 不修改原始 config
        pcfg["max_tokens"] = max_tokens_override

    logger.info(
        "generate: provider=%s  model=%s  stream=%s  max_tokens=%s  sys=%d字  user=%d字",
        config.get("active_provider"), pcfg.get("model"), stream,
        pcfg.get("max_tokens"), len(system_prompt), len(user_prompt),
    )
    result = _call_openai_style(system_prompt, user_prompt, pcfg, stream=stream)
    logger.info("generate 完成：响应 %d 字", len(result))
    return result


def generate_with_history(
    system_prompt: str,
    history: list[dict],
    user_input: str,
    provider_name: str | None = None,
    force_stream: bool | None = None,
) -> str:
    """
    带历史对话的生成入口。

    Args:
        system_prompt: 系统提示词
        history: 历史对话列表 [{"role": "user"/"assistant", "content": "..."}]
        user_input: 本轮用户输入
        provider_name: 指定 provider
        force_stream: None=读 config，True/False=强制覆盖（存档时传 False 可静默生成）

    Returns:
        生成的文本内容
    """
    config = load_config()
    pcfg   = get_provider_cfg(provider_name, config)
    stream = is_streaming(config) if force_stream is None else force_stream

    logger.info(
        "generate_with_history: provider=%s  model=%s  stream=%s  历史=%d条  input=%d字",
        config.get("active_provider"), pcfg.get("model"), stream,
        len(history), len(user_input),
    )
    result = _call_openai_style_with_history(
        system_prompt, history, user_input, pcfg, stream=stream
    )
    logger.info("generate_with_history 完成：响应 %d 字", len(result))
    return result


# ─── 内部实现 ─────────────────────────────────────────────────────

def _call_openai_style(
    system_prompt: str,
    user_prompt: str,
    pcfg: dict,
    stream: bool = False,
) -> str:
    """OpenAI 兼容格式调用"""
    api_key = pcfg.get("api_key", "")
    if not api_key or "填你的" in api_key:
        raise RuntimeError(
            f"请先在 config.json 的 providers 中填写 API Key"
        )

    base_url = pcfg.get("base_url", "https://api.deepseek.com").rstrip("/")

    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    payload = {
        "model": pcfg.get("model", "deepseek-chat"),
        "max_tokens": pcfg.get("max_tokens", 2048),
        "temperature": pcfg.get("temperature", 0.9),
        "messages": messages,
        "stream": stream,
    }
    
    return _send_request(base_url, api_key, payload, stream)


def _call_openai_style_with_history(
    system_prompt: str,
    history: list[dict],
    user_input: str,
    pcfg: dict,
    stream: bool = False,
) -> str:
    """OpenAI 兼容格式调用，带历史对话"""
    api_key = pcfg.get("api_key", "")
    if not api_key or "填你的" in api_key:
        raise RuntimeError(
            f"请先在 config.json 的 providers 中填写 API Key"
        )

    base_url = pcfg.get("base_url", "https://api.deepseek.com").rstrip("/")

    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    
    # 添加历史对话
    for msg in history:
        messages.append({
            "role": msg.get("role", "user"),
            "content": msg.get("content", "")
        })
    
    # 添加当前输入
    messages.append({"role": "user", "content": user_input})

    payload = {
        "model": pcfg.get("model", "deepseek-chat"),
        "max_tokens": pcfg.get("max_tokens", 2048),
        "temperature": pcfg.get("temperature", 0.9),
        "messages": messages,
        "stream": stream,
    }
    
    return _send_request(base_url, api_key, payload, stream)


def _send_request(
    base_url: str,
    api_key: str,
    payload: dict,
    stream: bool = False,
) -> str:
    """发送 HTTP 请求，网络抖动时自动重试最多3次"""
    import time as _time

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    _RETRYABLE = (
        ConnectionResetError,
        ConnectionError,
        TimeoutError,
        OSError,
    )
    max_retries = 3
    for attempt in range(max_retries):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/v1/chat/completions",
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                if stream:
                    return _handle_stream(resp)
                result = json.loads(resp.read().decode("utf-8"))
                return result["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"API 请求失败 [{e.code}]: {body}") from e
        except _RETRYABLE as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning("网络错误（第%d次），%ds后重试：%s", attempt + 1, wait, e)
                _time.sleep(wait)
            else:
                raise RuntimeError(f"网络连接失败（已重试{max_retries}次）：{e}") from e


_STREAM_SEPARATOR = "---JSON---"

def _handle_stream(resp) -> str:
    """处理流式 SSE 响应：逐字打印叙事部分，检测到 ---JSON--- 后停止打印，返回完整内容。"""
    full_content = ""
    suppress_print = False  # 检测到分隔符后停止打印
    for raw_line in resp:
        line = raw_line.decode("utf-8").rstrip("\r\n")
        if not line.startswith("data: "):
            continue
        data_str = line[6:]
        if data_str.strip() == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
            delta = chunk["choices"][0]["delta"].get("content", "")
            if delta:
                full_content += delta
                if not suppress_print:
                    if _STREAM_SEPARATOR in full_content:
                        suppress_print = True
                        # 只打印分隔符之前的部分（处理分隔符跨 chunk 到达的情况）
                        before = full_content[:full_content.index(_STREAM_SEPARATOR)]
                        # 计算已多打印的字符数，用退格覆盖
                        already_printed = len(delta) - (len(before) - (len(full_content) - len(delta)))
                        if already_printed > 0:
                            print("\b" * already_printed + " " * already_printed + "\b" * already_printed, end="", flush=True)
                    else:
                        print(delta, end="", flush=True)
        except Exception as e:
            logger.debug("SSE chunk 解析跳过：%s", e)
    print()  # 末尾换行
    logger.debug("流式响应完成，共 %d 字", len(full_content))
    return full_content
