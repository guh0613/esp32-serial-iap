#include <inttypes.h>

#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "iap_service.h"

static const char *TAG = "serial_iap_demo";

void app_main(void)
{
    const esp_app_desc_t *description = esp_app_get_description();
    const esp_partition_t *running = esp_ota_get_running_partition();

    ESP_LOGI(TAG, "Serial IAP demo application is running");
    ESP_LOGI(TAG, "Project: %s", description->project_name);
    ESP_LOGI(TAG, "Version: %s", description->version);
    ESP_LOGI(TAG, "Partition: %s at 0x%08" PRIx32,
             running->label, running->address);

    esp_err_t error = iap_service_start();
    if (error != ESP_OK) {
        ESP_LOGE(TAG, "Could not start UART IAP service: %s",
                 esp_err_to_name(error));
    }
}
