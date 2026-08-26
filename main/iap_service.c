#include "iap_service.h"

#include <inttypes.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "driver/uart.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_rom_uart.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "iap_protocol.h"
#include "mbedtls/sha256.h"

#define IAP_UART_PORT                 UART_NUM_0
#define IAP_UART_BAUD_RATE            115200
#define IAP_UART_RX_BUFFER_SIZE       4096
#define IAP_UART_READ_BUFFER_SIZE     256
#define IAP_UART_READ_TIMEOUT_MS      100
#define IAP_UART_TX_TIMEOUT_MS        1000
#define IAP_SESSION_TIMEOUT_MS        10000
#define IAP_TASK_STACK_SIZE           8192
#define IAP_TASK_PRIORITY             5
#define IAP_RESPONSE_PREFIX_SIZE      9U
#define IAP_RESPONSE_MAX_DETAIL       96U
#define IAP_BEGIN_PREFIX_SIZE         37U
#define IAP_BEGIN_MAX_VERSION         32U
#define IAP_DATA_OFFSET_SIZE          4U

static const char *TAG = "serial_iap";

typedef enum {
    UPDATE_IDLE = 0,
    UPDATE_RECEIVING,
    UPDATE_COMMITTED,
} update_state_t;

typedef struct {
    iap_parser_t parser;
    iap_frame_t received_frame;
    update_state_t update_state;
    esp_ota_handle_t ota_handle;
    const esp_partition_t *target_partition;
    uint32_t image_size;
    uint32_t next_offset;
    uint8_t expected_sha256[32];
    uint16_t expected_sequence;
    int64_t last_valid_frame_us;
    bool session_active;
    bool has_last_response;
    uint8_t last_command;
    uint16_t last_sequence;
    uint32_t last_payload_crc;
    size_t last_response_length;
    uint8_t last_response[IAP_PROTOCOL_MAX_FRAME_SIZE];
} iap_service_context_t;

static iap_service_context_t s_context;

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

static bool append_info_text(char *buffer,
                             size_t capacity,
                             size_t *length,
                             const char *text,
                             size_t maximum_length)
{
    while (*text != '\0' && maximum_length > 0) {
        if (*length + 1U >= capacity) {
            return false;
        }
        buffer[*length] = *text;
        (*length)++;
        text++;
        maximum_length--;
    }
    buffer[*length] = '\0';
    return true;
}

static void abort_update(iap_service_context_t *context)
{
    if (context->update_state == UPDATE_RECEIVING) {
        esp_err_t error = esp_ota_abort(context->ota_handle);
        if (error != ESP_OK) {
            ESP_LOGW(TAG, "esp_ota_abort failed: %s", esp_err_to_name(error));
        }
    }
    context->update_state = UPDATE_IDLE;
    context->ota_handle = 0;
    context->target_partition = NULL;
    context->image_size = 0;
    context->next_offset = 0;
    memset(context->expected_sha256, 0, sizeof(context->expected_sha256));
}

static esp_err_t send_encoded(const uint8_t *data, size_t length)
{
    int written = uart_write_bytes(IAP_UART_PORT, data, length);
    if (written < 0 || (size_t)written != length) {
        return ESP_FAIL;
    }
    return uart_wait_tx_done(IAP_UART_PORT,
                             pdMS_TO_TICKS(IAP_UART_TX_TIMEOUT_MS));
}

static esp_err_t resend_last_response(const iap_service_context_t *context)
{
    return send_encoded(context->last_response,
                        context->last_response_length);
}

static esp_err_t send_response(iap_service_context_t *context,
                               uint8_t request_command,
                               uint16_t request_sequence,
                               iap_status_t status,
                               const char *detail,
                               bool cache_response)
{
    iap_frame_t response = {
        .version = IAP_PROTOCOL_VERSION,
        .command = (uint8_t)(request_command | 0x80U),
        .sequence = request_sequence,
    };
    size_t detail_length = detail == NULL ? 0 : strlen(detail);
    if (detail_length > IAP_RESPONSE_MAX_DETAIL) {
        detail_length = IAP_RESPONSE_MAX_DETAIL;
    }

    write_le16(&response.payload[0], (uint16_t)status);
    write_le16(&response.payload[2], context->expected_sequence);
    write_le32(&response.payload[4], context->next_offset);
    response.payload[8] = (uint8_t)detail_length;
    if (detail_length > 0) {
        memcpy(&response.payload[IAP_RESPONSE_PREFIX_SIZE],
               detail,
               detail_length);
    }
    response.payload_length = (uint16_t)(IAP_RESPONSE_PREFIX_SIZE
                                         + detail_length);

    uint8_t encoded[IAP_PROTOCOL_MAX_FRAME_SIZE];
    size_t encoded_length = 0;
    if (!iap_protocol_encode(&response, encoded, sizeof(encoded),
                             &encoded_length)) {
        return ESP_ERR_INVALID_SIZE;
    }

    esp_err_t error = send_encoded(encoded, encoded_length);
    if (error == ESP_OK && cache_response) {
        memcpy(context->last_response, encoded, encoded_length);
        context->last_response_length = encoded_length;
        context->has_last_response = true;
    }
    return error;
}

