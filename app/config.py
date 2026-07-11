"""Vibry AI Core — 配置管理"""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class UpstreamConfig:
    """上游 LLM API 配置"""
    base_url: str = os.getenv("UPSTREAM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
    api_key: str = os.getenv("UPSTREAM_API_KEY", "")
    model: str = os.getenv("UPSTREAM_MODEL", "doubao-seed-2-1-turbo-260628")
    # ASR 专用模型（云端模式）
    cloud_asr_model: str = os.getenv("CLOUD_ASR_MODEL", "doubao-seed-2-0-mini-260428")
    # embedding 模型（用于 Mem0 向量化）
    embedding_model: str = os.getenv("UPSTREAM_EMBEDDING_MODEL", "doubao-embedding-text-240715")
    timeout: int = 120


@dataclass
class AsrConfig:
    """ASR 语音识别配置"""
    # "local" = FunASR Paraformer 本地模型, "cloud" = Doubao API 云端转写
    # "cloud_flash" = 豆包极速版, "cloud_standard" = 豆包标准版（说话人分离）
    mode: str = os.getenv("ASR_MODE", "local")
    # Whisper 模型大小（备用，当前默认用 FunASR）
    whisper_size: str = os.getenv("WHISPER_SIZE", "small")
    # HuggingFace 镜像（国内加速）
    hf_endpoint: str = os.getenv("HF_ENDPOINT", "https://hf-mirror.com")


@dataclass
class AudioConfig:
    """音频处理配置"""
    ffmpeg_path: str = os.getenv("FFMPEG_PATH", "ffmpeg")
    # 音频存储目录
    _audio_dir: str = ""
    _debug_dir: str = ""

    @property
    def audio_dir(self) -> str:
        if not self._audio_dir:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            self._audio_dir = os.path.join(base_dir, "audio")
            os.makedirs(self._audio_dir, exist_ok=True)
        return self._audio_dir

    @property
    def debug_dir(self) -> str:
        if not self._debug_dir:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            self._debug_dir = os.path.join(base_dir, "debug")
            os.makedirs(self._debug_dir, exist_ok=True)
        return self._debug_dir


@dataclass
class DoubaoAsrConfig:
    """豆包 ASR 云端配置 — 启动时从 DB 读取，env vars 兜底"""
    app_id: str = ""
    access_key: str = ""
    flash_url: str = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
    standard_url: str = "https://openspeech-direct.zijieapi.com/api/v3/auc/bigmodel/submit"

    def __post_init__(self):
        # 先用 env vars 作为初始值
        self.app_id = os.getenv("DOUBAO_ASR_APP_ID", "")
        self.access_key = os.getenv("DOUBAO_ASR_ACCESS_KEY", "")
        self.flash_url = os.getenv("DOUBAO_ASR_FLASH_URL",
            "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash")
        self.standard_url = os.getenv("DOUBAO_ASR_STANDARD_URL",
            "https://openspeech-direct.zijieapi.com/api/v3/auc/bigmodel/submit")

    def reload_from_db(self):
        """服务启动后从 DB 重新加载（覆盖 env vars）"""
        try:
            import db
            asr_cfg = db.get_asr_config()
            if asr_cfg.get("app_id"):
                self.app_id = asr_cfg["app_id"]
            if asr_cfg.get("access_key"):
                self.access_key = asr_cfg["access_key"]
            if asr_cfg.get("flash_url"):
                self.flash_url = asr_cfg["flash_url"]
            if asr_cfg.get("standard_url"):
                self.standard_url = asr_cfg["standard_url"]
        except Exception:
            pass  # DB 还没初始化，保持 env vars 值

    @property
    def voice_mode(self) -> str:
        """语音聊天 ASR 模式（默认极速版）"""
        try:
            import db
            asr_cfg = db.get_asr_config()
            return asr_cfg.get("voice_mode", "cloud")
        except Exception:
            return "cloud"


@dataclass
class PromptConfig:
    """Prompt 模板配置"""
    insight_prompt: str = os.getenv("INSIGHT_PROMPT", "")


@dataclass
class SummaryConfig:
    """会议纪要配置"""
    # 摘要专用模型（可与主 chat 模型不同）
    model: str = os.getenv("SUMMARY_MODEL", "")
    # 用户画像（注入到纪要 system prompt 中）
    user_name: str = os.getenv("USER_NAME", "用户")
    user_role: str = os.getenv("USER_ROLE", "创始人")
    user_context: str = os.getenv("USER_CONTEXT", "关注现金流、项目进度、团队协作。偏好简洁直接的沟通风格，重视数据驱动决策。")
    default_tags: str = os.getenv("USER_DEFAULT_TAGS", "会议纪要,行动项,决策记录")

    @property
    def tags_list(self) -> list[str]:
        return [t.strip() for t in self.default_tags.split(",") if t.strip()]

    @property
    def system_prompt(self) -> str:
        """生成纪要 system prompt — DB 优先，代码兜底"""
        try:
            import db
            asr_cfg = db.get_asr_config()
            db_prompt = asr_cfg.get("summary_prompt", "")
            if db_prompt.strip():
                return db_prompt
        except Exception:
            pass
        # 代码兜底
        return f"""你是一位专业的会议纪要撰写助手，也是用户的数字孪生战略副驾。请根据以下录音转写内容，结合用户的个人背景和偏好，完成分析。

要求：按以下 JSON 格式输出（不要包含 markdown 代码块标记），每个字段都必须填写：

{{
  "current_intent": "用一句话总结本次录音的核心目的",
  "key_decisions": ["决策1", "决策2"],
  "action_items": ["行动项1 @责任人", "行动项2 @责任人"],
  "memory_conflict": "对比用户过去的偏好和习惯，指出一致性或矛盾点",
  "proactive_next": "基于用户日程的建议行动",
  "tags": ["标签1", "标签2", "标签3"],
  "detailed_summary": "一段300-500字的结构化完整纪要，分段包括：会议背景、讨论要点、结论、后续安排"
}}

请用中文输出，简洁专业，避免废话。特别是 action_items 必须明确责任人。tags 必须用中文短语，3-5个。"""

    @property
    def user_profile_text(self) -> str:
        """生成用户画像文本（注入到 system prompt）"""
        return f"""【用户背景】
姓名：{self.user_name}
角色：{self.user_role}
关注点：{self.user_context}
常用标签：{'、'.join(self.tags_list)}"""


@dataclass
class MemoryConfig:
    """Mem0 记忆引擎配置"""
    collection: str = os.getenv("MEM0_COLLECTION", "vibry_memories")
    vector_store: str = os.getenv("MEM0_VECTOR_STORE", "qdrant_local")
    qdrant_path: str = os.getenv("MEM0_QDRANT_PATH", "./qdrant_data")
    top_k: int = int(os.getenv("MEMORY_TOP_K", "5"))
    threshold: float = float(os.getenv("MEMORY_THRESHOLD", "0.35"))


@dataclass
class ServerConfig:
    """服务配置"""
    host: str = os.getenv("SERVER_HOST", "0.0.0.0")
    port: int = int(os.getenv("SERVER_PORT", "9999"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")


@dataclass
class AppConfig:
    """聚合配置"""
    upstream: UpstreamConfig = field(default_factory=UpstreamConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    doubao_asr: DoubaoAsrConfig = field(default_factory=DoubaoAsrConfig)
    summary: SummaryConfig = field(default_factory=SummaryConfig)
    prompt: PromptConfig = field(default_factory=PromptConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    server: ServerConfig = field(default_factory=ServerConfig)


# 全局单例
config = AppConfig()
