"""Mem0 自定义 Embedder — 直接调火山引擎 Multimodal Embeddings API

绕过 OpenAI embedder + 本地代理，避免单 worker 死锁。
"""
import json
import logging
import urllib.request
import urllib.error

log = logging.getLogger("vibry.embedder")


class VolcengineEmbedder:
    """火山引擎多模态 Embedding 适配器

    实现 Mem0 需要的 embed() 接口，直接调用 multimodal API。
    """

    def __init__(self, model: str, api_key: str, base_url: str):
        self.model = model
        self.api_key = api_key
        self.multimodal_url = base_url.rstrip("/") + "/embeddings/multimodal"

    def embed(self, text: str, memory_action: str = None) -> list[float]:
        """Mem0 接口: 单文本 → 单向量"""
        return self._embed_one(text)

    def _embed_one(self, text: str) -> list[float]:
        multimodal_input = [{"type": "text", "text": text}]
        payload = json.dumps({
            "model": self.model,
            "input": multimodal_input,
        }, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(self.multimodal_url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {self.api_key}")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:300]
            raise RuntimeError(f"Embedding API error {e.code}: {body}")
        except Exception as e:
            raise RuntimeError(f"Embedding request failed: {e}")

        emb_data = data.get("data", {})
        if isinstance(emb_data, dict):
            return emb_data.get("embedding", [])
        if isinstance(emb_data, list) and emb_data:
            return emb_data[0].get("embedding", [])
        return []
