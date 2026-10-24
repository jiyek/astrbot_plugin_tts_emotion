"""Make GPT-SoVITS TTS sound more emotional.

GPT-SoVITS does not expose an "emotion" switch.  Its emotion is mainly carried
by the reference audio, with temperature/speed adding nuance.  This plugin
patches the AstrBot GSV provider so each reply chooses a matching reference
audio and a small prosody profile.
"""

from __future__ import annotations

import asyncio
import contextvars
import re
import types
import uuid
from pathlib import Path

import imageio_ffmpeg
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

_CURRENT_EMOTION: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tts_emotion",
    default="",
)

# The four reference clips that ship with this GPT-SoVITS voice.
_PROMPT_TEXTS = {
    "normal1": "ただなぁ、正直に言ってしまえば私の身の回りの世話を最低限できそうな人なら。",
    "normal2": "瑞穂がここへ着いてから、それほど時間は経ってないはずだが。",
    "asking": "おいおいデザインのために聞いたのか？だとすれば約束が違うだろ？",
    "soft": "朝日の悪いところは、入り込むと私の言うことすら聞かないところだな。",
}

_EMOTION_PROFILES = {
    "neutral": {
        "ref": "normal1",
        "speed_factor": "1.0",
        "temperature": "1.0",
        "top_k": "5",
        "top_p": "1.0",
    },
    "question": {
        "ref": "asking",
        "speed_factor": "1.0",
        "temperature": "0.85",
        "top_k": "5",
        "top_p": "0.95",
    },
    "sharp": {
        "ref": "asking",
        "speed_factor": "1.06",
        "temperature": "0.9",
        "top_k": "5",
        "top_p": "0.95",
    },
    "angry": {
        "ref": "asking",
        "speed_factor": "1.18",
        "temperature": "0.95",
        "top_k": "4",
        "top_p": "0.95",
        "fragment_interval": "0.18",
    },
    "soft": {
        "ref": "soft",
        "speed_factor": "0.92",
        "temperature": "0.72",
        "top_k": "8",
        "top_p": "0.9",
    },
    "sad": {
        "ref": "soft",
        "speed_factor": "0.85",
        "temperature": "0.62",
        "top_k": "10",
        "top_p": "0.88",
    },
    "happy": {
        "ref": "normal2",
        "speed_factor": "1.20",
        "temperature": "0.82",
        "top_k": "8",
        "top_p": "0.95",
        "fragment_interval": "0.2",
    },
}

_ANGRY_WORDS = (
    "生气",
    "愤怒",
    "火大",
    "受够",
    "够了",
    "滚",
    "闭嘴",
    "怒",
    "キレ",
    "怒る",
    "うるさい",
    "黙れ",
)
_SHARP_WORDS = (
    "哼",
    "切",
    "嘁",
    "少来",
    "别碰",
    "别闹",
    "烦",
    "讨厌",
    "笨蛋",
    "蠢",
    "谁要",
    "凭什么",
    "可笑",
    "おい",
    "ふん",
    "別に",
    "嫌い",
)
_SAD_WORDS = (
    "难过",
    "伤心",
    "想哭",
    "舍不得",
    "对不起",
    "抱歉",
    "遗憾",
    "寂寞",
    "孤单",
    "悲しい",
    "寂しい",
    "ごめん",
    "泣",
)
_SOFT_WORDS = (
    "晚安",
    "谢谢",
    "喜欢",
    "抱",
    "陪",
    "辛苦",
    "小心",
    "早点",
    "温柔",
    "安心",
    "好き",
    "ありがとう",
    "おやすみ",
    "大丈夫",
)
_HAPPY_WORDS = (
    "哈哈",
    "开心",
    "高兴",
    "太好了",
    "好耶",
    "笑",
    "嬉しい",
    "楽しい",
    "やった",
)
_QUESTION_WORDS = (
    "?",
    "？",
    "吗",
    "呢",
    "怎么",
    "为什么",
    "谁",
    "什么",
    "哪",
    "如何",
    "なぜ",
    "どう",
    "なに",
)


