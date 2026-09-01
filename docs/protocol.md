# 串口 IAP 文件传输协议 v1

## 1. 物理层

| 参数 | 值 |
|---|---|
| 波特率 | 115200 |
| 数据位 | 8 |
| 校验位 | 无 |
| 停止位 | 1 |
| 流控 | 无 |
| 字节序 | little-endian |

## 2. 帧格式

```
Offset  Size  Field
0       2     SOF = 0xA5 0x5A
2       1     Protocol version
3       1     Command
4       2     Sequence
6       2     Payload length (N)
8       2     Header CRC16
10      N     Payload
10+N    4     Payload CRC32
```

- Header CRC16 覆盖 offset 2..7（version, command, sequence, payload length），CRC-16/CCITT-FALSE（poly=0x1021, init=0xFFFF）。
- Payload CRC32 为 IEEE CRC-32，空 payload 时值为 0。
- Payload length 上限 1024 字节。
- 无 EOF 标记，由长度字段界定帧结束。

接收端先校验 Header CRC 再接收 payload。Header CRC 不匹配或长度越界时从下一个 `A5 5A` 重新同步。

## 3. 命令表

| 值 | 名称 | 方向 | 说明 |
|---|---|---|---|
| `0x01` | HELLO | H→D | 建立会话 |
| `0x02` | INFO | H→D | 查询设备模式、版本、分区 |
| `0x10` | BEGIN | H→D | 声明镜像大小和摘要，擦除目标分区 |
| `0x11` | DATA | H→D | 传输镜像数据块 |
| `0x12` | END | H→D | 请求校验并提交 |
| `0x13` | ABORT | H→D | 放弃当前会话 |
| `0x20` | BOOT | H→D | 退出 IAP，启动当前应用 |

响应命令值 = 请求命令值 | 0x80。

## 4. 序号

HELLO 序号为 0。每次成功响应后序号 +1，uint16_t 回绕。同一时刻只有一个未确认请求（stop-and-wait）。

超时未收到响应时原样重发。设备识别重复序号后重发缓存的响应，不重复写入。

## 5. 响应负载

```
Offset  Size  Field
0       2     status
2       2     expected_sequence
4       4     next_offset
8       1     detail_length (≤96)
9       N     detail (UTF-8)
```

HELLO/INFO 的 detail 返回描述文本：

```
serial-iap-bootloader;protocol=1;chip=esp32s3
mode=bootloader;inactive=ota_1;flash-encryption=off
```

### 状态码

| 值 | 名称 |
|---|---|
| `0x0000` | OK |
| `0x0001` | ERR_VERSION |
| `0x0002` | ERR_COMMAND |
| `0x0003` | ERR_SEQUENCE |
| `0x0004` | ERR_LENGTH |
| `0x0005` | ERR_CRC |
| `0x0006` | ERR_STATE |
| `0x0007` | ERR_OFFSET |
| `0x0008` | ERR_FLASH |
| `0x0009` | ERR_IMAGE |
| `0x000A` | ERR_HASH |
| `0x000B` | ERR_TIMEOUT |
| `0x000C` | ERR_BUSY |
| `0x000D` | ERR_INTERNAL |

未通过帧级校验的数据被静默丢弃，由上位机超时重传。

## 6. BEGIN

```
Offset  Size  Field
0       4     image_size
4       32    image_sha256
36      1     version_length (≤32)
37      N     version (UTF-8)
```

image_sha256 为整个 .bin 文件的 SHA-256。成功后设备擦除非活动 OTA 分区所需扇区，next_offset 置 0。

## 7. DATA

```
Offset  Size  Field
0       4     offset
4       N     data (1..1020 字节)
```

offset 须等于上一响应的 next_offset。非末块数据长度须为 4 的倍数，末块任意。上位机默认每块 1020 字节。

## 8. END

无 payload。设备依次：

1. 检查 next_offset == image_size
2. Flash 回读计算 SHA-256，与 BEGIN 声明的比较
3. 验证 ESP-IDF 镜像格式（magic, segment, checksum, chip）
4. 验证镜像长度 == image_size
5. 写 otadata 切换启动分区
6. 返回响应后复位

步骤 1–4 任一失败则不执行步骤 5。

## 9. IAP 入口

设备正常复位后 Bootloader 等待 800 ms。收到合法 HELLO 进入 IAP 会话，超时则正常启动应用。

会话空闲 10 s 自动退出，不改变启动选择。

## 10. 安全

CRC 和 SHA-256 检测传输错误，不做身份认证。v1 不支持 Secure Boot 和 Flash Encryption。检测到 Flash Encryption 已启用时拒绝 BEGIN。
