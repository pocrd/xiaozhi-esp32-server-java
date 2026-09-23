package com.xiaozhi.ai.tts;

import java.nio.file.Path;

/**
 * TTS 合成结果。
 *
 * <p>各字段说明：
 * <ul>
 *   <li>path - 生成的音频文件路径，合成失败时为 null</li>
 *   <li>requestId - 供应商返回的请求标识（阿里云对帐用），不支持或失败时为 null</li>
 * </ul>
 *
 * <p>TtsService 为跨会话共享实例，无法持有会话上下文，故通过结果实体把 requestId 传出，
 * 由握有会话的调用方打印 requestId 与 SessionId 的对应关系以对帐。
 */
public record TtsResult(Path path, String requestId) {

    /**
     * 仅含音频路径、无 requestId（不支持返回 requestId 的供应商使用）。
     */
    public static TtsResult of(Path path) {
        return new TtsResult(path, null);
    }
}
