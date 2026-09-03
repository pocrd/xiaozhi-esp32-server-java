package com.xiaozhi.device.config;

import java.util.HashMap;
import java.util.Map;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.context.annotation.Configuration;

import lombok.Data;

@Configuration
@ConfigurationProperties(prefix = "xiaozhi.ota")
@Data
public class OtaProperties {

    /** 固件配置：key=硬件类型(dx/yd)，value=固件信息(url, version) */
    private Map<String, FirmwareInfo> firmware = new HashMap<>();

    @Data
    public static class FirmwareInfo {
        /** 固件下载地址 */
        private String url;
        /** 固件版本号 */
        private String version;
    }
}
