#include "iap_bootloader.h"

#include <inttypes.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "bootloader_common.h"
#include "bootloader_flash_priv.h"
#include "bootloader_utility.h"
#include "esp_flash_encrypt.h"
#include "esp_image_format.h"
#include "esp_log.h"
#include "esp_rom_sys.h"
#include "esp_rom_uart.h"
#include "hal/wdt_hal.h"
#include "iap_protocol.h"

#define IAP_ENTRY_TIMEOUT_MS           800U
#define IAP_SESSION_TIMEOUT_MS         10000U
#define IAP_POLL_INTERVAL_US           100U
#define IAP_POLLS_PER_MS               (1000U / IAP_POLL_INTERVAL_US)
#define IAP_WDT_FEED_POLLS             1000U
#define IAP_RESPONSE_PREFIX_SIZE       9U
#define IAP_RESPONSE_MAX_DETAIL        96U
#define IAP_BEGIN_PREFIX_SIZE          37U
#define IAP_BEGIN_MAX_VERSION          32U
#define IAP_DATA_OFFSET_SIZE           4U
#define IAP_SHA256_SIZE                32U
#define IAP_FLASH_BLOCK_SIZE           0x10000U
#define IAP_WRITE_ALIGNMENT            4U

static const char *TAG = "iap_boot";

typedef enum {
    SESSION_CONTINUE = 0,
    SESSION_BOOT,
    SESSION_RESET,
} session_action_t;

typedef struct {
    iap_parser_t parser;
    iap_frame_t received_frame;
    iap_frame_t response_frame;
    uint8_t encoded_response[IAP_PROTOCOL_MAX_FRAME_SIZE];
    uint8_t last_response[IAP_PROTOCOL_MAX_FRAME_SIZE];
    uint32_t aligned_write_buffer[IAP_PROTOCOL_MAX_PAYLOAD / sizeof(uint32_t)];
    size_t last_response_length;
    bool has_last_response;
    uint8_t last_command;
    uint16_t last_sequence;
    uint32_t last_payload_crc;
    uint16_t expected_sequence;
    bool update_active;
    int target_index;
    esp_partition_pos_t target_partition;
    uint32_t image_size;
    uint32_t next_offset;
    uint8_t expected_sha256[IAP_SHA256_SIZE];
} iap_bootloader_context_t;

static iap_bootloader_context_t s_context;

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

static void feed_rtc_watchdog(void)
{
#ifdef CONFIG_BOOTLOADER_WDT_ENABLE
    wdt_hal_context_t watchdog = RWDT_HAL_CONTEXT_DEFAULT();
    wdt_hal_write_protect_disable(&watchdog);
    wdt_hal_feed(&watchdog);
    wdt_hal_write_protect_enable(&watchdog);
#endif
}

static bool receive_frame(iap_bootloader_context_t *context,
                          uint32_t timeout_ms)
{
    uint32_t poll_limit = timeout_ms * IAP_POLLS_PER_MS;

    for (uint32_t poll = 0; poll < poll_limit; ++poll) {
        uint8_t byte = 0;
        if (esp_rom_output_rx_one_char(&byte) == 0) {
            iap_parse_result_t result = iap_protocol_parser_push(
                &context->parser, byte, &context->received_frame);
            if (result == IAP_PARSE_FRAME) {
                return true;
            }
        } else {
            esp_rom_delay_us(IAP_POLL_INTERVAL_US);
        }

        if ((poll % IAP_WDT_FEED_POLLS) == 0U) {
            feed_rtc_watchdog();
        }
    }
    return false;
}

static bool send_bytes(const uint8_t *data, size_t length)
{
    for (size_t index = 0; index < length; ++index) {
        if (esp_rom_output_tx_one_char(data[index]) != 0) {
            return false;
        }
    }
    esp_rom_output_tx_wait_idle(ESP_ROM_UART_0);
    return true;
}

