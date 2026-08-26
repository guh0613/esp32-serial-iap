# ESP32-S3 串口 IAP

基于 ESP-IDF v5.5.5 的 ESP32-S3 串口固件升级系统。自定义二级 Bootloader 在启动阶段通过 UART 接收应用镜像，写入非活动 OTA 分区，校验通过后切换启动分区并重启。升级过程中任何阶段失败均不改变当前启动选择。

## 工程结构

```
.
├── bootloader_components/      自定义二级 Bootloader
│   ├── main/                   入口、IAP 状态机
│   └── iap_protocol/           引用共享协议源码
├── components/iap_protocol/    C 协议编解码（Bootloader 和应用共用）
├── main/                       应用固件，含应用态 IAP 服务
├── host/                       Python 上位机
│   ├── iap_tool.py             CLI 入口
│   ├── protocol.py             协议帧
│   ├── transport.py            串口传输
│   ├── hardware_validation.py  硬件故障测试
│   └── tests/                  单元测试
└── docs/
    ├── protocol.md             文件传输协议
    └── bootloader_interface.md Bootloader 接口说明
```

## 升级流程

```
上位机 DTR/RTS 复位设备
        │
自定义 Bootloader 等待 HELLO（800 ms）
        │                     │
      收到                   超时
        │                     │
        ▼                     ▼
  选择非活动 OTA 槽      正常启动应用
        │
  BEGIN → DATA × N → END
        │                │
      失败             通过
        │                │
  不改 otadata       提交并重启
```

## 环境

- ESP-IDF v5.5.5
- 目标芯片 ESP32-S3（`idf.py set-target esp32s3`）
- Python 3，依赖 pyserial（ESP-IDF 环境自带，或 `pip install -r host/requirements.txt`）

每个终端会话需要先激活 ESP-IDF 环境再执行 `idf.py` 命令。

## 构建与烧录

```bash
idf.py build
```

首次需要通过 `idf.py flash` 完整写入 Bootloader、分区表和 factory 应用：

```bash
python -B -m host.iap_tool ports          # 查看串口
idf.py -p <PORT> flash                    # 首次完整烧录
```

后续升级通过上位机工具完成，不再需要 `idf.py flash`。

## 上位机

```bash
python -B -m host.iap_tool ports                              # 列出串口
python -B -m host.iap_tool --port <PORT> info                 # 查询 Bootloader
python -B -m host.iap_tool --port <PORT> flash build/serial_iap.bin --version 1.2.0
python -B -m host.iap_tool --port <PORT> boot                 # 跳过升级直接启动
python -B -m host.iap_tool --port <PORT> --no-reset info      # 查询运行中的应用
```

使用上位机前需关闭 `idf.py monitor` 等占用同一串口的程序。

## 测试

```bash
# 单元测试（不需要硬件）
python -B -m unittest discover -s host/tests -v

# 硬件故障测试（会擦写非活动 OTA 槽，需确认）
python -B -m host.hardware_validation --port <PORT> --confirm-inactive-write protocol-errors
python -B -m host.hardware_validation --port <PORT> --confirm-inactive-write bad-hash build/serial_iap.bin
python -B -m host.hardware_validation --port <PORT> --confirm-inactive-write interrupted-transfer build/serial_iap.bin
```

## 分区表

```
nvs         data  nvs      0x11000   24 KB
otadata     data  ota      0x17000    8 KB
phy_init    data  phy      0x19000    4 KB
iap_state   data  0x40     0x1A000    4 KB   预留
factory     app   factory  0x20000    1 MB
ota_0       app   ota_0    0x120000   7 MB
ota_1       app   ota_1    0x820000   7 MB
```

分区表偏移 `0x10000`，为自定义 Bootloader 预留 64 KB。

## 文档

- [文件传输协议](docs/protocol.md) — 帧格式、命令、校验、状态机
- [Bootloader 接口说明](docs/bootloader_interface.md) — 启动流程、Flash 访问规则、模块接口
