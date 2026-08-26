#include "iap_protocol.h"

#include <string.h>

#define IAP_HEADER_CRC_INPUT_SIZE 6U

static uint16_t read_le16(const uint8_t *data)
{
    return (uint16_t)data[0] | ((uint16_t)data[1] << 8);
}

static uint32_t read_le32(const uint8_t *data)
{
    return (uint32_t)data[0]
           | ((uint32_t)data[1] << 8)
           | ((uint32_t)data[2] << 16)
           | ((uint32_t)data[3] << 24);
}

static void write_le16(uint8_t *data, uint16_t value)
{
    data[0] = (uint8_t)value;
    data[1] = (uint8_t)(value >> 8);
}

static void write_le32(uint8_t *data, uint32_t value)
{
    data[0] = (uint8_t)value;
    data[1] = (uint8_t)(value >> 8);
    data[2] = (uint8_t)(value >> 16);
    data[3] = (uint8_t)(value >> 24);
}

uint16_t iap_protocol_crc16(const uint8_t *data, size_t length)
{
    uint16_t crc = 0xFFFFU;

    for (size_t index = 0; index < length; ++index) {
        crc ^= (uint16_t)data[index] << 8;
        for (uint8_t bit = 0; bit < 8; ++bit) {
            if ((crc & 0x8000U) != 0U) {
                crc = (uint16_t)((crc << 1) ^ 0x1021U);
            } else {
                crc <<= 1;
            }
        }
    }
    return crc;
}

uint32_t iap_protocol_crc32(const uint8_t *data, size_t length)
{
    uint32_t crc = 0xFFFFFFFFU;

    for (size_t index = 0; index < length; ++index) {
        crc ^= data[index];
        for (uint8_t bit = 0; bit < 8; ++bit) {
            uint32_t mask = (uint32_t)-(int32_t)(crc & 1U);
            crc = (crc >> 1) ^ (0xEDB88320U & mask);
        }
    }
    return crc ^ 0xFFFFFFFFU;
}

void iap_protocol_parser_init(iap_parser_t *parser)
{
    if (parser != NULL) {
        parser->length = 0;
    }
}

static void parser_keep_possible_sof(iap_parser_t *parser)
{
    for (size_t index = 1; index + 1 < parser->length; ++index) {
        if (parser->buffer[index] == IAP_PROTOCOL_SOF_FIRST
                && parser->buffer[index + 1] == IAP_PROTOCOL_SOF_SECOND) {
            size_t remaining = parser->length - index;
            memmove(parser->buffer, &parser->buffer[index], remaining);
            parser->length = remaining;
            return;
        }
    }

    if (parser->length > 0
            && parser->buffer[parser->length - 1] == IAP_PROTOCOL_SOF_FIRST) {
        parser->buffer[0] = IAP_PROTOCOL_SOF_FIRST;
        parser->length = 1;
    } else {
        parser->length = 0;
    }
}

iap_parse_result_t iap_protocol_parser_push(iap_parser_t *parser,
                                            uint8_t byte,
                                            iap_frame_t *frame)
{
    if (parser == NULL || frame == NULL) {
        return IAP_PARSE_OVERFLOW;
    }

    if (parser->length == 0 && byte != IAP_PROTOCOL_SOF_FIRST) {
        return IAP_PARSE_NONE;
    }
    if (parser->length == 1) {
        if (byte == IAP_PROTOCOL_SOF_FIRST) {
            parser->buffer[0] = byte;
            return IAP_PARSE_NONE;
        }
        if (byte != IAP_PROTOCOL_SOF_SECOND) {
            parser->length = 0;
            return IAP_PARSE_NONE;
        }
    }

    if (parser->length >= sizeof(parser->buffer)) {
        parser->length = 0;
        if (byte == IAP_PROTOCOL_SOF_FIRST) {
            parser->buffer[parser->length++] = byte;
        }
        return IAP_PARSE_OVERFLOW;
    }
    parser->buffer[parser->length++] = byte;

    if (parser->length < IAP_PROTOCOL_HEADER_SIZE) {
        return IAP_PARSE_NONE;
    }

    uint16_t payload_length = read_le16(&parser->buffer[6]);
    uint16_t received_header_crc = read_le16(&parser->buffer[8]);
    uint16_t actual_header_crc = iap_protocol_crc16(
        &parser->buffer[2], IAP_HEADER_CRC_INPUT_SIZE);
    if (payload_length > IAP_PROTOCOL_MAX_PAYLOAD
            || received_header_crc != actual_header_crc) {
        parser_keep_possible_sof(parser);
        return IAP_PARSE_HEADER_ERROR;
    }

    size_t expected_length = IAP_PROTOCOL_HEADER_SIZE
                             + payload_length
                             + IAP_PROTOCOL_TRAILER_SIZE;
    if (parser->length < expected_length) {
        return IAP_PARSE_NONE;
    }

    const uint8_t *payload = &parser->buffer[IAP_PROTOCOL_HEADER_SIZE];
    uint32_t received_payload_crc = read_le32(&payload[payload_length]);
    uint32_t actual_payload_crc = iap_protocol_crc32(payload, payload_length);
    if (received_payload_crc != actual_payload_crc) {
        parser->length = 0;
        return IAP_PARSE_PAYLOAD_CRC_ERROR;
    }

    frame->version = parser->buffer[2];
    frame->command = parser->buffer[3];
    frame->sequence = read_le16(&parser->buffer[4]);
    frame->payload_length = payload_length;
    if (payload_length > 0) {
        memcpy(frame->payload, payload, payload_length);
    }
    parser->length = 0;
    return IAP_PARSE_FRAME;
}

