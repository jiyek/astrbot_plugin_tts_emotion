# TTS 情绪

让 Luna 的 GPT-SoVITS TTS 根据回复内容切换语气。

## 先说限制

GPT-SoVITS **没有真正的 `emotion="angry"` 参数**。它的情绪主要由 **参考音频（ref_audio_path）** 决定，语速、temperature、top_k、top_p 只能做辅助。

当前语音包只有 4 段参考音频：

```text
normal1.wav   平静
normal2.wav   平静/略轻快
asking.wav    质问、带攻击性
soft.wav      柔和、低声
```

因此本插件能做的是：

- 平静/普通 → `normal1`
- 疑问/质问 → `asking`
- 毒舌、生气 → `asking` + 更快语速 + 更高 temperature
- 温柔、亲近 → `soft` + 更慢语速 + 更低 temperature
- 难过、低落 → `soft` + 明显更慢 + 更低 temperature
- 开心 → `normal2` + 稍快语速

这不是“真正演出来”的愤怒或哭泣。想要更明显的情绪，需要补充对应的参考音频（例如生气的台词、哭腔、笑声），再在插件里加 profile。

## 工作原理

AstrBot 的 GPT-SoVITS provider 原本：

```text
build_synthesis_params(text) -> 默认参数 + text
```

插件在加载时找到 `ProviderGSVTTS`，包装它的 `build_synthesis_params`：

1. `on_decorating_result` 先读取整条回复，判断整体情绪，写入当前请求的 ContextVar。
2. 如果回复本身没有明显情绪，再用当前用户消息补充判断（例如用户说“难过”，bot 只回“嗯”）。
3. provider 真正请求 TTS 时，包装方法按情绪替换 `ref_audio_path`、`prompt_text`，并覆盖温度参数。
4. 当前 9881 的 API 实际会忽略 `speed_factor`，所以合成后用 ffmpeg `atempo` 对 WAV 做真实变速，再把新路径返回给 AstrBot。
5. 同时按文本语言设置 `text_lang`，避免中文回复用日文音素朗读。
6. 每个回复任务有自己的 ContextVar，不会把 A 消息的情绪串到 B 消息。

情绪判断使用中英/日文关键词和标点，例如：

```text
哼、少来、别碰、烦、讨厌        -> sharp
滚、闭嘴、受够、生气              -> angry
……、晚安、谢谢、喜欢、抱、陪      -> soft
难过、舍不得、对不起、寂寞        -> sad
哈哈、开心、太好了                -> happy
？、怎么、为什么、谁、哪          -> question
```

## 配置

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关 |
| `use_reference_audio` | `true` | 是否切换 4 个参考音频 |
| `use_prosody` | `true` | 是否调整语速/温度/top_k/top_p |
| `use_user_context` | `true` | 回复本身很中性时，用用户消息的情绪补充判断 |
| `auto_text_language` | `true` | 中文回复自动用 `text_lang=zh`，日文用 `ja`，英文用 `en` |
| `post_process_speed` | `true` | 当前 9881 的 v2 API 会忽略 `speed_factor`，开启后用 ffmpeg `atempo` 实际变速 |

## 注意

- 只处理 `ProviderGSVTTS`（`gsv_tts_selfhost`），其他 TTS 提供商不会改动。
- 参考音频路径从 provider 的 `gsv_default_parms.ref_audio_path` 所在目录自动推导，文件名必须是 `normal1.wav`、`normal2.wav`、`asking.wav`、`soft.wav`。
- 如果参考音频缺失，会跳过对应情绪，回退到默认参数。
- 插件卸载时会恢复 provider 的原方法；如果重载后没有生效，检查日志中的 `[TtsEmotion]`。
- 想明显增强情绪，最有效的办法仍然是增加高质量的情绪参考音频，而不是只调 temperature。
