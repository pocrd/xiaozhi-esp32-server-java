package com.xiaozhi.common;

import java.util.concurrent.TimeUnit;
import java.util.function.Supplier;

import org.redisson.api.RLock;
import org.redisson.api.RedissonClient;
import org.springframework.stereotype.Component;

import jakarta.annotation.Resource;
import lombok.extern.slf4j.Slf4j;
/**
 * 缓存助手类
 * 提供带分布式锁的缓存查询,防止缓存击穿
 *
 * @author Joey
 */
@Slf4j
@Component
public class CacheHelper {

    @Resource
    private RedissonClient redissonClient;

    /**
     * 带分布式锁的缓存查询
     * 防止缓存击穿 - 当缓存失效时,只有一个请求去查询数据库
     *
     * @param lockKey 锁的key
     * @param cacheGetter 从缓存获取数据的函数
     * @param dbGetter 从数据库获取数据的函数
     * @param <T> 数据类型
     * @return 数据
     */
    public <T> T getWithLock(String lockKey, Supplier<T> cacheGetter, Supplier<T> dbGetter) {
        // 1. 先尝试从缓存获取
        T cached = cacheGetter.get();
        if (cached != null) {
            return cached;
        }

        // 2. 缓存未命中,使用分布式锁
        RLock lock = redissonClient.getLock("lock:" + lockKey);

        try {
            // 尝试获取锁,最多等待3秒,锁10秒后自动释放
            if (lock.tryLock(3, 10, TimeUnit.SECONDS)) {
                try {
                    // 3. 双重检查,避免重复查询数据库
                    cached = cacheGetter.get();
                    if (cached != null) {
                        log.debug("获取锁后从缓存命中: {}", lockKey);
                        return cached;
                    }

                    // 4. 查询数据库
                    log.debug("从数据库查询: {}", lockKey);
                    T result = dbGetter.get();

                    // 5. 结果会通过@Cacheable自动写入缓存
                    return result;

                } finally {
                    safeUnlock(lock, lockKey);
                }
            } else {
                // 获取锁失败,直接查询数据库(降级策略)
                log.warn("获取锁超时,直接查询数据库: {}", lockKey);
                return dbGetter.get();
            }

        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            log.error("获取锁被中断: {}", lockKey, e);
            // 降级: 直接查询数据库
            return dbGetter.get();
        } catch (Exception e) {
            log.error("分布式锁异常: {}", lockKey, e);
            // 降级: 直接查询数据库
            return dbGetter.get();
        }
    }

    /**
     * 简化版 - 带分布式锁的操作
     *
     * @param lockKey 锁的key
     * @param supplier 需要执行的操作
     * @param <T> 返回类型
     * @return 操作结果
     */
    public <T> T executeWithLock(String lockKey, Supplier<T> supplier) {
        RLock lock = redissonClient.getLock("lock:" + lockKey);

        try {
            if (lock.tryLock(3, 10, TimeUnit.SECONDS)) {
                try {
                    return supplier.get();
                } finally {
                    safeUnlock(lock, lockKey);
                }
            } else {
                log.warn("获取锁超时: {}", lockKey);
                return null;
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            log.error("获取锁被中断: {}", lockKey, e);
            return null;
        } catch (Exception e) {
            log.error("执行带锁操作异常: {}", lockKey, e);
            return null;
        }
    }

    /**
     * 安全释放分布式锁
     * <p>
     * 仅在锁仍由当前线程持有时才执行 unlock,避免租约到期或 Redis 断连时
     * 抛出 {@link IllegalMonitorStateException}。释放过程中的任何异常都只记录告警,
     * 不向外传播,以免污染主流程(如导致重复查询数据库)。
     *
     * @param lock    待释放的锁
     * @param lockKey 锁的key(用于日志)
     */
    private void safeUnlock(RLock lock, String lockKey) {
        try {
            if (lock.isHeldByCurrentThread()) {
                lock.unlock();
            }
        } catch (Exception e) {
            log.warn("释放分布式锁失败(已忽略): {}", lockKey, e);
        }
    }
}
