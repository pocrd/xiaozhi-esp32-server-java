package com.xiaozhi.communication.domain;

import com.fasterxml.jackson.databind.PropertyNamingStrategies;
import com.fasterxml.jackson.databind.annotation.JsonNaming;
import lombok.Data;
import lombok.EqualsAndHashCode;

@Data
@EqualsAndHashCode(callSuper = true)
@JsonNaming(PropertyNamingStrategies.SnakeCaseStrategy.class)
public final class LogMessage extends Message {
    public LogMessage() {
        super("log");
    }

    /**
     * 日志级别（如 debug / info / warn / error）
     */
    private String level;

    /**
     * 日志内容
     */
    private String message;

    /**
     * 设备端时间戳（毫秒）
     */
    private Long timestamp;
}