static bool send_response(iap_bootloader_context_t *context,
                          uint8_t request_command,
                          uint16_t request_sequence,
                          iap_status_t status,
                          const char *detail,
                          bool cache_response)
{
    size_t detail_length = detail == NULL ? 0 : strlen(detail);
    if (detail_length > IAP_RESPONSE_MAX_DETAIL) {
        detail_length = IAP_RESPONSE_MAX_DETAIL;
    }

    iap_frame_t *response = &context->response_frame;
    response->version = IAP_PROTOCOL_VERSION;
    response->command = (uint8_t)(request_command | 0x80U);
    response->sequence = request_sequence;
    write_le16(&response->payload[0], (uint16_t)status);
    write_le16(&response->payload[2], context->expected_sequence);
    write_le32(&response->payload[4], context->next_offset);
    response->payload[8] = (uint8_t)detail_length;
    if (detail_length > 0) {
        memcpy(&response->payload[IAP_RESPONSE_PREFIX_SIZE],
               detail, detail_length);
    }
    response->payload_length = (uint16_t)(IAP_RESPONSE_PREFIX_SIZE
                                          + detail_length);

    size_t encoded_length = 0;
    if (!iap_protocol_encode(response,
                             context->encoded_response,
                             sizeof(context->encoded_response),
                             &encoded_length)) {
        return false;
    }

    if (cache_response) {
        memcpy(context->last_response,
               context->encoded_response,
               encoded_length);
        context->last_response_length = encoded_length;
        context->has_last_response = true;
    }
    return send_bytes(context->encoded_response, encoded_length);
}

static void abandon_update(iap_bootloader_context_t *context)
{
    context->update_active = false;
    context->image_size = 0;
    context->next_offset = 0;
    memset(context->expected_sha256, 0, sizeof(context->expected_sha256));
}

static bool choose_target(iap_bootloader_context_t *context,
                          const bootloader_state_t *bootloader_state,
                          int current_boot_index)
{
    if (bootloader_state->app_count < 2) {
        return false;
    }

    int target_index = current_boot_index == 0 ? 1 : 0;
    if (target_index < 0
            || target_index >= (int)bootloader_state->app_count
            || bootloader_state->ota[target_index].offset == 0
            || bootloader_state->ota[target_index].size == 0) {
        return false;
    }

    context->target_index = target_index;
    context->target_partition = bootloader_state->ota[target_index];
    return true;
}

static esp_err_t erase_target_range(iap_bootloader_context_t *context,
                                    uint32_t image_size)
{
    if (image_size > UINT32_MAX - (FLASH_SECTOR_SIZE - 1U)) {
        return ESP_ERR_INVALID_SIZE;
    }
    uint32_t erase_size = (image_size + FLASH_SECTOR_SIZE - 1U)
                          & ~(FLASH_SECTOR_SIZE - 1U);
    if (erase_size > context->target_partition.size) {
        return ESP_ERR_INVALID_SIZE;
    }

    uint32_t erased = 0;
    while (erased < erase_size) {
        uint32_t chunk = erase_size - erased;
        if (chunk > IAP_FLASH_BLOCK_SIZE) {
            chunk = IAP_FLASH_BLOCK_SIZE;
        }
        esp_err_t error = bootloader_flash_erase_range(
            context->target_partition.offset + erased, chunk);
        if (error != ESP_OK) {
            return error;
        }
        erased += chunk;
        feed_rtc_watchdog();
    }
    return ESP_OK;
}

static iap_status_t begin_update(iap_bootloader_context_t *context,
                                 const iap_frame_t *frame,
                                 const char **detail)
{
    if (esp_flash_encryption_enabled()) {
        *detail = "Flash Encryption unsupported in protocol v1";
        return IAP_STATUS_ERR_STATE;
    }
    if (context->target_partition.offset == 0) {
        *detail = "two OTA slots are required";
        return IAP_STATUS_ERR_STATE;
    }
    if (frame->payload_length < IAP_BEGIN_PREFIX_SIZE) {
        *detail = "BEGIN payload is truncated";
        return IAP_STATUS_ERR_LENGTH;
    }

    uint32_t image_size = read_le32(&frame->payload[0]);
    uint8_t version_length = frame->payload[36];
    if (image_size == 0
            || image_size > context->target_partition.size
            || version_length > IAP_BEGIN_MAX_VERSION
            || frame->payload_length != IAP_BEGIN_PREFIX_SIZE + version_length) {
        *detail = "invalid image size or version length";
        return IAP_STATUS_ERR_LENGTH;
    }

    abandon_update(context);
    esp_err_t error = erase_target_range(context, image_size);
    if (error != ESP_OK) {
        ESP_LOGE(TAG, "erase failed at 0x%08" PRIx32 ": %s",
                 context->target_partition.offset, esp_err_to_name(error));
        *detail = "target erase failed";
        return IAP_STATUS_ERR_FLASH;
    }

    context->update_active = true;
    context->image_size = image_size;
    context->next_offset = 0;
    memcpy(context->expected_sha256, &frame->payload[4], IAP_SHA256_SIZE);
    return IAP_STATUS_OK;
}