static iap_status_t begin_update(iap_service_context_t *context,
                                 const iap_frame_t *frame,
                                 const char **detail)
{
    if (frame->payload_length < IAP_BEGIN_PREFIX_SIZE) {
        *detail = "BEGIN payload is truncated";
        return IAP_STATUS_ERR_LENGTH;
    }

    uint32_t image_size = read_le32(&frame->payload[0]);
    uint8_t version_length = frame->payload[36];
    if (image_size == 0
            || version_length > IAP_BEGIN_MAX_VERSION
            || frame->payload_length != IAP_BEGIN_PREFIX_SIZE + version_length) {
        *detail = "invalid image size or version length";
        return IAP_STATUS_ERR_LENGTH;
    }

    if (context->update_state == UPDATE_RECEIVING) {
        abort_update(context);
    }

    const esp_partition_t *target = esp_ota_get_next_update_partition(NULL);
    if (target == NULL || image_size > target->size) {
        *detail = "no suitable inactive OTA partition";
        return IAP_STATUS_ERR_LENGTH;
    }

    esp_ota_handle_t handle = 0;
    esp_err_t error = esp_ota_begin(target, image_size, &handle);
    if (error != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_begin failed: %s", esp_err_to_name(error));
        *detail = "OTA begin/erase failed";
        return IAP_STATUS_ERR_FLASH;
    }

    context->update_state = UPDATE_RECEIVING;
    context->ota_handle = handle;
    context->target_partition = target;
    context->image_size = image_size;
    context->next_offset = 0;
    memcpy(context->expected_sha256, &frame->payload[4], 32);
    ESP_LOGI(TAG, "Receiving %" PRIu32 " bytes into %s at 0x%08" PRIx32,
             image_size, target->label, target->address);
    return IAP_STATUS_OK;
}

static iap_status_t write_update(iap_service_context_t *context,
                                 const iap_frame_t *frame,
                                 const char **detail)
{
    if (context->update_state != UPDATE_RECEIVING) {
        *detail = "BEGIN is required before DATA";
        return IAP_STATUS_ERR_STATE;
    }
    if (frame->payload_length <= IAP_DATA_OFFSET_SIZE) {
        *detail = "DATA payload has no image bytes";
        return IAP_STATUS_ERR_LENGTH;
    }

    uint32_t offset = read_le32(&frame->payload[0]);
    size_t data_length = frame->payload_length - IAP_DATA_OFFSET_SIZE;
    if (offset != context->next_offset) {
        *detail = "unexpected DATA offset";
        return IAP_STATUS_ERR_OFFSET;
    }
    if (data_length > context->image_size - context->next_offset) {
        *detail = "DATA exceeds declared image size";
        return IAP_STATUS_ERR_LENGTH;
    }

    esp_err_t error = esp_ota_write(context->ota_handle,
                                    &frame->payload[IAP_DATA_OFFSET_SIZE],
                                    data_length);
    if (error != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_write failed at 0x%08" PRIx32 ": %s",
                 context->next_offset, esp_err_to_name(error));
        *detail = "Flash write failed";
        return IAP_STATUS_ERR_FLASH;
    }
    context->next_offset += data_length;
    return IAP_STATUS_OK;
}

static esp_err_t calculate_written_sha256(const iap_service_context_t *context,
                                          uint8_t output[32])
{
    uint8_t buffer[1024];
    uint32_t offset = 0;
    mbedtls_sha256_context sha;
    mbedtls_sha256_init(&sha);
    int crypto_error = mbedtls_sha256_starts(&sha, 0);

    while (crypto_error == 0 && offset < context->image_size) {
        size_t length = context->image_size - offset;
        if (length > sizeof(buffer)) {
            length = sizeof(buffer);
        }
        esp_err_t error = esp_partition_read(context->target_partition,
                                             offset, buffer, length);
        if (error != ESP_OK) {
            mbedtls_sha256_free(&sha);
            return error;
        }
        crypto_error = mbedtls_sha256_update(&sha, buffer, length);
        offset += length;
    }
    if (crypto_error == 0) {
        crypto_error = mbedtls_sha256_finish(&sha, output);
    }
    mbedtls_sha256_free(&sha);
    return crypto_error == 0 ? ESP_OK : ESP_FAIL;
}