bool iap_protocol_encode(const iap_frame_t *frame,
                         uint8_t *output,
                         size_t output_capacity,
                         size_t *output_length)
{
    if (frame == NULL || output == NULL || output_length == NULL
            || frame->payload_length > IAP_PROTOCOL_MAX_PAYLOAD) {
        return false;
    }

    size_t frame_length = IAP_PROTOCOL_HEADER_SIZE
                          + frame->payload_length
                          + IAP_PROTOCOL_TRAILER_SIZE;
    if (output_capacity < frame_length) {
        return false;
    }

    output[0] = IAP_PROTOCOL_SOF_FIRST;
    output[1] = IAP_PROTOCOL_SOF_SECOND;
    output[2] = frame->version;
    output[3] = frame->command;
    write_le16(&output[4], frame->sequence);
    write_le16(&output[6], frame->payload_length);
    write_le16(&output[8], iap_protocol_crc16(
        &output[2], IAP_HEADER_CRC_INPUT_SIZE));
    if (frame->payload_length > 0) {
        memcpy(&output[IAP_PROTOCOL_HEADER_SIZE],
               frame->payload,
               frame->payload_length);
    }
    write_le32(&output[IAP_PROTOCOL_HEADER_SIZE + frame->payload_length],
               iap_protocol_crc32(frame->payload, frame->payload_length));
    *output_length = frame_length;
    return true;
}

bool iap_protocol_self_test(void)
{
    static const uint8_t crc_text[] = "123456789";
    static const uint8_t golden_hello[] = {
        0xA5, 0x5A, 0x01, 0x01, 0x00, 0x00, 0x00,
        0x00, 0xE1, 0xE1, 0x00, 0x00, 0x00, 0x00,
    };
    uint8_t encoded[IAP_PROTOCOL_HEADER_SIZE + IAP_PROTOCOL_TRAILER_SIZE];
    size_t encoded_length = 0;
    iap_frame_t hello = {
        .version = IAP_PROTOCOL_VERSION,
        .command = IAP_COMMAND_HELLO,
        .sequence = 0,
        .payload_length = 0,
    };

    if (iap_protocol_crc16(crc_text, sizeof(crc_text) - 1) != 0x29B1U
            || iap_protocol_crc32(NULL, 0) != 0U
            || !iap_protocol_encode(&hello, encoded, sizeof(encoded),
                                    &encoded_length)
            || encoded_length != sizeof(golden_hello)
            || memcmp(encoded, golden_hello, sizeof(golden_hello)) != 0) {
        return false;
    }

    iap_parser_t parser;
    iap_frame_t decoded;
    iap_protocol_parser_init(&parser);
    iap_parse_result_t result = IAP_PARSE_NONE;
    for (size_t index = 0; index < sizeof(golden_hello); ++index) {
        result = iap_protocol_parser_push(&parser, golden_hello[index], &decoded);
    }
    return result == IAP_PARSE_FRAME
           && decoded.version == IAP_PROTOCOL_VERSION
           && decoded.command == IAP_COMMAND_HELLO
           && decoded.sequence == 0
           && decoded.payload_length == 0;
}