static iap_status_t write_update(iap_bootloader_context_t *context,
                                 const iap_frame_t *frame,
                                 const char **detail)
{
    if (!context->update_active) {
        *detail = "BEGIN is required before DATA";
        return IAP_STATUS_ERR_STATE;
    }
    if (frame->payload_length <= IAP_DATA_OFFSET_SIZE) {
        *detail = "DATA payload has no image bytes";
        return IAP_STATUS_ERR_LENGTH;
    }

    uint32_t offset = read_le32(&frame->payload[0]);
    uint32_t data_length = frame->payload_length - IAP_DATA_OFFSET_SIZE;
    if (offset != context->next_offset) {
        *detail = "unexpected DATA offset";
        return IAP_STATUS_ERR_OFFSET;
    }
    if (context->next_offset > context->image_size
            || data_length > context->image_size - context->next_offset) {
        *detail = "DATA exceeds declared image size";
        return IAP_STATUS_ERR_LENGTH;
    }

    bool is_final = context->next_offset + data_length == context->image_size;
    if (!is_final && (data_length % IAP_WRITE_ALIGNMENT) != 0U) {
        *detail = "non-final DATA length must be 4-byte aligned";
        return IAP_STATUS_ERR_LENGTH;
    }

    uint32_t padded_length = (data_length + IAP_WRITE_ALIGNMENT - 1U)
                             & ~(IAP_WRITE_ALIGNMENT - 1U);
    uint8_t *write_buffer = (uint8_t *)context->aligned_write_buffer;
    memset(write_buffer, 0xFF, padded_length);
    memcpy(write_buffer, &frame->payload[IAP_DATA_OFFSET_SIZE], data_length);

    esp_err_t error = bootloader_flash_write(
        context->target_partition.offset + context->next_offset,
        write_buffer, padded_length, false);
    if (error != ESP_OK) {
        ESP_LOGE(TAG, "write failed at image offset 0x%08" PRIx32 ": %s",
                 context->next_offset, esp_err_to_name(error));
        *detail = "target write failed";
        return IAP_STATUS_ERR_FLASH;
    }
    context->next_offset += data_length;
    feed_rtc_watchdog();
    return IAP_STATUS_OK;
}

static esp_err_t commit_target(const bootloader_state_t *bootloader_state,
                               int target_index)
{
    esp_ota_select_entry_t otadata[2];
    esp_err_t error = bootloader_common_read_otadata(
        &bootloader_state->ota_info, otadata);
    if (error != ESP_OK || bootloader_state->app_count == 0) {
        return error == ESP_OK ? ESP_ERR_INVALID_STATE : error;
    }

    int active_entry = bootloader_common_get_active_otadata(otadata);
    int next_entry = active_entry >= 0 ? (active_entry ^ 1) : 0;
    uint32_t sequence = (uint32_t)target_index + 1U;
    if (active_entry >= 0) {
        uint32_t active_sequence = otadata[active_entry].ota_seq;
        while (sequence < active_sequence) {
            if (sequence > UINT32_MAX - bootloader_state->app_count) {
                return ESP_ERR_INVALID_SIZE;
            }
            sequence += bootloader_state->app_count;
        }
    }

    memset(&otadata[next_entry], 0xFF, sizeof(otadata[next_entry]));
    otadata[next_entry].ota_seq = sequence;
#ifdef CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE
    otadata[next_entry].ota_state = ESP_OTA_IMG_NEW;
#else
    otadata[next_entry].ota_state = ESP_OTA_IMG_UNDEFINED;
#endif
    otadata[next_entry].crc = bootloader_common_ota_select_crc(
        &otadata[next_entry]);

    uint32_t address = bootloader_state->ota_info.offset
                       + (uint32_t)next_entry * FLASH_SECTOR_SIZE;
    error = bootloader_flash_erase_sector(address / FLASH_SECTOR_SIZE);
    feed_rtc_watchdog();
    if (error == ESP_OK) {
        error = bootloader_flash_write(address,
                                       &otadata[next_entry],
                                       sizeof(otadata[next_entry]),
                                       false);
    }
    return error;
}

