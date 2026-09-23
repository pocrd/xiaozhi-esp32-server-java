package com.xiaozhi.ai.stt;

/**
 * STT 识别结果。
 * 情感字段仅在支持情感识别的模型下有值，其余为 null。
 *
 * <p>各字段说明：
 * <ul>
 *   <li>text - 识别文本</li>
 *   <li>emotion - 情感标签，如 happy / neutral / angry / sad 等</li>
 *   <li>emotionScore - 情感置信度（0~1）</li>
 *   <li>emotionDegree - 情感强度标签，如 weak / moderate / strong（火山引擎）</li>
 *   <li>emotionDegreeScore - 情感强度置信度（0~1）（火山引擎）</li>
 *   <li>requestId - 供应商返回的请求标识（阿里云对帐用），无则为 null</li>
 * </ul>
 */
public record SttResult(
        String text,
        String emotion,
        Double emotionScore,
        String emotionDegree,
        Double emotionDegreeScore,
        String requestId
) {

    /**
     * 仅含文本，无情感信息。
     */
    public static SttResult textOnly(String text) {
        return new SttResult(text, null, null, null, null, null);
    }

    /**
     * 含文本和情感信息（阿里云 paraformer 使用）。
     */
    public static SttResult withEmotion(String text, String emotion, Double emotionScore) {
        return new SttResult(text, emotion, emotionScore, null, null, null);
    }

    /**
     * 含文本和完整情感信息（火山引擎使用）。
     */
    public static SttResult withFullEmotion(String text, String emotion, Double emotionScore,
                                            String emotionDegree, Double emotionDegreeScore) {
        return new SttResult(text, emotion, emotionScore, emotionDegree, emotionDegreeScore, null);
    }

    /**
     * 返回携带供应商 RequestId 的副本。SttService 为跨会话共享实例，无法持有会话上下文，
     * 故通过结果实体把 RequestId 传出，由握有会话的调用方打印 RequestId 与 SessionId 的对应关系以对帐。
     */
    public SttResult withRequestId(String requestId) {
        return new SttResult(text, emotion, emotionScore, emotionDegree, emotionDegreeScore, requestId);
    }

    public boolean hasEmotion() {
        return emotion != null && !emotion.isEmpty();
    }
}