static iap_status_t finish_update(iap_service_context_t *context,
                                  const iap_frame_t *frame,
                                  const char **detail)
{
    if (frame->payload_length != 0) {
        *detail = "END payload must be empty";
        return IAP_STATUS_ERR_LENGTH;
    }
    if (context->update_state != UPDATE_RECEIVING) {
        *detail = "no update is in progress";
        return IAP_STATUS_ERR_STATE;
    }
    if (context->next_offset != context->image_size) {
        *detail = "image is incomplete";
        return IAP_STATUS_ERR_LENGTH;
    }

    esp_ota_handle_t handle = context->ota_handle;
    context->ota_handle = 0;
    esp_err_t error = esp_ota_end(handle);
    if (error != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_end image validation failed: %s",
                 esp_err_to_name(error));
        context->update_state = UPDATE_IDLE;
        context->next_offset = 0;
        *detail = "ESP image validation failed";
        return IAP_STATUS_ERR_IMAGE;
    }

    uint8_t actual_sha256[32];
    error = calculate_written_sha256(context, actual_sha256);
    if (error != ESP_OK) {
        context->update_state = UPDATE_IDLE;
        context->next_offset = 0;
        *detail = "Flash read-back failed";
        return IAP_STATUS_ERR_FLASH;
    }
    if (memcmp(actual_sha256, context->expected_sha256, 32) != 0) {
        context->update_state = UPDATE_IDLE;
        context->next_offset = 0;
        *detail = "whole-image SHA-256 mismatch";
        return IAP_STATUS_ERR_HASH;
    }

    error = esp_ota_set_boot_partition(context->target_partition);
    if (error != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_set_boot_partition failed: %s",
                 esp_err_to_name(error));
        context->update_state = UPDATE_IDLE;
        context->next_offset = 0;
        *detail = "could not commit boot partition";
        return IAP_STATUS_ERR_FLASH;
    }

    context->update_state = UPDATE_COMMITTED;
    ESP_LOGI(TAG, "Image verified; next boot partition is %s",
             context->target_partition->label);
    return IAP_STATUS_OK;
}

static bool request_matches_last(const iap_service_context_t *context,
                                 const iap_frame_t *frame,
                                 uint32_t payload_crc)
{
    return context->has_last_response
           && frame->sequence == context->last_sequence
           && frame->command == context->last_command
           && payload_crc == context->last_payload_crc;
}