static iap_status_t finish_update(iap_bootloader_context_t *context,
                                  const bootloader_state_t *bootloader_state,
                                  const iap_frame_t *frame,
                                  const char **detail)
{
    if (frame->payload_length != 0) {
        *detail = "END payload must be empty";
        return IAP_STATUS_ERR_LENGTH;
    }
    if (!context->update_active) {
        *detail = "no update is in progress";
        return IAP_STATUS_ERR_STATE;
    }
    if (context->next_offset != context->image_size) {
        *detail = "image is incomplete";
        return IAP_STATUS_ERR_LENGTH;
    }

    uint8_t actual_sha256[IAP_SHA256_SIZE];
    feed_rtc_watchdog();
    esp_err_t error = bootloader_sha256_flash_contents(
        context->target_partition.offset,
        context->image_size,
        actual_sha256);
    feed_rtc_watchdog();
    if (error != ESP_OK) {
        *detail = "Flash SHA-256 failed";
        return IAP_STATUS_ERR_FLASH;
    }
    if (memcmp(actual_sha256,
               context->expected_sha256,
               IAP_SHA256_SIZE) != 0) {
        *detail = "whole-image SHA-256 mismatch";
        return IAP_STATUS_ERR_HASH;
    }

    esp_image_metadata_t metadata = {0};
    error = esp_image_verify(ESP_IMAGE_VERIFY,
                             &context->target_partition,
                             &metadata);
    feed_rtc_watchdog();
    if (error != ESP_OK || metadata.image_len != context->image_size) {
        *detail = "ESP image validation failed";
        return IAP_STATUS_ERR_IMAGE;
    }

    error = commit_target(bootloader_state, context->target_index);
    if (error != ESP_OK) {
        ESP_LOGE(TAG, "otadata commit failed: %s", esp_err_to_name(error));
        *detail = "otadata commit failed";
        return IAP_STATUS_ERR_FLASH;
    }

    context->update_active = false;
    return IAP_STATUS_OK;
}

static bool request_matches_last(const iap_bootloader_context_t *context,
                                 const iap_frame_t *frame,
                                 uint32_t payload_crc)
{
    return context->has_last_response
           && frame->command == context->last_command
           && frame->sequence == context->last_sequence
           && payload_crc == context->last_payload_crc;
}

