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
 * 通过 Redis 维护每台设备每月的对话轮次，用于限额控制：
 * 每轮对话提交语音合成前计数 +1，连上却不说话、还没开口就被打断的轮次不占额度。
 * 额度是否用尽在连接建立和每轮对话开始前各判定一次。
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
     * 每月对话上限，默认 300；配置为 0 或负数表示不限额
     */
    @Value("${xiaozhi.dialogue-limit.monthly-max:300}")
    private int monthlyMax;

    /**
     * 递增设备当月对话计数，每轮对话调用一次。
     *
     * @param deviceId 设备 ID
     */
    public void increment(String deviceId) {
        if (monthlyMax <= 0) {
            return;
        }
        String key = buildKey(deviceId);
        try {
            Long count = stringRedisTemplate.opsForValue().increment(key);
            if (count != null && count == 1) {
                // 首次计数，设置过期时间
                stringRedisTemplate.expire(key, KEY_TTL);
            }
            log.debug("设备月度对话计数 - DeviceId: {}, Count: {}, Max: {}", deviceId, count, monthlyMax);
        } catch (Exception e) {
            // Redis 异常时放行，不影响正常使用
            log.error("设备对话计数异常，已放行 - DeviceId: {}", deviceId, e);
        }
    }

    /**
     * 只读判断设备当月额度是否已用尽，不递增计数。
     * <p>
     * 连接建立与每轮对话开始前调用，把额度已耗尽的设备挡在对话之外。
     *
     * @param deviceId 设备 ID
     * @return true 表示本月额度已用尽
     */
    public boolean isExhausted(String deviceId) {
        if (monthlyMax <= 0) {
            return false;
        }
        try {
            boolean exhausted = getCount(deviceId) >= monthlyMax;
            if (exhausted) {
                log.info("设备月度对话额度已用尽 - DeviceId: {}, Max: {}", deviceId, monthlyMax);
            }
            return exhausted;
        } catch (Exception e) {
            // Redis 异常时放行，不影响正常使用
            log.error("查询设备对话计数异常，已放行 - DeviceId: {}", deviceId, e);
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
