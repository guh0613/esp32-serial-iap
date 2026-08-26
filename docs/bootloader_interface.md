# Bootloader 接口说明

## 1. 启动流程

```
ROM Bootloader
      │
二级 Bootloader 初始化（bootloader_init, 加载分区表）
      │
      ├── 800 ms 内收到 HELLO → IAP 会话
      │
      └── 超时 → 按 otadata 选择应用并加载
```

IAP 会话结束后（BOOT / 超时 / ABORT），Bootloader 重新读取 otadata 再选择应用，不沿用会话前的选择。

END 验证并提交成功后直接复位，不返回。

## 2. UART

UART0，115200 8N1，无流控。与 Bootloader 日志共用同一通道，上位机解析器需从文本噪声中定位 `A5 5A` 帧头。

复位时序：DTR=false（GPIO0 高）、RTS=true 100 ms 后恢复（EN 脉冲），50 ms 后发送 HELLO。

## 3. Flash 访问

| 分区 | 访问 |
|---|---|
| otadata | 读；提交时写 |
| ota_0 / ota_1 | 非活动时可擦写 |
| factory | 只读 |
| iap_state | 预留 |

Bootloader 根据当前启动索引选择目标：

| 当前启动 | BEGIN 写入 |
|---|---|
| factory | ota_0 |
| ota_0 | ota_1 |
| ota_1 | ota_0 |

上位机不指定 Flash 地址。BEGIN 按 image_size 擦除所需扇区。

## 4. 接口

Bootloader 入口 (`bootloader_components/main/bootloader_start.c`) 调用：

```c
void iap_bootloader_run(const bootloader_state_t *bootloader_state,
                        int current_boot_index);
```

前置条件：`bootloader_init()` 完成，分区表已加载。

共享协议 (`components/iap_protocol/include/iap_protocol.h`)：

```c
void     iap_protocol_parser_init(iap_parser_t *parser);
iap_parse_result_t iap_protocol_parser_push(iap_parser_t *parser,
                                            uint8_t byte,
                                            iap_frame_t *frame);
bool     iap_protocol_encode(const iap_frame_t *frame,
                              uint8_t *output, size_t capacity,
                              size_t *length);
uint16_t iap_protocol_crc16(const uint8_t *data, size_t length);
uint32_t iap_protocol_crc32(const uint8_t *data, size_t length);
bool     iap_protocol_self_test(void);
```

IAP 状态机 (`iap_bootloader.c`) 为内部实现，不对外暴露。无 FreeRTOS 依赖，无动态分配，不使用 PSRAM。

## 5. 与应用的边界

Bootloader 和应用通过以下机制协作，不需要链接对方的符号：

- ESP-IDF 应用镜像格式
- 共用的分区表
- otadata 启动选择
- 共用的协议编解码组件 (`iap_protocol`)

应用态 IAP 服务 (`main/iap_service.c`) 使用 `esp_ota_*` API 实现相同协议，在应用运行期间也可接收升级。

## 6. 资源

- IAP 上下文约 6272 字节静态分配（接收帧 + 响应帧 + 编码缓冲 + 1024 字节写缓冲）
- 非末块 DATA 须 4 字节对齐，末块以 0xFF 补齐后写入
- 擦除和校验期间主动喂 RTC watchdog

## 7. 版本兼容

协议主版本不匹配时拒绝升级（ERR_VERSION）。同一主版本内只增加可忽略字段。v1 未启用 rollback。