static void process_frame(iap_service_context_t *context,
                          const iap_frame_t *frame)
{
    context->last_valid_frame_us = esp_timer_get_time();
    uint32_t payload_crc = iap_protocol_crc32(frame->payload,
                                              frame->payload_length);

    if (request_matches_last(context, frame, payload_crc)) {
        ESP_LOGD(TAG, "Repeating cached response for sequence %u",
                 frame->sequence);
        resend_last_response(context);
        return;
    }

    if (frame->version != IAP_PROTOCOL_VERSION) {
        send_response(context, frame->command, frame->sequence,
                      IAP_STATUS_ERR_VERSION, "unsupported protocol version",
                      false);
        return;
    }

    if (frame->command == IAP_COMMAND_HELLO && frame->sequence == 0) {
        abort_update(context);
        context->expected_sequence = 0;
        context->session_active = true;
        context->has_last_response = false;
    } else if (!context->session_active) {
        send_response(context, frame->command, frame->sequence,
                      IAP_STATUS_ERR_STATE, "HELLO is required first",
                      false);
        return;
    } else if (frame->sequence != context->expected_sequence) {
        send_response(context, frame->command, frame->sequence,
                      IAP_STATUS_ERR_SEQUENCE, "unexpected request sequence",
                      false);
        return;
    }

    iap_status_t status = IAP_STATUS_OK;
    const char *detail = NULL;
    char info_detail[IAP_RESPONSE_MAX_DETAIL + 1U];
    bool restart_after_response = false;

    switch (frame->command) {
    case IAP_COMMAND_HELLO:
        detail = "serial-iap-app;protocol=1;chip=esp32s3";
        break;
    case IAP_COMMAND_INFO: {
        const esp_app_desc_t *description = esp_app_get_description();
        const esp_partition_t *running = esp_ota_get_running_partition();
        size_t info_length = 0;
        bool info_ok = append_info_text(info_detail, sizeof(info_detail),
                                        &info_length, "mode=application;version=",
                                        SIZE_MAX)
                       && append_info_text(info_detail, sizeof(info_detail),
                                           &info_length, description->version, 32U)
                       && append_info_text(info_detail, sizeof(info_detail),
                                           &info_length, ";active=", SIZE_MAX)
                       && append_info_text(info_detail, sizeof(info_detail),
                                           &info_length, running->label, 16U);
        detail = info_ok ? info_detail : "mode=application;info=unavailable";
        break;
    }
    case IAP_COMMAND_BEGIN:
        status = begin_update(context, frame, &detail);
        break;
    case IAP_COMMAND_DATA:
        status = write_update(context, frame, &detail);
        break;
    case IAP_COMMAND_END:
        status = finish_update(context, frame, &detail);
        restart_after_response = status == IAP_STATUS_OK;
        break;
    case IAP_COMMAND_ABORT:
        if (frame->payload_length != 0) {
            status = IAP_STATUS_ERR_LENGTH;
            detail = "ABORT payload must be empty";
        } else {
            abort_update(context);
        }
        break;
    case IAP_COMMAND_BOOT:
        if (frame->payload_length != 0) {
            status = IAP_STATUS_ERR_LENGTH;
            detail = "BOOT payload must be empty";
        } else {
            abort_update(context);
            restart_after_response = true;
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
    esp_err_t error = send_response(context, frame->command, frame->sequence,
                                    status, detail, success);
    if (error != ESP_OK) {
        ESP_LOGE(TAG, "Failed to send protocol response: %s",
                 esp_err_to_name(error));
    }

    if (restart_after_response && error == ESP_OK) {
        vTaskDelay(pdMS_TO_TICKS(100));
        esp_restart();
    }
}

static void iap_service_task(void *argument)
{
    iap_service_context_t *context = argument;
    uint8_t input[IAP_UART_READ_BUFFER_SIZE];

    while (true) {
        int received = uart_read_bytes(IAP_UART_PORT, input, sizeof(input),
                                       pdMS_TO_TICKS(IAP_UART_READ_TIMEOUT_MS));
        if (received < 0) {
            ESP_LOGE(TAG, "UART read failed");
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }

        for (int index = 0; index < received; ++index) {
            iap_parse_result_t result = iap_protocol_parser_push(
                &context->parser, input[index], &context->received_frame);
            if (result == IAP_PARSE_FRAME) {
                process_frame(context, &context->received_frame);
            } else if (result != IAP_PARSE_NONE) {
                ESP_LOGW(TAG, "Dropped invalid protocol candidate (%d)", result);
            }
        }

        if (context->session_active) {
            int64_t elapsed_ms = (esp_timer_get_time()
                                  - context->last_valid_frame_us) / 1000;
            if (elapsed_ms >= IAP_SESSION_TIMEOUT_MS) {
                ESP_LOGW(TAG, "IAP session timed out; keeping current app");
                abort_update(context);
                context->expected_sequence = 0;
                context->session_active = false;
                context->has_last_response = false;
            }
        }
    }
}

esp_err_t iap_service_start(void)
{
    if (!iap_protocol_self_test()) {
        ESP_LOGE(TAG, "C protocol self-test failed");
        return ESP_FAIL;
    }

    // app_main logs use the ROM console path. Wait before the UART driver
    // reconfigures UART0, otherwise the tail of the previous log can corrupt.
    esp_rom_output_tx_wait_idle(ESP_ROM_UART_0);

    uart_config_t uart_config = {
        .baud_rate = IAP_UART_BAUD_RATE,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    esp_err_t error = uart_param_config(IAP_UART_PORT, &uart_config);
    if (error != ESP_OK) {
        return error;
    }
    error = uart_set_pin(IAP_UART_PORT,
                         UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE,
                         UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
    if (error != ESP_OK) {
        return error;
    }
    error = uart_driver_install(IAP_UART_PORT,
                                IAP_UART_RX_BUFFER_SIZE,
                                0, 0, NULL, 0);
    if (error != ESP_OK) {
        return error;
    }
    uart_flush_input(IAP_UART_PORT);

    memset(&s_context, 0, sizeof(s_context));
    iap_protocol_parser_init(&s_context.parser);
    s_context.last_valid_frame_us = esp_timer_get_time();

    BaseType_t task_created = xTaskCreate(iap_service_task,
                                          "serial_iap",
                                          IAP_TASK_STACK_SIZE,
                                          &s_context,
                                          IAP_TASK_PRIORITY,
                                          NULL);
    if (task_created != pdPASS) {
        uart_driver_delete(IAP_UART_PORT);
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG, "UART IAP service ready on UART%d at %d baud",
             IAP_UART_PORT, IAP_UART_BAUD_RATE);
    return ESP_OK;
}
