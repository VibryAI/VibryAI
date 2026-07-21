"""Vibry AI Core — 配置管理"""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


DEFAULT_SUMMARY_PROMPT = """你是专业录音内容整理专员。请基于语音转写全文生成客观、完整的结构化纪要，严禁编造原文不存在的内容。

只输出纯 Markdown，不要输出 JSON，不要使用 Markdown 代码块。

# 录音纪要

## 基本信息
仅输出能够从文件名、系统上下文或录音原文中明确确认的信息：
- 录音时间
- 录音时长
- 核心主题
- 关键词

无法确认的项目直接省略，不要填写“未知”“未识别”或推测内容。

## 核心目的
用一段话概括本次录音的核心主题和目的，不超过100字。

## 关键决定
只记录录音中明确作出的决定，使用无序列表。
如果没有明确决定，省略整个“关键决定”章节。

## 行动项
只记录录音中明确提出的后续行动，使用无序列表。
原文明确提供负责人、时间或交付内容时一并保留。
如果没有明确行动项，省略整个“行动项”章节。

## 会议主要内容

根据录音中的实际主题拆分三级标题，例如：

### 主题标题

完整还原相关讨论、逻辑、数据、方案、不同意见和结论。正文可以使用段落、有序列表或无序列表。

根据内容数量设置主题章节，不要为了套用格式添加没有实际内容的章节。

## 标签
输出3-5个能够从录音内容中明确提取的简短标签，使用无序列表。
无法提取有效标签时，省略整个“标签”章节。

## 硬性约束
- 关键决定：录音中明确提到的决策才写，没有则省略整个章节
- 行动项：录音中明确提到的行动项才写，没有则省略整个章节
- 所有内容使用纯 Markdown，禁止内嵌 HTML
- `##` 用于主要章节，`###` 用于具体主题
- 客观还原，去除语气词和重复口语，但保留所有实质内容
- 识别不清的原文标注为【录音模糊】
- 不推测时间、时长、负责人、决定、行动项或结论
- 不虚构数据、比例和因果关系
- 没有实际内容的字段、列表或章节直接省略
- 不输出开场说明、格式说明或结尾解释"""


def _env_int(name: str, default: int, minimum: int = 1, maximum: int = 8) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