static session_action_t process_frame(
    iap_bootloader_context_t *context,
    const bootloader_state_t *bootloader_state,
    const iap_frame_t *frame)
{
    uint32_t payload_crc = iap_protocol_crc32(frame->payload,
                                              frame->payload_length);
    if (request_matches_last(context, frame, payload_crc)) {
        send_bytes(context->last_response, context->last_response_length);
        return SESSION_CONTINUE;
    }

    if (frame->version != IAP_PROTOCOL_VERSION) {
        send_response(context, frame->command, frame->sequence,
                      IAP_STATUS_ERR_VERSION, "unsupported protocol version",
                      false);
        return SESSION_CONTINUE;
    }

    if (frame->command == IAP_COMMAND_HELLO) {
        if (frame->sequence != 0) {
            send_response(context, frame->command, frame->sequence,
                          IAP_STATUS_ERR_SEQUENCE,
                          "HELLO sequence must be zero", false);
            return SESSION_CONTINUE;
        }
        abandon_update(context);
        context->expected_sequence = 0;
        context->has_last_response = false;
    } else if (frame->sequence != context->expected_sequence) {
        send_response(context, frame->command, frame->sequence,
                      IAP_STATUS_ERR_SEQUENCE, "unexpected request sequence",
                      false);
        return SESSION_CONTINUE;
    }

    iap_status_t status = IAP_STATUS_OK;
    const char *detail = NULL;
    session_action_t action = SESSION_CONTINUE;

    switch (frame->command) {
    case IAP_COMMAND_HELLO:
        detail = "serial-iap-bootloader;protocol=1;chip=esp32s3";
        break;
    case IAP_COMMAND_INFO:
        if (context->target_index == 0) {
            detail = "mode=bootloader;inactive=ota_0;flash-encryption=off";
        } else if (context->target_index == 1) {
            detail = "mode=bootloader;inactive=ota_1;flash-encryption=off";
        } else {
            detail = "mode=bootloader;inactive=none";
        }
        break;
    case IAP_COMMAND_BEGIN:
        status = begin_update(context, frame, &detail);
        break;
    case IAP_COMMAND_DATA:
        status = write_update(context, frame, &detail);
        break;
    case IAP_COMMAND_END:
        status = finish_update(context, bootloader_state, frame, &detail);
        if (status == IAP_STATUS_OK) {
            action = SESSION_RESET;
        }
        break;
    case IAP_COMMAND_ABORT:
        if (frame->payload_length != 0) {
            status = IAP_STATUS_ERR_LENGTH;
            detail = "ABORT payload must be empty";
        } else {
            abandon_update(context);
        }
        break;
    case IAP_COMMAND_BOOT:
        if (frame->payload_length != 0) {
            status = IAP_STATUS_ERR_LENGTH;
            detail = "BOOT payload must be empty";
        } else {
            abandon_update(context);
            action = SESSION_BOOT;
        }
        break;
    default:
        status = IAP_STATUS_ERR_COMMAND;
        detail = "unsupported command";
        break;
    }

    bool success = status == IAP_STATUS_OK;
    if (success) {
        context->expected_sequence++;
        context->last_command = frame->command;
        context->last_sequence = frame->sequence;
        context->last_payload_crc = payload_crc;
    }
    send_response(context, frame->command, frame->sequence,
                  status, detail, success);
    return action;
}

void iap_bootloader_run(const bootloader_state_t *bootloader_state,
                        int current_boot_index)
{
    if (!iap_protocol_self_test()) {
        ESP_LOGE(TAG, "Serial IAP protocol core self-test failed");
        bootloader_reset();
    }

    memset(&s_context, 0, sizeof(s_context));
    iap_protocol_parser_init(&s_context.parser);
    s_context.target_index = INVALID_INDEX;
    choose_target(&s_context, bootloader_state, current_boot_index);

    ESP_LOGI(TAG, "Serial IAP v%u: waiting %u ms for HELLO",
             IAP_PROTOCOL_VERSION, IAP_ENTRY_TIMEOUT_MS);
    if (!receive_frame(&s_context, IAP_ENTRY_TIMEOUT_MS)) {
        return;
    }

    const iap_frame_t *first = &s_context.received_frame;
    if (first->version != IAP_PROTOCOL_VERSION
            || first->command != IAP_COMMAND_HELLO
            || first->sequence != 0) {
        return;
    }

    process_frame(&s_context, bootloader_state, first);
    ESP_LOGI(TAG, "Serial IAP session active; target OTA index %d",
             s_context.target_index);

    uint32_t idle_ms = 0;
    while (idle_ms < IAP_SESSION_TIMEOUT_MS) {
        if (!receive_frame(&s_context, 100U)) {
            idle_ms += 100U;
            continue;
        }
        idle_ms = 0;

        session_action_t action = process_frame(
            &s_context, bootloader_state, &s_context.received_frame);
        if (action == SESSION_BOOT) {
            ESP_LOGI(TAG, "Leaving Serial IAP and booting selected app");
            return;
        }
        if (action == SESSION_RESET) {
            ESP_LOGI(TAG, "Serial IAP committed; restarting");
            esp_rom_delay_us(100000U);
            bootloader_reset();
        }
    }

    ESP_LOGW(TAG, "Serial IAP timed out; boot selection was not changed");
    abandon_update(&s_context);
}