class TtsEmotionPlugin(Star):
    """Patch GSV TTS requests with emotion-aware reference audio and prosody."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.enabled = self._as_bool(config.get("enabled", True))
        self.use_reference_audio = self._as_bool(
            config.get("use_reference_audio", True)
        )
        self.use_prosody = self._as_bool(config.get("use_prosody", True))
        self.use_user_context = self._as_bool(
            config.get("use_user_context", True)
        )
        self.auto_text_language = self._as_bool(
            config.get("auto_text_language", True)
        )
        self.post_process_speed = self._as_bool(
            config.get("post_process_speed", True)
        )
        self._ffmpeg = self._find_ffmpeg()
        self._patched: list[tuple[object, object, object]] = []

    @staticmethod
    def _as_bool(value) -> bool:
        if isinstance(value, str):
            return value.strip().lower() not in {"false", "0", "no", "off"}
        return bool(value)

    @staticmethod
    def _find_ffmpeg() -> str | None:
        try:
            path = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:  # noqa: BLE001 - optional speed post-processing
            return None
        return path if Path(path).is_file() else None

    async def _apply_speed(self, source: str, speed: float) -> str:
        """Apply time-stretch with atempo when the API ignores speed_factor."""

        if not self._ffmpeg or abs(speed - 1.0) < 0.01:
            return source
        speed = min(2.0, max(0.5, speed))
        source_path = Path(source)
        if not source_path.is_file():
            return source
        output = source_path.with_name(
            f"{source_path.stem}_sp{int(speed * 100)}_{uuid.uuid4().hex[:6]}.wav"
        )
        command = [
            self._ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(source_path),
            "-filter:a",
            f"atempo={speed:.3f}",
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(output),
        ]
        log_path = output.with_suffix(".ffmpeg.log")
        try:
            log_file = log_path.open("wb")
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=log_file,
            )
        except OSError as exc:
            try:
                log_file.close()
            except (NameError, OSError):
                pass
            output.unlink(missing_ok=True)
            log_path.unlink(missing_ok=True)
            logger.warning("[TtsEmotion] speed post-process start failed: %s", exc)
            return source
        try:
            await asyncio.wait_for(process.wait(), timeout=30)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            output.unlink(missing_ok=True)
            logger.warning("[TtsEmotion] speed post-process timed out")
            return source
        finally:
            log_file.close()
        log_path.unlink(missing_ok=True)
        if process.returncode != 0 or not output.is_file():
            output.unlink(missing_ok=True)
            logger.warning("[TtsEmotion] speed post-process failed")
            return source
        # The original is a temporary TTS file; remove it after replacement.
        try:
            source_path.unlink(missing_ok=True)
        except OSError:
            pass
        return str(output)

    @staticmethod
    def _detect_language(text: str, fallback: str = "ja") -> str:
        """Choose text_lang for mixed Chinese/Japanese replies."""

        text = str(text or "")
        if re.search(r"[\u3040-\u30ff]", text):
            return "ja"
        if re.search(r"[\u4e00-\u9fff]", text):
            return "zh"
        if re.search(r"[A-Za-z]", text):
            return "en"
        return fallback

    @staticmethod
    def _detect_emotion(text: str) -> str:
        """Classify a short reply segment with bilingual keyword cues."""

        text = str(text or "").casefold()
        if not text.strip():
            return "neutral"
        if any(word in text for word in _ANGRY_WORDS):
            return "angry"
        if any(word in text for word in _SHARP_WORDS):
            return "sharp"
        if any(word in text for word in _SAD_WORDS):
            return "sad"
        if any(word in text for word in _SOFT_WORDS):
            return "soft"
        if any(word in text for word in _HAPPY_WORDS):
            return "happy"
        if "?" in text or "？" in text or any(
            word in text for word in _QUESTION_WORDS
        ):
            return "question"
        return "neutral"

    def _build_profiles(self, provider) -> dict[str, dict[str, str]]:
        """Create per-emotion parameter overlays from the provider defaults."""

        defaults = dict(getattr(provider, "default_params", {}) or {})
        ref_value = str(defaults.get("ref_audio_path", "") or "").strip()
        if not ref_value:
            return {}
        ref_dir = Path(ref_value).expanduser().parent
        prompt_lang = str(defaults.get("prompt_lang", "ja") or "ja")

        profiles: dict[str, dict[str, str]] = {}
        for emotion, profile in _EMOTION_PROFILES.items():
            params: dict[str, str] = {}
            if self.use_reference_audio:
                ref_name = profile["ref"]
                ref_path = ref_dir / f"{ref_name}.wav"
                if ref_path.is_file():
                    params.update(
                        {
                            "ref_audio_path": str(ref_path),
                            "prompt_text": _PROMPT_TEXTS[ref_name],
                            "prompt_lang": prompt_lang,
                        }
                    )
                else:
                    logger.warning(
                        "[TtsEmotion] reference audio missing for %s: %s",
                        emotion,
                        ref_path,
                    )
            if self.use_prosody:
                params.update(
                    {
                        "speed_factor": profile["speed_factor"],
                        "temperature": profile["temperature"],
                        "top_k": profile["top_k"],
                        "top_p": profile["top_p"],
                    }
                )
                if "fragment_interval" in profile:
                    params["fragment_interval"] = profile["fragment_interval"]
            if params:
                profiles[emotion] = params
        return profiles

    def _patch_provider(self, provider) -> None:
        if getattr(provider, "_tts_emotion_patched", False):
            return
        original_build = getattr(provider, "build_synthesis_params", None)
        if not callable(original_build):
            return
        profiles = self._build_profiles(provider)
        if not profiles:
            return

        plugin = self

        def build_with_emotion(self, text: str) -> dict:
            params = original_build(text)
            if not plugin.enabled:
                return params
            if plugin.auto_text_language:
                params["text_lang"] = plugin._detect_language(
                    text,
                    str(params.get("text_lang", "ja") or "ja"),
                )
            emotion = _CURRENT_EMOTION.get("") or plugin._detect_emotion(text)
            overlay = profiles.get(emotion) or profiles.get("neutral")
            if overlay:
                params.update(overlay)
            if plugin.post_process_speed and plugin._ffmpeg:
                # The local v2 API ignores speed_factor; apply atempo after
                # synthesis instead, so do not request speed changes twice.
                params["speed_factor"] = "1.0"
            return params

        provider.build_synthesis_params = types.MethodType(
            build_with_emotion,
            provider,
        )
        original_get_audio = getattr(provider, "get_audio", None)

        if plugin.post_process_speed and callable(original_get_audio):

            async def get_audio_with_speed(self, text: str) -> str:
                path = await original_get_audio(text)
                if not plugin.enabled or not plugin.use_prosody:
                    return path
                emotion = _CURRENT_EMOTION.get("") or plugin._detect_emotion(
                    text
                )
                profile = _EMOTION_PROFILES.get(
                    emotion,
                    _EMOTION_PROFILES["neutral"],
                )
                try:
                    speed = float(profile["speed_factor"])
                except (TypeError, ValueError):
                    speed = 1.0
                return await plugin._apply_speed(path, speed)

            provider.get_audio = types.MethodType(
                get_audio_with_speed,
                provider,
            )

        provider._tts_emotion_patched = True
        provider._tts_emotion_original_build = original_build
        provider._tts_emotion_original_get_audio = original_get_audio
        self._patched.append((provider, original_build, original_get_audio))
        logger.info(
            "[TtsEmotion] patched provider %s with %d emotion profiles",
            type(provider).__name__,
            len(profiles),
        )

    async def _ensure_patched(self) -> None:
        if not getattr(self, "context", None):
            return
        try:
            providers = self.context.get_all_tts_providers()
        except Exception as exc:  # noqa: BLE001 - provider may not be ready
            logger.debug("[TtsEmotion] TTS providers unavailable: %s", exc)
            return
        for provider in providers:
            if type(provider).__name__ != "ProviderGSVTTS":
                continue
            self._patch_provider(provider)

    async def initialize(self) -> None:
        await self._ensure_patched()

    async def terminate(self) -> None:
        for provider, original_build, original_get_audio in self._patched:
            try:
                provider.build_synthesis_params = original_build
                if callable(original_get_audio):
                    provider.get_audio = original_get_audio
                provider._tts_emotion_patched = False
            except Exception:  # noqa: BLE001 - best-effort restore
                logger.debug("[TtsEmotion] failed to restore provider")
        self._patched.clear()

    @filter.on_decorating_result(priority=100)
    async def choose_emotion(self, event: AstrMessageEvent) -> None:
        """Set the emotion for the whole reply before AstrBot runs TTS."""

        await self._ensure_patched()
        if not self.enabled:
            _CURRENT_EMOTION.set("")
            return
        result = event.get_result()
        if result is None:
            _CURRENT_EMOTION.set("")
            return
        reply_text = result.get_plain_text()
        if not reply_text:
            _CURRENT_EMOTION.set("")
            return
        emotion = self._detect_emotion(reply_text)
        if emotion == "neutral" and self.use_user_context:
            # A neutral reply such as "嗯。" should still sound soft when the
            # user was sad, or sharp when the user was provoking the bot.
            emotion = self._detect_emotion(event.get_message_str())
        _CURRENT_EMOTION.set(emotion)
        logger.debug("[TtsEmotion] selected emotion=%s", emotion)