@dataclass
class ChatConfig:
    """Chat / LLM 模型配置（对话、摘要等）

    优先级: DB > CHAT_* env > UPSTREAM_* env > 默认值
    """
    base_url: str = os.getenv("CHAT_BASE_URL") or os.getenv("UPSTREAM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
    api_key: str = os.getenv("CHAT_API_KEY") or os.getenv("UPSTREAM_API_KEY", "")
    model: str = os.getenv("CHAT_MODEL") or os.getenv("UPSTREAM_MODEL", "doubao-seed-2-1-turbo-260628")
    timeout: int = 120

    def reload_from_db(self):
        """从 DB 加载（覆盖 env vars）"""
        try:
            import db
            cfg = db.get_model_config()
            if cfg.get("chat_base_url"):
                self.base_url = cfg["chat_base_url"]
            if cfg.get("chat_api_key"):
                self.api_key = cfg["chat_api_key"]
            if cfg.get("chat_model"):
                self.model = cfg["chat_model"]
        except Exception:
            pass


@dataclass
class EmbeddingConfig:
    """Embedding 模型配置（向量化、语义检索）

    优先级: DB > EMBEDDING_* env > UPSTREAM_* env > 默认值
    """
    base_url: str = os.getenv("EMBEDDING_BASE_URL") or os.getenv("UPSTREAM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
    api_key: str = os.getenv("EMBEDDING_API_KEY") or os.getenv("UPSTREAM_API_KEY", "")
    model: str = os.getenv("EMBEDDING_MODEL") or os.getenv("UPSTREAM_EMBEDDING_MODEL", "doubao-embedding-text-240715")
    timeout: int = 120

    def reload_from_db(self):
        """从 DB 加载（覆盖 env vars）"""
        try:
            import db
            cfg = db.get_model_config()
            if cfg.get("embedding_base_url"):
                self.base_url = cfg["embedding_base_url"]
            if cfg.get("embedding_api_key"):
                self.api_key = cfg["embedding_api_key"]
            if cfg.get("embedding_model"):
                self.model = cfg["embedding_model"]
        except Exception:
            pass


# 兼容旧代码的别名
@dataclass
class UpstreamConfig:
    """[已废弃] 上游 LLM API 配置 — 请使用 ChatConfig / EmbeddingConfig"""
    @property
    def base_url(self) -> str:
        from app.config import config
        return config.chat.base_url

    @property
    def api_key(self) -> str:
        from app.config import config
        return config.chat.api_key

    @property
    def model(self) -> str:
        from app.config import config
        return config.chat.model

    @property
    def embedding_model(self) -> str:
        from app.config import config
        return config.embedding.model

    cloud_asr_model: str = os.getenv("CLOUD_ASR_MODEL", "doubao-seed-2-0-mini-260428")
    timeout: int = 120


@dataclass
class AsrConfig:
    """ASR 语音识别配置"""
    # "cloud" = 豆包极速版, "cloud_standard" = 豆包标准版（说话人分离）
    # "funasr_server" = 本地 FunASR 独立服务（8008 端口）
    mode: str = os.getenv("ASR_MODE", "cloud")
    funasr_model: str = os.getenv("FUNASR_MODEL", "sensevoice")
    funasr_device: str = os.getenv("FUNASR_DEVICE", "cpu")
    funasr_server_url: str = os.getenv("FUNASR_SERVER_URL", "http://127.0.0.1:8008")
    openai_audio_base_url: str = os.getenv("ASR_OPENAI_AUDIO_BASE_URL", "")
    openai_audio_model: str = os.getenv("ASR_OPENAI_AUDIO_MODEL", "")
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
            self._audio_dir = os.path.join(base_dir, "data", "audio")
            os.makedirs(self._audio_dir, exist_ok=True)
        return self._audio_dir

    @property
    def debug_dir(self) -> str:
        if not self._debug_dir:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            self._debug_dir = os.path.join(base_dir, "data", "debug")
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

    @property
    def system_prompt(self) -> str:
        """Return the Source-level insight prompt, preferring the admin DB value."""
        try:
            import db

            db_prompt = db.get_asr_config().get("insight_prompt", "")
            if db_prompt.strip():
                return db_prompt
        except Exception:
            pass
        if self.insight_prompt.strip():
            return self.insight_prompt
        return """你是 Vibry.AI 的单条录音洞察分析器。请识别这次录音中最值得关注的判断、机会、风险和下一步行动。

只输出纯 Markdown，不要 JSON，不要代码块。使用“核心洞察、机会分析、风险提示、行动建议”四个二级标题。
区分录音中明确表达的事实和你的推断；不要编造录音中不存在的信息；用中文简洁输出。"""


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
    def effective_model(self) -> str:
        """实际使用的模型: SUMMARY_MODEL or chat model"""
        return self.model or config.chat.model

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
        # 代码兜底；云端后台配置仍拥有最高优先级。
        return DEFAULT_SUMMARY_PROMPT

    @property
    def user_profile_text(self) -> str:
        """生成用户画像文本（注入到 system prompt）"""
        return f"""【用户背景】
姓名：{self.user_name}
角色：{self.user_role}
关注点：{self.user_context}
常用标签：{'、'.join(self.tags_list)}"""


@dataclass
class CognitionConfig:
    """Cognitive Core v2 processing and reflection settings."""
    nightly_insight_time: str = os.getenv("COGNITION_NIGHTLY_INSIGHT_TIME", "02:30")
    scheduler_enabled: bool = os.getenv("COGNITION_SCHEDULER_ENABLED", "true").lower() not in {"0", "false", "no"}
    transcription_workers: int = _env_int("COGNITION_TRANSCRIPTION_WORKERS", 2)
    minutes_workers: int = _env_int("COGNITION_MINUTES_WORKERS", 2)
    memory_workers: int = _env_int("COGNITION_MEMORY_WORKERS", 2)
    insight_workers: int = _env_int("COGNITION_INSIGHT_WORKERS", 1)


@dataclass
class ServerConfig:
    """服务配置"""
    version: str = os.getenv("SERVER_VERSION", "1.0.1")
    build_id: str = os.getenv("SERVER_BUILD_ID", "local-dev")
    host: str = os.getenv("SERVER_HOST", "0.0.0.0")
    port: int = int(os.getenv("SERVER_PORT", "9999"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    # CORS 允许的源（逗号分隔，* = 允许所有）
    cors_origins: list = field(default_factory=lambda: [
        o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()
    ])


@dataclass
class AppConfig:
    """聚合配置"""
    chat: ChatConfig = field(default_factory=ChatConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    upstream: UpstreamConfig = field(default_factory=UpstreamConfig)  # 兼容旧代码
    asr: AsrConfig = field(default_factory=AsrConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    doubao_asr: DoubaoAsrConfig = field(default_factory=DoubaoAsrConfig)
    summary: SummaryConfig = field(default_factory=SummaryConfig)
    prompt: PromptConfig = field(default_factory=PromptConfig)
    cognition: CognitionConfig = field(default_factory=CognitionConfig)
    server: ServerConfig = field(default_factory=ServerConfig)


# 全局单例
config = AppConfig()
