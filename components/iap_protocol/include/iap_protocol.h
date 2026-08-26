#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define IAP_PROTOCOL_VERSION         1U
#define IAP_PROTOCOL_SOF_FIRST       0xA5U
#define IAP_PROTOCOL_SOF_SECOND      0x5AU
#define IAP_PROTOCOL_MAX_PAYLOAD     1024U
#define IAP_PROTOCOL_HEADER_SIZE     10U
#define IAP_PROTOCOL_TRAILER_SIZE    4U
#define IAP_PROTOCOL_MAX_FRAME_SIZE  (IAP_PROTOCOL_HEADER_SIZE + \
                                      IAP_PROTOCOL_MAX_PAYLOAD + \
                                      IAP_PROTOCOL_TRAILER_SIZE)

typedef enum {
    IAP_COMMAND_HELLO = 0x01,
    IAP_COMMAND_INFO = 0x02,
    IAP_COMMAND_BEGIN = 0x10,
    IAP_COMMAND_DATA = 0x11,
    IAP_COMMAND_END = 0x12,
    IAP_COMMAND_ABORT = 0x13,
    IAP_COMMAND_BOOT = 0x20,
} iap_command_t;

typedef enum {
    IAP_STATUS_OK = 0x0000,
    IAP_STATUS_ERR_VERSION = 0x0001,
    IAP_STATUS_ERR_COMMAND = 0x0002,
    IAP_STATUS_ERR_SEQUENCE = 0x0003,
    IAP_STATUS_ERR_LENGTH = 0x0004,
    IAP_STATUS_ERR_CRC = 0x0005,
    IAP_STATUS_ERR_STATE = 0x0006,
    IAP_STATUS_ERR_OFFSET = 0x0007,
    IAP_STATUS_ERR_FLASH = 0x0008,
    IAP_STATUS_ERR_IMAGE = 0x0009,
    IAP_STATUS_ERR_HASH = 0x000A,
    IAP_STATUS_ERR_TIMEOUT = 0x000B,
    IAP_STATUS_ERR_BUSY = 0x000C,
    IAP_STATUS_ERR_INTERNAL = 0x000D,
} iap_status_t;

typedef struct {
    uint8_t version;
    uint8_t command;
    uint16_t sequence;
    uint16_t payload_length;
    uint8_t payload[IAP_PROTOCOL_MAX_PAYLOAD];
} iap_frame_t;

typedef enum {
    IAP_PARSE_NONE = 0,
    IAP_PARSE_FRAME,
    IAP_PARSE_HEADER_ERROR,
    IAP_PARSE_PAYLOAD_CRC_ERROR,
    IAP_PARSE_OVERFLOW,
} iap_parse_result_t;

typedef struct {
    size_t length;
    uint8_t buffer[IAP_PROTOCOL_MAX_FRAME_SIZE];
} iap_parser_t;

uint16_t iap_protocol_crc16(const uint8_t *data, size_t length);
uint32_t iap_protocol_crc32(const uint8_t *data, size_t length);

void iap_protocol_parser_init(iap_parser_t *parser);

iap_parse_result_t iap_protocol_parser_push(iap_parser_t *parser,
                                            uint8_t byte,
                                            iap_frame_t *frame);

bool iap_protocol_encode(const iap_frame_t *frame,
                         uint8_t *output,
                         size_t output_capacity,
                         size_t *output_length);

bool iap_protocol_self_test(void);

#ifdef __cplusplus
}
#endif
