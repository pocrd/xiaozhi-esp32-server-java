package com.xiaozhi.communication.common;

import java.time.Duration;
import java.time.YearMonth;
import java.time.format.DateTimeFormatter;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

import jakarta.annotation.Resource;
import lombok.extern.slf4j.Slf4j;

/**
 * 设备月度对话计数器。
 * <p>
 * 通过 Redis 维护每台设备每月的会话次数，用于限额控制：
 * 每次 WebSocket 连接建立（即创建一个 ChatSession）计数 +1，
 * 当月超过配置上限后拒绝后续对话。
 * <p>
 * Redis key 格式：{@code xiaozhi:dialogue:count:{deviceId}:{yyyy-MM}}
 */
@Slf4j
@Component
public class DeviceDialogueCounter {

    private static final String KEY_PREFIX = "xiaozhi:dialogue:count:";
    private static final DateTimeFormatter MONTH_FMT = DateTimeFormatter.ofPattern("yyyy-MM");

    /**
     * key 过期时间：比一个月稍长，确保跨月后旧 key 自动清理
     */
    private static final Duration KEY_TTL = Duration.ofDays(40);

    @Resource
    private StringRedisTemplate stringRedisTemplate;

    /**
     * 每月对话上限，默认 300
     */
    @Value("${xiaozhi.dialogue-limit.monthly-max:300}")
    private int monthlyMax;

    /**
     * 递增设备当月对话计数并判断是否超限。
     *
     * @param deviceId 设备 ID
     * @return true 表示已超限，应拒绝对话
     */
    public boolean incrementAndCheck(String deviceId) {
        String key = buildKey(deviceId);
        try {
            Long count = stringRedisTemplate.opsForValue().increment(key);
            if (count != null && count == 1) {
                // 首次计数，设置过期时间
                stringRedisTemplate.expire(key, KEY_TTL);
            }
            boolean exceeded = count != null && count > monthlyMax;
            if (exceeded) {
                log.info("设备月度对话超限 - DeviceId: {}, Count: {}, Max: {}", deviceId, count, monthlyMax);
            }
            return exceeded;
        } catch (Exception e) {
            // Redis 异常时放行，不影响正常使用
            log.error("设备对话计数异常，已放行 - DeviceId: {}", deviceId, e);
            return false;
        }
    }

    /**
     * 查询设备当月对话次数（不递增）
     */
    public long getCount(String deviceId) {
        String key = buildKey(deviceId);
        String value = stringRedisTemplate.opsForValue().get(key);
        return value != null ? Long.parseLong(value) : 0;
    }

    private String buildKey(String deviceId) {
        String month = YearMonth.now().format(MONTH_FMT);
        return KEY_PREFIX + deviceId + ":" + month;
    }
}
